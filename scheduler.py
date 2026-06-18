"""APScheduler wiring — runs the digest pipeline daily at the configured time.

Started from the FastAPI lifespan. Jobs run in APScheduler's own thread pool (plain
threads, no asyncio loop), which is exactly what the sync-Playwright collector needs.
The schedule is read from the DB and can be changed live from the UI via reschedule().
"""
from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

import pipeline
from db.session import get_session, get_settings

logger = logging.getLogger("scheduler")
JOB_ID = "daily_digest"
JOB_ID_META = "weekly_meta_digest"
JOB_ID_COLLECT = "collection"
JOB_ID_PROCESS = "process_draft"

# Random ±wobble (seconds) added to each interval fire so the scrape cadence looks
# human rather than a robotic on-the-hour beacon (e.g. 08:14, 10:21 instead of 08:00).
_JITTER_SECONDS = 20 * 60

_scheduler: BackgroundScheduler | None = None


def _interval_trigger(hours: int, tz: ZoneInfo) -> IntervalTrigger:
    """Interval anchored to local midnight, with jitter.

    A bare IntervalTrigger fires at now+interval and resets that clock on every restart,
    so frequent restarts (or a sleeping host) can starve the job. Anchoring start_date to
    midnight pins the fire grid (e.g. every 2 h => 00:00, 02:00, …) so restarts only skip
    to the next slot instead of pushing a full interval out. Jitter then wobbles each fire.
    """
    anchor = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    return IntervalTrigger(hours=hours, start_date=anchor, jitter=_JITTER_SECONDS, timezone=tz)


def _resolve_tz(name: str) -> ZoneInfo:
    """Look up an IANA timezone, falling back to UTC if it's missing/invalid."""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning("Unknown timezone %r; falling back to UTC", name)
        return ZoneInfo("UTC")


def _job() -> None:
    """Legacy all-in-one: scrape + summarize + deliver (used when collection is disabled)."""
    logger.info("Scheduled digest firing (inline)")
    pipeline.run_guarded()


def _deliver_job() -> None:
    logger.info("Scheduled delivery firing (from archive)")
    pipeline.deliver_guarded()


def _collect_job() -> None:
    logger.info("Scheduled collection firing")
    pipeline.collect_guarded()


def _process_job() -> None:
    logger.info("Scheduled draft refresh firing")
    pipeline.refresh_draft_guarded()


def _meta_job() -> None:
    logger.info("Weekly meta-digest firing")
    from agents import analytics
    analytics.generate_meta_digest_guarded()


def start() -> None:
    global _scheduler
    if _scheduler is not None:
        return
    _scheduler = BackgroundScheduler(daemon=True)
    _scheduler.start()
    reschedule()


def reschedule() -> None:
    """(Re)apply all schedules from the DB. Safe to call after a settings change.

    Three independent schedules:
      - collection (every N hrs): scrape new tweets into the archive,
      - processing (every M hrs): refresh the live draft digest (no delivery),
      - delivery (daily, evening): finalize + send. When collection is enabled it reads from
        the archive; otherwise it falls back to the legacy inline scrape-and-send.
    """
    if _scheduler is None:
        return
    with get_session() as s:
        cfg = get_settings(s)
        enabled, hour, minute = cfg.schedule_enabled, cfg.schedule_hour, cfg.schedule_minute
        collect_on, collect_hrs = cfg.collection_enabled, max(1, cfg.collection_interval_hours)
        process_on, process_hrs = cfg.process_enabled, max(1, cfg.process_interval_hours)
        tz = _resolve_tz(cfg.timezone)

    for job_id in (JOB_ID, JOB_ID_META, JOB_ID_COLLECT, JOB_ID_PROCESS):
        if _scheduler.get_job(job_id):
            _scheduler.remove_job(job_id)

    if collect_on:
        _scheduler.add_job(_collect_job, _interval_trigger(collect_hrs, tz),
                           id=JOB_ID_COLLECT, replace_existing=True, misfire_grace_time=3600)
        logger.info("Collection scheduled every %d h (anchored, jittered)", collect_hrs)
    if process_on:
        _scheduler.add_job(_process_job, _interval_trigger(process_hrs, tz),
                           id=JOB_ID_PROCESS, replace_existing=True, misfire_grace_time=3600)
        logger.info("Draft refresh scheduled every %d h (anchored, jittered)", process_hrs)

    if enabled:
        # When collection runs on its own schedule, the evening job just delivers the archive;
        # otherwise it does the legacy all-in-one scrape + summarize + send.
        delivery = _deliver_job if collect_on else _job
        _scheduler.add_job(delivery, CronTrigger(hour=hour, minute=minute, timezone=tz),
                           id=JOB_ID, replace_existing=True, misfire_grace_time=3600)
        # Weekly retrospective, Sundays at the same time of day as the delivery.
        _scheduler.add_job(_meta_job, CronTrigger(day_of_week="sun", hour=hour, minute=minute, timezone=tz),
                           id=JOB_ID_META, replace_existing=True, misfire_grace_time=3600)
        logger.info("%s scheduled at %02d:%02d %s; weekly meta-digest on Sundays",
                    "Delivery" if collect_on else "Daily digest", hour, minute, tz)
    else:
        logger.info("Daily delivery schedule disabled")


def next_run_times() -> dict[str, datetime | None]:
    """Next scheduled fire (tz-aware) per job id, for display in the UI. Missing/disabled → None."""
    out: dict[str, datetime | None] = {}
    for jid in (JOB_ID_COLLECT, JOB_ID_PROCESS, JOB_ID):
        job = _scheduler.get_job(jid) if _scheduler else None
        out[jid] = job.next_run_time if job else None
    return out


def shutdown() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
