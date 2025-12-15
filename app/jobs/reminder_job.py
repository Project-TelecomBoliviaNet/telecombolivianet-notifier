"""
ReminderJob — daily payment-reminder scheduler (R1/R2/R3).

This module owns the loop and Bolivia-timezone window logic only.
Pure domain logic lives in sub-modules:
  reminder_context     — template variable builders
  reminder_persistence — DB state, distributed lock, invoice processing
  _time_window         — Bolivia timezone helpers (mockable in tests)

Responsibilities kept here:
  LOOP      — run_reminder_scheduler (daily 08:01 Bolivia with jitter)
  JOB       — run_once (callable from CLI/US-05 or the scheduler)
  LOCK      — _try_run_job (acquire lock, run, release)
"""
import asyncio
import random
from datetime import date, datetime
from typing import Optional

import structlog

from app.db.connection import get_pool
from app.db import queries as Q
from app.domain.db_types import Pool
from app.jobs._time_window import BOLIVIA_TZ
from app.jobs.reminder_persistence import (
    get_last_run_day,
    persist_job_run,
    try_acquire_lock,
    release_lock,
    process_reminder_type,
)

log = structlog.get_logger(__name__)

_REMINDER_LOCK_KEY   = 20250101
_JOB_MAX_DURATION_MS = 900_000  # 15 min — prevents zombie connections


# ── JOB ───────────────────────────────────────────────────────────────────────

async def run_once(pool: Pool, target_date: Optional[date] = None) -> None:
    """
    Runs all active reminder types for target_date (today if omitted).
    Callable from CLI (US-05) or from the scheduler.

    Reminder types are read dynamically from NotifConfigs — adding R4 or
    renaming types only requires a DB INSERT, no Python changes.
    """
    today_bo = target_date or datetime.now(BOLIVIA_TZ).date()
    log.info("reminder_job.running", date=str(today_bo))

    async with pool.acquire() as conn:
        configs       = await conn.fetch(Q.FETCH_ALL_CONFIGS)
        config_map    = {r["tipo"]: dict(r) for r in configs}
        reminder_rows = await conn.fetch(Q.FETCH_ACTIVE_REMINDER_TYPES)
        reminder_types = [r["tipo"] for r in reminder_rows]
        sys_config_rows = await conn.fetch(Q.FETCH_SYSTEM_CONFIG_BULK)
        sys_config    = {r["key"]: r["value"] for r in sys_config_rows}

    if not reminder_types:
        log.warning("reminder_job.no_active_types")
        return

    for tipo in reminder_types:
        cfg = config_map.get(tipo)
        if not cfg or not cfg["activo"] or not cfg["dias_antes"]:
            log.debug("reminder_job.skip", tipo=tipo, reason="disabled or no dias_antes")
            continue
        await process_reminder_type(pool, tipo, cfg, today_bo, sys_config)


# ── SCHEDULER ─────────────────────────────────────────────────────────────────

async def _try_run_job(pool: Pool, today: date) -> bool:
    """
    Tries to execute run_once() after acquiring the distributed lock.
    statement_timeout ensures the connection is not left open indefinitely
    if the process dies — PostgreSQL releases the advisory lock on close.
    """
    async with pool.acquire() as lock_conn:
        await lock_conn.execute(
            f"SET statement_timeout = '{_JOB_MAX_DURATION_MS}'"
        )
        if not await try_acquire_lock(lock_conn):
            log.debug("reminder_scheduler.lock_busy")
            return False
        try:
            await run_once(pool)
            await persist_job_run(pool, today)
            return True
        finally:
            await release_lock(lock_conn)


async def _scheduler_sleep(
    shutdown_event: Optional[asyncio.Event], seconds: float
) -> None:
    """Sleeps for `seconds` or until shutdown_event is set."""
    try:
        if shutdown_event:
            await asyncio.wait_for(shutdown_event.wait(), timeout=seconds)
        else:
            await asyncio.sleep(seconds)
    except asyncio.TimeoutError:
        pass


async def run_reminder_scheduler(
    shutdown_event: Optional[asyncio.Event] = None,
) -> None:
    """Daily loop at 08:01 Bolivia with distributed lock, persistence and jitter."""
    pool         = await get_pool()
    last_run_day = await get_last_run_day(pool)
    log.info("reminder_scheduler.started",
             last_run_day=str(last_run_day) if last_run_day else "never")

    while not (shutdown_event and shutdown_event.is_set()):
        try:
            now_bo    = datetime.now(BOLIVIA_TZ)
            today     = now_bo.date()
            in_window = now_bo.hour == 8 and 1 <= now_bo.minute <= 10
            if in_window and last_run_day != today:
                ran = await _try_run_job(pool, today)
                if ran:
                    last_run_day = today
        except Exception as exc:
            log.error("reminder_scheduler.error", error=str(exc))
        await _scheduler_sleep(
            shutdown_event, max(40, 60 + random.uniform(-10, 10))
        )
