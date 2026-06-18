"""Pipeline orchestrator — runs the agents over a shared DigestRun state.

Collector -> Filter -> Summarizer -> Reporter, snapshotting state after each stage and
recording the run (status, counts, digest path) in the database.

NOTE: the Collector uses Playwright's sync API, so callers inside an asyncio event loop
(FastAPI) must invoke this in a worker thread.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone

from agents.base import AgentContext
from agents.clusterer import EmbeddingClusterer
from agents.collector import Collector
from agents.filter import Filter
from agents.reporter import Reporter
from agents.summarizer import Summarizer
from agents.threader import ThreadStitcher
from config import settings
from db.models import (ClusteringMethod, DigestRun as DigestRunRow, DigestStyle,
                       JobRun, RawTweet, RunStatus, Tweet)
from db.session import get_session, get_settings
from state import DigestRun, TweetItem, load_latest_snapshot

logger = logging.getLogger("pipeline")

# Shared guard so the UI "Run now" and the scheduler never run concurrently.
_run_lock = threading.Lock()


def is_running() -> bool:
    return _run_lock.locked()


def run_guarded(max_accounts: int | None = None) -> DigestRun | None:
    """Run the pipeline unless one is already in progress."""
    if not _run_lock.acquire(blocking=False):
        logger.info("A run is already in progress; skipping.")
        return None
    try:
        return run(max_accounts=max_accounts)
    except Exception:
        return None  # already recorded on the DigestRun row
    finally:
        _run_lock.release()


def resume_guarded(run_id: int | None = None) -> DigestRun | None:
    """Resume a failed run unless one is already in progress."""
    if not _run_lock.acquire(blocking=False):
        logger.info("A run is already in progress; skipping resume.")
        return None
    try:
        return resume(run_id=run_id)
    except Exception:
        return None  # already recorded on the DigestRun row
    finally:
        _run_lock.release()


def replay_guarded(source_run_id: int, overrides: dict | None = None,
                   deliver: bool = False) -> DigestRun | None:
    """Replay a past run (no re-scrape) unless one is already in progress."""
    if not _run_lock.acquire(blocking=False):
        logger.info("A run is already in progress; skipping replay.")
        return None
    try:
        return replay(source_run_id, overrides=overrides, deliver=deliver)
    except Exception:
        return None  # already recorded on the DigestRun row
    finally:
        _run_lock.release()


def collect_guarded(trigger: str = "schedule") -> int:
    """Scrape new tweets into the archive (Phase 1) unless a run is already in progress.

    Returns the number of newly-archived tweets (0 if skipped or nothing new).
    """
    if not _run_lock.acquire(blocking=False):
        logger.info("A run is already in progress; skipping collection.")
        return 0
    try:
        return collect(trigger=trigger)
    except Exception:
        logger.exception("Collection failed")
        return 0
    finally:
        _run_lock.release()


def refresh_draft_guarded(trigger: str = "schedule") -> DigestRun | None:
    """Refresh the live draft digest (Phase 2) unless a run is already in progress."""
    if not _run_lock.acquire(blocking=False):
        logger.info("A run is already in progress; skipping draft refresh.")
        return None
    try:
        return refresh_draft(trigger=trigger)
    except Exception:
        logger.exception("Draft refresh failed")
        return None
    finally:
        _run_lock.release()


def deliver_guarded() -> DigestRun | None:
    """Finalize + send the day's digest (Phase 3) unless a run is already in progress."""
    if not _run_lock.acquire(blocking=False):
        logger.info("A run is already in progress; skipping delivery.")
        return None
    try:
        return deliver()
    except Exception:
        return None  # already recorded on the DigestRun row
    finally:
        _run_lock.release()


def _to_namespace(aps, overrides: dict | None = None):
    """Copy a settings object into a plain namespace that can also hold per-run transient
    overrides (topics_override/deliver) the SQLModel itself would reject. Preserves enums."""
    from types import SimpleNamespace

    ns = SimpleNamespace(**{name: getattr(aps, name) for name in type(aps).model_fields})
    for key, value in (overrides or {}).items():
        setattr(ns, key, value)
    return ns


def _load_app_settings(overrides: dict | None = None):
    with get_session() as session:
        return _to_namespace(get_settings(session), overrides)


def _effective_topics(app_settings) -> list[str]:
    """Topics this run will use: a per-run override if set, else the global Topic table."""
    from sqlmodel import select

    from db.models import Topic
    override = getattr(app_settings, "topics_override", None)
    if override is not None:
        return override
    with get_session() as session:
        return [t.name for t in session.exec(select(Topic)).all()]


def _record_params(run_id: int, app_settings, source_run_id: int | None) -> None:
    """Snapshot the effective parameters onto the run row (so the UI shows 'what we did')."""
    with get_session() as session:
        row = session.get(DigestRunRow, run_id)
        if row is None:
            return
        row.source_run_id = source_run_id
        row.digest_style = app_settings.digest_style.value
        row.clustering_method = app_settings.clustering_method.value
        row.ollama_model = app_settings.ollama_model
        row.time_window_hours = app_settings.time_window_hours
        row.max_themes = app_settings.max_themes
        row.topics = ", ".join(_effective_topics(app_settings)) or None
        session.add(row)
        session.commit()


def _create_run_row() -> int:
    with get_session() as session:
        row = DigestRunRow(status=RunStatus.running)
        session.add(row)
        session.commit()
        session.refresh(row)
        return row.id


def _finish_run_row(run_id: int, state: DigestRun, status: RunStatus, error: str | None) -> None:
    with get_session() as session:
        row = session.get(DigestRunRow, run_id)
        row.status = status
        row.finished_at = datetime.utcnow()
        row.tweet_count = len(state.filtered_tweets)
        row.theme_count = len(state.themes)
        row.digest_path = state.digest_path
        row.emailed = state.emailed
        row.telegram_sent = state.telegram_sent
        row.error = error
        handles = {t.handle for t in (state.raw_tweets or state.filtered_tweets)}
        if handles:
            row.account_count = len(handles)
        session.add(row)
        session.commit()


def _parse_created(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _archive_raw(run_id: int | None, state: DigestRun) -> int:
    """Append every collected (pre-filter) tweet to the raw archive for later analysis.

    Idempotent (dedup by tweet_id). Called right after collection so the archive captures
    everything even if the run later fails or the tweet is dropped by the filter. Returns the
    number of new rows added.
    """
    from sqlmodel import select

    if not state.raw_tweets:
        return 0
    with get_session() as session:
        existing = set(session.exec(select(RawTweet.tweet_id)).all())
        added = 0
        for t in state.raw_tweets:
            if t.tweet_id in existing:
                continue
            existing.add(t.tweet_id)
            session.add(RawTweet(
                tweet_id=t.tweet_id, handle=t.handle, author_name=t.author_name,
                text=t.text, url=t.url, created_at=_parse_created(t.created_at),
                likes=t.likes, retweets=t.retweets, is_retweet=t.is_retweet,
                reply_to=t.reply_to, is_self_reply=t.is_self_reply, run_id=run_id,
            ))
            added += 1
        session.commit()
    logger.info("Raw archive: +%d new (%d already archived)", added, len(state.raw_tweets) - added)
    return added


def backfill_raw_archive() -> int:
    """One-time historical import: populate the raw archive from existing 1_collected snapshots."""
    import json
    from pathlib import Path

    runs_dir = Path(settings.data_dir) / "runs"
    if not runs_dir.is_dir():
        return 0
    total = 0
    for d in sorted(p for p in runs_dir.iterdir() if p.is_dir()):
        snap = d / "1_collected.json"
        if not snap.is_file():
            continue
        data = json.loads(snap.read_text())
        run_id = data.get("run_id")
        added = _archive_raw(run_id if isinstance(run_id, int) else None,
                             DigestRun.from_dict(data))
        if added:
            logger.info("Backfilled %d raw tweets from %s", added, snap)
        total += added
    return total


def _refresh_daily_stats(run_id: int | None = None) -> None:
    """Rebuild the materialized daily series from the archive. Best-effort."""
    from agents import analytics

    try:
        analytics.recompute_daily_stats()
    except Exception:
        logger.exception("Daily-stats refresh failed for run %s (continuing)", run_id)


def _update_trends(run_id: int, state: DigestRun, app_settings, index_themes: bool = True) -> None:
    """Refresh materialized trends after a real run. Best-effort: never fails the run.

    Theme continuity is indexed only for *finalized* (delivered) digests — intraday draft
    refreshes pass index_themes=False so the live draft doesn't pollute theme history.
    Skipped entirely for replays (no new data to aggregate).
    """
    from agents import analytics

    _refresh_daily_stats(run_id)

    # Theme continuity is only meaningful for the themed digest style (titles are topics).
    if index_themes and getattr(app_settings, "digest_style", None) == DigestStyle.themed and state.themes:
        try:
            with get_session() as session:
                analytics.index_run_themes(
                    session, run_id, (state.started_at or "")[:10], state.themes,
                    {t.tweet_id: (t.likes + t.retweets) for t in state.filtered_tweets},
                    model=app_settings.embedding_model,
                )
        except Exception:
            logger.exception("Theme indexing failed for run %s (continuing)", run_id)


def _persist_tweets(run_id: int, state: DigestRun) -> None:
    """Store digested tweets so they're skipped on future days (cross-day dedup).

    Idempotent: tweet_id is globally unique, so we skip any id already stored. This lets a
    resumed run persist safely even if some ids were recorded earlier.
    """
    from sqlmodel import select

    with get_session() as session:
        existing = set(session.exec(select(Tweet.tweet_id)).all())
        for t in state.filtered_tweets:
            created = _parse_created(t.created_at)
            # Record every source id (a stitched thread covers several) for cross-day dedup.
            for tid in (t.member_ids or [t.tweet_id]):
                if tid in existing:
                    continue
                existing.add(tid)
                session.add(Tweet(
                    tweet_id=tid, handle=t.handle, author_name=t.author_name,
                    text=t.text, url=t.url, created_at=created, likes=t.likes,
                    retweets=t.retweets, is_retweet=t.is_retweet, run_id=run_id,
                ))
        session.commit()


def _stage_plan(ctx: AgentContext) -> list[tuple[str, "callable", "callable"]]:
    """Post-collection stages in order: (snapshot_label, should_run, agent_factory).

    Shared by run() and resume() so both execute the exact same sequence.
    """
    aps = ctx.app_settings
    return [
        ("2_filtered",   lambda: True,                 lambda: Filter(ctx)),
        ("2a_threaded",  lambda: aps.stitch_threads,   lambda: ThreadStitcher(ctx)),
        ("2b_clustered",
         lambda: aps.digest_style == DigestStyle.themed and aps.clustering_method == ClusteringMethod.embedding,
         lambda: EmbeddingClusterer(ctx)),
        ("3_summarized", lambda: True,                 lambda: Summarizer(ctx)),
        ("4_reported",   lambda: True,                 lambda: Reporter(ctx)),
    ]


def _run_stages(ctx: AgentContext, state: DigestRun, start_after: str | None) -> None:
    """Run the post-collection stages, skipping everything up to and including `start_after`."""
    plan = _stage_plan(ctx)
    labels = [label for label, _, _ in plan]
    start_idx = labels.index(start_after) + 1 if start_after in labels else 0
    for label, should_run, factory in plan[start_idx:]:
        if should_run():
            factory().run(state)
            state.snapshot(settings.data_dir, label)


def run(max_accounts: int | None = None) -> DigestRun:
    app_settings = _load_app_settings()
    ctx = AgentContext(config=settings, app_settings=app_settings, logger=logger)

    run_id = _create_run_row()
    _record_params(run_id, app_settings, source_run_id=None)
    state = DigestRun(run_id=run_id)
    logger.info("=== Digest run %s started at %s ===", run_id, datetime.now(timezone.utc).isoformat())

    try:
        Collector(ctx, max_accounts=max_accounts).run(state)
        state.snapshot(settings.data_dir, "1_collected")
        _archive_raw(run_id, state)
        _run_stages(ctx, state, start_after=None)

        _persist_tweets(run_id, state)
        _update_trends(run_id, state, app_settings)
        _finish_run_row(run_id, state, RunStatus.success, None)
        logger.info("=== Digest run %s succeeded: %d tweets, %d themes -> %s ===",
                    run_id, len(state.filtered_tweets), len(state.themes), state.digest_path)
    except Exception as e:
        state.error = str(e)
        _finish_run_row(run_id, state, RunStatus.failed, str(e))
        logger.exception("Digest run %s failed", run_id)
        raise

    return state


# ----------------------------------------------------------------------------------------------
# Decoupled phases: Collect (every N hrs) -> Process/draft (every M hrs) -> Deliver (evening).
# Collection scrapes into the raw archive; processing rebuilds a live draft digest from the
# archive without delivering; delivery finalizes the draft, sends it, and commits cross-day dedup.
# ----------------------------------------------------------------------------------------------

def _load_archive_window(hours: int) -> list[TweetItem]:
    """Build a digest's candidate tweet set from the raw archive — no scraping.

    Returns archived tweets created within the last `hours`. The Filter stage still re-applies
    the time window and cross-day dedup, so this only scopes the candidate pool. Archived
    timestamps are naive UTC; we emit tz-aware ISO so the Filter parses them correctly.
    """
    from sqlmodel import select

    cutoff = datetime.utcnow() - timedelta(hours=hours)
    with get_session() as session:
        rows = session.exec(
            select(RawTweet).where(RawTweet.created_at >= cutoff)
            .order_by(RawTweet.created_at.desc())
        ).all()
    items: list[TweetItem] = []
    for r in rows:
        created = r.created_at.replace(tzinfo=timezone.utc).isoformat() if r.created_at else None
        items.append(TweetItem(
            tweet_id=r.tweet_id, handle=r.handle, author_name=r.author_name,
            text=r.text, url=r.url, created_at=created, likes=r.likes,
            retweets=r.retweets, is_retweet=r.is_retweet,
            reply_to=r.reply_to, is_self_reply=r.is_self_reply,
        ))
    return items


def collect(trigger: str = "schedule") -> int:
    """Phase 1: scrape new tweets into the raw archive. No filter/summary/digest/delivery.

    The Collector skips tweets already archived (early-stop), so frequent collection stays cheap
    and avoids re-hammering X. Returns the number of newly-archived tweets. Records a
    CollectionRun row (even on error or zero-new) so the schedule's cadence is auditable.
    """
    app_settings = _load_app_settings()
    ctx = AgentContext(config=settings, app_settings=app_settings, logger=logger)
    state = DigestRun()
    logger.info("=== Collection started at %s ===", datetime.now(timezone.utc).isoformat())
    job_id = _start_job_run("collect", trigger)
    try:
        Collector(ctx).run(state)
        added = _archive_raw(None, state)
        _refresh_daily_stats()
    except Exception as e:
        _finish_job_run(job_id, len(state.raw_tweets), 0, "error", str(e))
        raise
    logger.info("=== Collection done: %d scraped, %d newly archived ===",
                len(state.raw_tweets), added)
    _finish_job_run(job_id, len(state.raw_tweets), added, "ok", None)
    return added


def _start_job_run(job: str, trigger: str) -> int:
    with get_session() as session:
        row = JobRun(job=job, trigger=trigger)
        session.add(row)
        session.commit()
        session.refresh(row)
        return row.id


def _finish_job_run(job_run_id: int, primary: int, secondary: int,
                    status: str, error: str | None) -> None:
    with get_session() as session:
        row = session.get(JobRun, job_run_id)
        if row is None:
            return
        row.finished_at = datetime.utcnow()
        row.primary_count = primary
        row.secondary_count = secondary
        row.status = status
        row.error = error
        session.add(row)
        session.commit()


def _create_draft_row() -> int:
    with get_session() as session:
        row = DigestRunRow(status=RunStatus.draft)
        session.add(row)
        session.commit()
        session.refresh(row)
        return row.id


def _current_draft_id() -> int | None:
    """The id of the day's in-progress draft digest, if one exists (delivery finalizes it)."""
    from sqlmodel import select

    with get_session() as session:
        row = session.exec(
            select(DigestRunRow).where(DigestRunRow.status == RunStatus.draft)
            .order_by(DigestRunRow.id.desc())
        ).first()
        return row.id if row else None


def _update_draft_row(run_id: int, state: DigestRun) -> None:
    """Refresh a draft row's counts/path in place (status stays 'draft')."""
    with get_session() as session:
        row = session.get(DigestRunRow, run_id)
        if row is None:
            return
        row.tweet_count = len(state.filtered_tweets)
        row.theme_count = len(state.themes)
        row.digest_path = state.digest_path
        handles = {t.handle for t in (state.raw_tweets or state.filtered_tweets)}
        if handles:
            row.account_count = len(handles)
        session.add(row)
        session.commit()


def refresh_draft(trigger: str = "schedule") -> DigestRun | None:
    """Phase 2: rebuild the live draft digest from the archive (render only, never delivers).

    Reuses the single open draft row (or creates one) so the portal shows one growing "Today"
    digest. Does NOT persist tweets — that happens only at delivery, so each refresh shows the
    full set of tweets collected since the last delivery rather than disjoint slices. Logs a
    JobRun per call (incl. no-op/error) so the processing schedule's cadence is visible.
    """
    job_id = _start_job_run("process", trigger)
    app_settings = _load_app_settings({"deliver": False})
    raw = _load_archive_window(app_settings.time_window_hours)
    if not raw:
        logger.info("Draft refresh: no tweets in the archive window yet.")
        _finish_job_run(job_id, 0, 0, "skipped", None)
        return None

    try:
        ctx = AgentContext(config=settings, app_settings=app_settings, logger=logger)
        run_id = _current_draft_id()
        if run_id is None:
            run_id = _create_draft_row()
            _record_params(run_id, app_settings, source_run_id=None)
        state = DigestRun(run_id=run_id)
        state.raw_tweets = raw
        logger.info("=== Draft refresh (run %s): %d archived tweets in window ===", run_id, len(raw))

        state.snapshot(settings.data_dir, "1_collected")
        _run_stages(ctx, state, start_after=None)   # deliver=False → renders but doesn't send
        _update_draft_row(run_id, state)
    except Exception as e:
        _finish_job_run(job_id, len(raw), 0, "error", str(e))
        raise
    logger.info("=== Draft refresh (run %s) done: %d tweets, %d themes -> %s ===",
                run_id, len(state.filtered_tweets), len(state.themes), state.digest_path)
    _finish_job_run(job_id, len(state.filtered_tweets), len(state.themes), "ok", None)
    return state


def deliver() -> DigestRun | None:
    """Phase 3: finalize and send the day's digest, then commit cross-day dedup.

    Takes over the open draft row (or creates one), re-processes the archive window to be
    current, delivers via email/Telegram, persists the digested tweets (so they won't repeat
    tomorrow), indexes theme continuity, and marks the run success.
    """
    app_settings = _load_app_settings({"deliver": True})
    raw = _load_archive_window(app_settings.time_window_hours)
    if not raw:
        logger.info("Delivery: nothing in the archive window to send.")
        return None

    ctx = AgentContext(config=settings, app_settings=app_settings, logger=logger)
    run_id = _current_draft_id()
    if run_id is None:
        run_id = _create_draft_row()
        _record_params(run_id, app_settings, source_run_id=None)
    _mark_running(run_id)
    state = DigestRun(run_id=run_id)
    state.raw_tweets = raw
    logger.info("=== Delivering digest (run %s): %d archived tweets in window ===", run_id, len(raw))

    try:
        state.snapshot(settings.data_dir, "1_collected")
        _run_stages(ctx, state, start_after=None)   # deliver=True → reporter sends
        _persist_tweets(run_id, state)              # commit dedup: today's set is now "seen"
        _update_trends(run_id, state, app_settings, index_themes=True)
        _finish_run_row(run_id, state, RunStatus.success, None)
        logger.info("=== Delivered run %s: %d tweets, %d themes -> %s (emailed=%s, telegram=%s) ===",
                    run_id, len(state.filtered_tweets), len(state.themes), state.digest_path,
                    state.emailed, state.telegram_sent)
    except Exception as e:
        state.error = str(e)
        _finish_run_row(run_id, state, RunStatus.failed, str(e))
        logger.exception("Delivery run %s failed", run_id)
        raise
    return state


def _latest_failed_run_id() -> int | None:
    from sqlmodel import select
    with get_session() as session:
        row = session.exec(
            select(DigestRunRow).where(DigestRunRow.status == RunStatus.failed)
            .order_by(DigestRunRow.id.desc())
        ).first()
        return row.id if row else None


def resume(run_id: int | None = None) -> DigestRun | None:
    """Resume a failed run from its furthest saved snapshot — no re-scraping.

    Loads the most advanced snapshot on disk for the run and continues the pipeline from the
    next stage, reusing the existing DigestRun row. Returns None if there's nothing to resume.
    """
    if run_id is None:
        run_id = _latest_failed_run_id()
        if run_id is None:
            logger.info("No failed run to resume.")
            return None

    loaded = load_latest_snapshot(settings.data_dir, run_id)
    if loaded is None:
        logger.warning("No snapshot found for run %s; cannot resume.", run_id)
        return None
    state, stage = loaded
    state.run_id = run_id

    app_settings = _load_app_settings()
    ctx = AgentContext(config=settings, app_settings=app_settings, logger=logger)
    _mark_running(run_id)
    logger.info("=== Resuming run %s from '%s' (%d tweets recovered) ===",
                run_id, stage, len(state.filtered_tweets) or len(state.raw_tweets))

    try:
        _archive_raw(run_id, state)   # idempotent; covers a run that failed before archiving
        _run_stages(ctx, state, start_after=stage)
        _persist_tweets(run_id, state)
        _update_trends(run_id, state, app_settings)
        _finish_run_row(run_id, state, RunStatus.success, None)
        logger.info("=== Resumed run %s succeeded: %d tweets, %d themes -> %s ===",
                    run_id, len(state.filtered_tweets), len(state.themes), state.digest_path)
    except Exception as e:
        state.error = str(e)
        _finish_run_row(run_id, state, RunStatus.failed, str(e))
        logger.exception("Resumed run %s failed", run_id)
        raise

    return state


def _load_replay_state(source_run_id: int) -> DigestRun | None:
    """Load a source run's post-filter/threaded tweets for replay (pre-clustering).

    Returns a DigestRun whose `filtered_tweets` are ready to re-cluster + summarize + report,
    or None if no reusable snapshot exists. Threading is NOT re-run (we reuse the captured set).
    """
    import json
    from pathlib import Path

    run_dir = Path(settings.data_dir) / "runs" / str(source_run_id)
    for label in ("2a_threaded", "2_filtered"):
        snap = run_dir / f"{label}.json"
        if snap.is_file():
            return DigestRun.from_dict(json.loads(snap.read_text()))
    return None


def is_replayable(source_run_id: int) -> bool:
    return _load_replay_state(source_run_id) is not None


def replay(source_run_id: int, overrides: dict | None = None, deliver: bool = False) -> DigestRun:
    """Re-run a past run's captured tweets through clustering+summarize+report — no re-scrape.

    Creates a NEW run linked to the source via source_run_id. `overrides` may set digest_style,
    clustering_method, ollama_model, similarity_threshold, and topics_override. Delivery
    (email/Telegram) is off unless `deliver` is True.
    """
    src = _load_replay_state(source_run_id)
    if src is None:
        raise RuntimeError(
            f"Run {source_run_id} has no reusable snapshot to replay (it may be too old or "
            "its snapshots were deleted)."
        )

    overrides = dict(overrides or {})
    overrides["deliver"] = deliver
    app_settings = _load_app_settings(overrides)
    ctx = AgentContext(config=settings, app_settings=app_settings, logger=logger)

    run_id = _create_run_row()
    _record_params(run_id, app_settings, source_run_id=source_run_id)
    state = DigestRun(run_id=run_id)
    state.filtered_tweets = src.filtered_tweets
    logger.info("=== Replay run %s (from #%s): %d tweets, style=%s ===",
                run_id, source_run_id, len(state.filtered_tweets), app_settings.digest_style.value)

    try:
        # Start after threading: re-cluster (if applicable) + summarize + report only.
        _run_stages(ctx, state, start_after="2a_threaded")
        # No _persist_tweets: the source run already recorded these for cross-day dedup.
        _finish_run_row(run_id, state, RunStatus.success, None)
        logger.info("=== Replay run %s succeeded: %d themes -> %s ===",
                    run_id, len(state.themes), state.digest_path)
    except Exception as e:
        state.error = str(e)
        _finish_run_row(run_id, state, RunStatus.failed, str(e))
        logger.exception("Replay run %s failed", run_id)
        raise

    return state


def _mark_running(run_id: int) -> None:
    with get_session() as session:
        row = session.get(DigestRunRow, run_id)
        if row:
            row.status = RunStatus.running
            row.error = None
            session.add(row)
            session.commit()


def delete_run(run_id: int) -> dict | None:
    """Delete a run and everything tied to it: the DB row, its digested + raw tweets, the
    rendered digest HTML, and the snapshot directory. Returns a summary, or None if not found.

    Refuses to delete a run that is currently in progress.
    """
    import shutil
    from pathlib import Path

    from sqlmodel import delete, select

    with get_session() as session:
        row = session.get(DigestRunRow, run_id)
        if row is None:
            logger.warning("delete_run: run %s not found", run_id)
            return None
        if row.status == RunStatus.running:
            raise RuntimeError(f"Run {run_id} is in progress; cannot delete it.")
        digest_path = row.digest_path

        tweets = len(session.exec(select(Tweet.id).where(Tweet.run_id == run_id)).all())
        raw = len(session.exec(select(RawTweet.id).where(RawTweet.run_id == run_id)).all())
        session.exec(delete(Tweet).where(Tweet.run_id == run_id))
        session.exec(delete(RawTweet).where(RawTweet.run_id == run_id))
        session.delete(row)
        session.commit()

    # Remove on-disk artifacts (only within our data dir, to be safe).
    data_dir = Path(settings.data_dir).resolve()
    digest_deleted = False
    if digest_path:
        p = Path(digest_path).resolve()
        if data_dir in p.parents and p.is_file():
            p.unlink()
            digest_deleted = True

    snap_dir = data_dir / "runs" / str(run_id)
    snapshots_deleted = snap_dir.is_dir()
    if snapshots_deleted:
        shutil.rmtree(snap_dir)

    summary = {"run_id": run_id, "tweets": tweets, "raw_tweets": raw,
               "digest_deleted": digest_deleted, "snapshots_deleted": snapshots_deleted}
    logger.info("Deleted run %s: %s", run_id, summary)
    return summary


def reset_runs(backup: bool = True) -> dict:
    """Delete ALL run data, keeping configuration. Returns a summary dict.

    Removes the run-history + tweet + raw-archive + trends tables and the on-disk snapshot dirs
    and rendered digests. KEEPS settings, excluded accounts, per-account limits/VIPs, and topics.
    Refuses while a run is in progress. Optionally backs up the SQLite file first (timestamped).
    """
    import shutil
    from pathlib import Path

    from sqlmodel import delete, func, select

    from db.models import DailyStat, MetaDigest, ThemeHistory, TopicCluster

    if is_running():
        raise RuntimeError("A run is in progress; cannot reset now.")

    # Back up the DB file first so the wipe is reversible.
    backup_path = None
    if backup:
        src = Path(settings.db_path)
        if src.is_file():
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_path = src.with_suffix(src.suffix + f".{stamp}.bak")
            shutil.copy2(src, backup_path)

    # Children before parents so foreign keys never dangle.
    ordered = (ThemeHistory, TopicCluster, MetaDigest, DailyStat, Tweet, RawTweet, DigestRunRow)
    labels = {ThemeHistory: "theme_history", TopicCluster: "theme_clusters",
              MetaDigest: "meta_digests", DailyStat: "daily_stats", Tweet: "tweets",
              RawTweet: "raw_tweets", DigestRunRow: "digest_runs"}
    counts: dict[str, int] = {}
    with get_session() as session:
        for model in ordered:
            counts[labels[model]] = session.exec(select(func.count()).select_from(model)).one()
            session.exec(delete(model))
        session.commit()

    # On-disk artifacts — only ever within our own data dir.
    data_dir = Path(settings.data_dir).resolve()
    removed_dirs = removed_files = 0
    runs_dir = data_dir / "runs"
    if runs_dir.is_dir():
        for p in runs_dir.iterdir():
            if p.is_dir():
                shutil.rmtree(p)
                removed_dirs += 1
            elif p.is_file():
                p.unlink()
                removed_files += 1
    digests_dir = data_dir / "digests"
    if digests_dir.is_dir():
        for p in digests_dir.iterdir():
            if p.is_file():
                p.unlink()
                removed_files += 1

    summary = {"tables": counts, "snapshot_dirs": removed_dirs, "files": removed_files,
               "backup": str(backup_path) if backup_path else None}
    logger.info("Reset runs: %s", summary)
    return summary
