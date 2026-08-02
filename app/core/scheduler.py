"""
Stage 15 (bonus): APScheduler wiring for the proactive daily digest.

--- Why APScheduler, not Celery ---

Stage 0 Section 15 lists the digest as the one job in this whole
project explicitly called out as "APScheduler (digest only)" — every
other candidate for a background queue (the Stage 4 product→vector
dual-write, Stage 9 Mesh calls) was deliberately kept synchronous
in-request because there's no fan-out, no retry-with-backoff needs
beyond what's already built, and no second worker process to deploy
and monitor. The digest is a single cron-scheduled function call, once
a day, in-process — APScheduler's `BackgroundScheduler` is exactly
that and nothing more. Reaching for Celery here would mean standing up
a message broker (Redis/RabbitMQ) and a separate worker process for a
job that runs once every 24 hours; that's infrastructure the spec
never asks for.

--- Why a `BackgroundScheduler`, not `AsyncIOScheduler` ---

The app is FastAPI/async, but the digest job itself (`run_daily_digest`)
is synchronous SQLAlchemy + synchronous Mesh HTTP calls end to end —
matching every other service module in this codebase (Stage 9's Mesh
client is sync `httpx`, not `httpx.AsyncClient`). `BackgroundScheduler`
runs jobs on its own thread pool, independent of the event loop, so a
slow digest run (many users, many Mesh calls) never blocks request
handling. Wiring `AsyncIOScheduler` in would only pay off if the job
function were itself async, which it isn't and doesn't need to be.

--- Lifecycle ---

`start_scheduler()` / `shutdown_scheduler()` are called from
`app/main.py`'s FastAPI startup/shutdown events, gated by
`settings.digest_enabled`. Each scheduled run opens its own DB session
via `SessionLocal` (never reuses a request-scoped session — there is
no request), and closes it when done regardless of outcome.
"""

from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler

from app.core.config import settings
from app.db.session import SessionLocal
from app.recommendations.digest import run_daily_digest

logger = logging.getLogger("chennai_labs.scheduler")

_scheduler: BackgroundScheduler | None = None


def _run_digest_job() -> None:
    db = SessionLocal()
    try:
        run_daily_digest(db)
    finally:
        db.close()


def start_scheduler() -> BackgroundScheduler:
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(
        _run_digest_job,
        trigger="cron",
        hour=settings.digest_hour_utc,
        minute=0,
        id="daily_digest",
        replace_existing=True,
    )
    scheduler.start()
    _scheduler = scheduler
    logger.info("scheduler_started", extra={"digest_hour_utc": settings.digest_hour_utc})
    return scheduler


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logger.info("scheduler_stopped")
