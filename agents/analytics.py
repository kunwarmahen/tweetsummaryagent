"""Trends & analytics over the collected tweet archive.

Most trends are built from `raw_tweets` (the append-only, pre-filter archive — every captured
tweet with created_at + engagement, surviving even failed runs):

  - recompute_daily_stats: materializes the per-UTC-date series into `daily_stats` (rebuilt
    wholesale, idempotent). Cheap at this volume; called after each real run.
  - daily_series / account_leaderboard / top_tweets: read helpers for the trends page. The
    leaderboard and top-tweets are LIVE windowed queries (naturally date-ranged, cheap), so
    only the daily series is materialized.

Theme continuity and the weekly meta-digest live in later additions to this module.
"""
from __future__ import annotations

import json
import logging
import math
import threading
from datetime import datetime, timedelta, timezone

from sqlalchemy import case
from sqlmodel import Session, delete, func, select

from config import settings
from db.models import DailyStat, MetaDigest, RawTweet, ThemeHistory, TopicCluster
from db.session import get_session

logger = logging.getLogger("analytics")

# Cosine threshold for folding a theme into the cluster of its nearest PRIOR theme (single
# linkage). nomic title embeddings carry a high baseline cosine (median ~0.46 on real data), so
# this sits above the p75 of unrelated pairs to catch only genuine same-topic recurrences.
THEME_SIMILARITY = 0.62


# --------------------------------------------------------------- daily_stats (materialized)
def recompute_daily_stats(session: Session | None = None) -> int:
    """Rebuild the `daily_stats` table from `raw_tweets`. Returns the number of day-rows.

    Wipe-and-rebuild keeps it correct with no drift; O(rows), trivial at personal volume.
    Tweets with no parseable created_at are skipped (can't be placed on a day).
    """
    own = session is None
    session = session or get_session()
    try:
        day = func.date(RawTweet.created_at)
        rows = session.exec(
            select(
                day,
                func.count(RawTweet.id),
                func.count(func.distinct(RawTweet.handle)),
                func.coalesce(func.sum(RawTweet.likes), 0),
                func.coalesce(func.sum(RawTweet.retweets), 0),
                func.coalesce(func.sum(case((RawTweet.is_retweet == True, 1), else_=0)), 0),       # noqa: E712
                func.coalesce(func.sum(case((RawTweet.is_self_reply == True, 1), else_=0)), 0),    # noqa: E712
            )
            .where(RawTweet.created_at.is_not(None))
            .group_by(day)
        ).all()

        session.exec(delete(DailyStat))
        for date, n, accts, likes, rts, retw, selfr in rows:
            session.add(DailyStat(
                date=date, tweet_count=n, account_count=accts,
                total_likes=likes, total_retweets=rts, engagement=likes + rts,
                retweet_count=retw, self_reply_count=selfr,
            ))
        session.commit()
        logger.info("daily_stats rebuilt: %d day(s)", len(rows))
        return len(rows)
    finally:
        if own:
            session.close()


# ----------------------------------------------------------- weekly LLM meta-digest
_meta_lock = threading.Lock()


def is_meta_running() -> bool:
    return _meta_lock.locked()


def latest_meta_digest(session: Session) -> MetaDigest | None:
    return session.exec(select(MetaDigest).order_by(MetaDigest.id.desc())).first()


def _meta_context(session: Session, days: int) -> tuple[list[str], int]:
    """Build the prompt lines (one per recurring topic) and a count of themes in the window."""
    trending = trending_themes(session, days=days, limit=20)
    if not trending:
        return [], 0
    # A representative (latest) summary per cluster, for color in the narrative.
    summaries: dict[int, str] = {}
    since = (datetime.now(timezone.utc).date() - timedelta(days=days - 1)).isoformat()
    for cid, summary in session.exec(
        select(ThemeHistory.cluster_id, ThemeHistory.summary)
        .where(ThemeHistory.run_date >= since)
        .order_by(ThemeHistory.id)
    ).all():
        if summary:
            summaries[cid] = summary           # last write wins -> most recent
    lines = []
    for t in trending:
        s = (summaries.get(t["cluster_id"], "") or "").replace("\n", " ").strip()[:300]
        lines.append(
            f"- {t['label']} (seen {t['days']} day(s), {t['appearances']} mention(s), "
            f"{t['engagement']:,} engagement){': ' + s if s else ''}"
        )
    return lines, len(trending)


def _llm_meta_narrative(lines: list[str], days: int, model: str) -> str | None:
    import ollama

    system = ("You are the editor of a personal newsletter. You write a short, engaging "
              "retrospective over the reader's X/Twitter feed. Be concrete and neutral; do not "
              "invent facts beyond the topics provided.")
    prompt = (
        f"Over the past {days} days, these recurring topics dominated the reader's feed "
        f"(with how many days each appeared, total mentions, and engagement):\n\n"
        + "\n".join(lines)
        + "\n\nWrite a brief markdown retrospective titled '## This week in your feed'. "
        "Use 3-5 short paragraphs: lead with the biggest storyline, note what's rising or "
        "recurring, and call out anything notable. Keep it tight — no bullet lists."
    )
    client = ollama.Client(host=settings.ollama_url)
    resp = client.chat(
        model=model,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        options={"temperature": 0.4},
    )
    text = (resp["message"]["content"] or "").strip()
    return text or None


def generate_meta_digest(days: int = 7, session: Session | None = None) -> MetaDigest | None:
    """Generate + store a 'this week in your feed' narrative from recent themes (needs Ollama).

    Returns the new MetaDigest, or None if there are no themes in the window or the LLM yields
    nothing. The model is the configured digest model.
    """
    from db.session import get_settings

    own = session is None
    session = session or get_session()
    try:
        lines, n = _meta_context(session, days)
        if not lines:
            logger.info("Meta-digest skipped: no themes in the last %d days", days)
            return None
        model = get_settings(session).ollama_model
        narrative = _llm_meta_narrative(lines, days, model)
        if not narrative:
            logger.warning("Meta-digest skipped: model returned no text")
            return None
        today = datetime.now(timezone.utc).date()
        row = MetaDigest(
            period_start=(today - timedelta(days=days - 1)).isoformat(),
            period_end=today.isoformat(), narrative=narrative, model=model,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        logger.info("Meta-digest generated over %d topic(s) with %s", n, model)
        return row
    finally:
        if own:
            session.close()


def generate_meta_digest_guarded(days: int = 7) -> MetaDigest | None:
    """Generate a meta-digest unless one is already generating (non-blocking)."""
    if not _meta_lock.acquire(blocking=False):
        logger.info("Meta-digest already generating; skipping.")
        return None
    try:
        return generate_meta_digest(days=days)
    except Exception:
        logger.exception("Meta-digest generation failed")
        return None
    finally:
        _meta_lock.release()


# ------------------------------------------------------------------------- read helpers (UI)
def daily_series(session: Session, days: int = 30) -> list[DailyStat]:
    """The last `days` of materialized daily stats, oldest first (for time-series charts)."""
    since = (datetime.now(timezone.utc).date() - timedelta(days=days - 1)).isoformat()
    return list(session.exec(
        select(DailyStat).where(DailyStat.date >= since).order_by(DailyStat.date)
    ).all())


def _window_start(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


def account_leaderboard(session: Session, days: int = 7, limit: int = 10) -> list[dict]:
    """Most active / most engaged handles over the window (live query on raw_tweets)."""
    since = _window_start(days)
    rows = session.exec(
        select(
            RawTweet.handle,
            func.max(RawTweet.author_name),
            func.count(RawTweet.id),
            func.coalesce(func.sum(RawTweet.likes + RawTweet.retweets), 0),
        )
        .where(RawTweet.created_at >= since)
        .group_by(RawTweet.handle)
        .order_by(func.count(RawTweet.id).desc())
        .limit(limit)
    ).all()
    return [
        {"handle": h, "author_name": name or h, "tweets": n, "engagement": eng}
        for h, name, n, eng in rows
    ]


def top_tweets(session: Session, days: int = 7, limit: int = 10) -> list[RawTweet]:
    """Highest-engagement tweets over the window (live query on raw_tweets)."""
    since = _window_start(days)
    return list(session.exec(
        select(RawTweet)
        .where(RawTweet.created_at >= since)
        .order_by((RawTweet.likes + RawTweet.retweets).desc())
        .limit(limit)
    ).all())


# ----------------------------------------------------------- theme continuity (embedding-based)
def _normalize(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def _dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def _embed_theme(client, model: str, title: str, summary: str) -> list[float] | None:
    text = (f"{title}. {summary}").strip()[:1000]
    if not text:
        return None
    resp = client.embeddings(model=model, prompt=text)
    return resp.get("embedding") or None


def index_run_themes(session: Session, run_id: int | None, run_date: str,
                     themes: list, engagement_by_id: dict[str, int],
                     model: str | None = None, threshold: float = THEME_SIMILARITY) -> int:
    """Record a run's themes into theme_history and fold each into a persistent TopicCluster.

    `themes` are state.ThemeCluster-like objects (title, summary, tweet_ids). Each title is
    embedded and matched (single-linkage) against the nearest theme from PRIOR runs; on a match
    above `threshold` it joins that theme's cluster, otherwise it starts a new one. Matching only
    against prior runs keeps a run's own distinct themes from collapsing into each other. Returns
    the number of themes indexed. Best-effort: needs Ollama reachable for embeddings.
    """
    import ollama

    titled = [t for t in themes if getattr(t, "title", "").strip()]
    if not titled:
        return 0
    model = model or settings.ollama_model  # caller passes the embedding model explicitly
    client = ollama.Client(host=settings.ollama_url)

    # Prior themes (id'd by cluster) to match against — excludes this run's own themes.
    prior: list[tuple[list[float], int]] = []
    for emb_json, cid in session.exec(
        select(ThemeHistory.embedding_json, ThemeHistory.cluster_id)
        .where(ThemeHistory.cluster_id.is_not(None))
    ).all():
        vec = json.loads(emb_json or "[]")
        if vec:
            prior.append((vec, cid))

    indexed = 0
    for th in titled:
        emb = _embed_theme(client, model, th.title, getattr(th, "summary", "") or "")
        if emb is None:
            continue
        unit = _normalize(emb)

        best_cid, best_sim = None, threshold
        for vec, cid in prior:
            if len(vec) != len(unit):
                continue
            sim = _dot(unit, vec)
            if sim >= best_sim:
                best_cid, best_sim = cid, sim

        if best_cid is None:
            cluster = TopicCluster(label=th.title, first_seen=run_date,
                                   last_seen=run_date, appearance_count=1)
            session.add(cluster)
            session.flush()                # assign id for the FK below
            best_cid = cluster.id
        else:
            cluster = session.get(TopicCluster, best_cid)
            cluster.label = th.title       # latest title names the cluster
            cluster.last_seen = run_date
            cluster.appearance_count += 1
            session.add(cluster)

        eng = sum(engagement_by_id.get(tid, 0) for tid in getattr(th, "tweet_ids", []))
        session.add(ThemeHistory(
            run_id=run_id, run_date=run_date, title=th.title,
            summary=(getattr(th, "summary", "") or "")[:2000],
            member_count=len(getattr(th, "tweet_ids", [])), engagement=eng,
            embedding_json=json.dumps(unit), cluster_id=best_cid,
        ))
        indexed += 1

    session.commit()
    logger.info("Indexed %d theme(s) for run %s", indexed, run_id)
    return indexed


def trending_themes(session: Session, days: int = 7, limit: int = 8) -> list[dict]:
    """Topics recurring across the window, ranked by distinct days seen then total engagement."""
    since = (datetime.now(timezone.utc).date() - timedelta(days=days - 1)).isoformat()
    rows = session.exec(
        select(
            ThemeHistory.cluster_id,
            func.count(func.distinct(ThemeHistory.run_date)),
            func.count(ThemeHistory.id),
            func.coalesce(func.sum(ThemeHistory.engagement), 0),
            func.max(ThemeHistory.run_date),
        )
        .where(ThemeHistory.run_date >= since)
        .group_by(ThemeHistory.cluster_id)
    ).all()

    labels = {c.id: c.label for c in session.exec(select(TopicCluster)).all()}
    out = [
        {"cluster_id": cid, "label": labels.get(cid, "(unknown)"),
         "days": days_seen, "appearances": appearances, "engagement": eng, "last_seen": last}
        for cid, days_seen, appearances, eng, last in rows
    ]
    out.sort(key=lambda r: (r["days"], r["engagement"]), reverse=True)
    return out[:limit]


def rebuild_theme_history(session: Session | None = None) -> int:
    """Wipe and rebuild theme_clusters + theme_history from original runs' summarized snapshots.

    Re-indexes runs oldest-first so cluster centroids accrue in chronological order. Skips
    replays (source_run_id set). Returns the number of themes indexed. Needs Ollama reachable.
    """
    import json as _json
    from pathlib import Path

    from db.models import DigestRun as DigestRunRow
    from db.session import get_settings
    from state import DigestRun as StateRun

    own = session is None
    session = session or get_session()
    try:
        embed_model = get_settings(session).embedding_model
        session.exec(delete(ThemeHistory))
        session.exec(delete(TopicCluster))
        session.commit()

        runs = session.exec(
            select(DigestRunRow)
            .where(DigestRunRow.source_run_id.is_(None))
            .order_by(DigestRunRow.id)
        ).all()
        total = 0
        for run in runs:
            snap = Path(settings.data_dir) / "runs" / str(run.id) / "3_summarized.json"
            if not snap.is_file():
                continue
            st = StateRun.from_dict(_json.loads(snap.read_text()))
            if not st.themes:
                continue
            run_date = (run.started_at or datetime.now(timezone.utc)).date().isoformat()
            engagement_by_id = {t.tweet_id: (t.likes + t.retweets) for t in st.filtered_tweets}
            total += index_run_themes(session, run.id, run_date, st.themes,
                                      engagement_by_id, model=embed_model)
        logger.info("theme_history rebuilt: %d theme(s) across %d run(s)", total, len(runs))
        return total
    finally:
        if own:
            session.close()
