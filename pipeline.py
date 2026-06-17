"""Pipeline orchestrator — runs the agents over a shared DigestRun state.

Collector -> Filter -> Summarizer -> Reporter, snapshotting state after each stage and
recording the run (status, counts, digest path) in the database.

NOTE: the Collector uses Playwright's sync API, so callers inside an asyncio event loop
(FastAPI) must invoke this in a worker thread.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

from agents.base import AgentContext
from agents.clusterer import EmbeddingClusterer
from agents.collector import Collector
from agents.filter import Filter
from agents.reporter import Reporter
from agents.summarizer import Summarizer
from agents.threader import ThreadStitcher
from config import settings
from db.models import (ClusteringMethod, DigestRun as DigestRunRow, DigestStyle,
                       RawTweet, RunStatus, Tweet)
from db.session import get_session, get_settings
from state import DigestRun, load_latest_snapshot

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


def _load_app_settings():
    with get_session() as session:
        app_settings = get_settings(session)
        session.expunge(app_settings)   # detach but keep loaded values
    return app_settings


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
    state = DigestRun(run_id=run_id)
    logger.info("=== Digest run %s started at %s ===", run_id, datetime.now(timezone.utc).isoformat())

    try:
        Collector(ctx, max_accounts=max_accounts).run(state)
        state.snapshot(settings.data_dir, "1_collected")
        _archive_raw(run_id, state)
        _run_stages(ctx, state, start_after=None)

        _persist_tweets(run_id, state)
        _finish_run_row(run_id, state, RunStatus.success, None)
        logger.info("=== Digest run %s succeeded: %d tweets, %d themes -> %s ===",
                    run_id, len(state.filtered_tweets), len(state.themes), state.digest_path)
    except Exception as e:
        state.error = str(e)
        _finish_run_row(run_id, state, RunStatus.failed, str(e))
        logger.exception("Digest run %s failed", run_id)
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
        _finish_run_row(run_id, state, RunStatus.success, None)
        logger.info("=== Resumed run %s succeeded: %d tweets, %d themes -> %s ===",
                    run_id, len(state.filtered_tweets), len(state.themes), state.digest_path)
    except Exception as e:
        state.error = str(e)
        _finish_run_row(run_id, state, RunStatus.failed, str(e))
        logger.exception("Resumed run %s failed", run_id)
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
