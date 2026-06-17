"""APScheduler wiring — runs the digest pipeline daily at the configured time.

Started from the FastAPI lifespan. Jobs run in APScheduler's own thread pool (plain
threads, no asyncio loop), which is exactly what the sync-Playwright collector needs.
The schedule is read from the DB and can be changed live from the UI via reschedule().
"""
from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

import pipeline
from db.session import get_session, get_settings

logger = logging.getLogger("scheduler")
JOB_ID = "daily_digest"

_scheduler: BackgroundScheduler | None = None


def _job() -> None:
    logger.info("Scheduled digest firing")
    pipeline.run_guarded()


def start() -> None:
    global _scheduler
    if _scheduler is not None:
        return
    _scheduler = BackgroundScheduler(daemon=True)
    _scheduler.start()
    reschedule()


def reschedule() -> None:
    """(Re)apply the schedule from the DB. Safe to call after settings change."""
    if _scheduler is None:
        return
    with get_session() as s:
        cfg = get_settings(s)
        enabled, hour, minute = cfg.schedule_enabled, cfg.schedule_hour, cfg.schedule_minute

    if _scheduler.get_job(JOB_ID):
        _scheduler.remove_job(JOB_ID)

    if enabled:
        _scheduler.add_job(_job, CronTrigger(hour=hour, minute=minute),
                           id=JOB_ID, replace_existing=True, misfire_grace_time=3600)
        logger.info("Daily digest scheduled at %02d:%02d", hour, minute)
    else:
        logger.info("Daily schedule disabled")


def shutdown() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
