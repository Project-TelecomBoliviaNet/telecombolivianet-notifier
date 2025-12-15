"""
ReminderPersistence — all DB operations for the reminder job.

Responsibilities:
  - Job-run state: get_last_run_day, persist_job_run
  - Distributed lock: try_acquire_lock, release_lock
  - Invoice processing: process_invoice_reminder, process_reminder_type
"""
import json
from datetime import date, timedelta
from typing import Optional

import structlog

from app.db import queries as Q
from app.domain.db_types import Pool, Connection
from app.jobs.reminder_context import build_reminder_context

log = structlog.get_logger(__name__)


# ── JOB STATE ─────────────────────────────────────────────────────────────────

async def get_last_run_day(pool: Pool) -> Optional[date]:
    """Date of the last successful job run. None if never ran."""
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(Q.FETCH_LAST_JOB_RUN, "REMINDER_JOB")
            if row and row["last_run"]:
                val = row["last_run"]
                return val.date() if hasattr(val, "date") else val
    except Exception as exc:
        log.warning("reminder_job.last_run_check_failed", error=str(exc))
    return None


async def persist_job_run(pool: Pool, run_date: date) -> None:
    """Records that the job ran successfully on run_date."""
    try:
        async with pool.acquire() as conn:
            await conn.execute(Q.UPSERT_JOB_RUN, "REMINDER_JOB", run_date)
    except Exception as exc:
        log.warning("reminder_job.persist_run_failed", error=str(exc))


# ── DISTRIBUTED LOCK ──────────────────────────────────────────────────────────

async def try_acquire_lock(conn: Connection) -> bool:
    """
    Acquires pg_try_advisory_lock (atomic PostgreSQL operation).
    True → this worker executes. False → another instance holds the lock.
    """
    return bool(await conn.fetchval(Q.ADVISORY_LOCK_REMINDER))


async def release_lock(conn: Connection) -> None:
    """Releases the advisory lock at the end of run_once()."""
    await conn.fetchval(Q.ADVISORY_UNLOCK_REMINDER)


# ── INVOICE PROCESSING ────────────────────────────────────────────────────────

async def process_invoice_reminder(
    conn: Connection, inv: dict, tipo: str,
    cfg: dict, today_bo: date, sys_config: dict,
) -> bool:
    """
    Queues one invoice reminder in NotifOutbox using an atomic INSERT.
    Returns True if inserted, False if already existed (ON CONFLICT DO NOTHING).
    """
    target_date    = today_bo + timedelta(days=cfg["dias_antes"])
    pending_months = await conn.fetchval(Q.COUNT_PENDING_MONTHS, inv["cliente_id"])
    contexto       = await build_reminder_context(
        conn, inv, target_date, pending_months or 1, sys_config
    )

    inserted_id = await conn.fetchval(
        Q.INSERT_OUTBOX_SAFE,
        tipo, inv["cliente_id"], inv["phone_number"],
        str(cfg["delay_segundos"]), json.dumps(contexto), inv["invoice_id"],
    )
    if inserted_id is None:
        log.debug("reminder_job.duplicate", tipo=tipo,
                  invoice_id=str(inv["invoice_id"]))
        return False
    return True


async def process_reminder_type(
    pool: Pool, tipo: str, cfg: dict, today_bo: date, sys_config: dict,
) -> None:
    """Processes all invoices for one reminder type for today."""
    target_invoice_date = today_bo + timedelta(days=cfg["dias_antes"])
    log.info("reminder_job.processing", tipo=tipo,
             target_date=str(target_invoice_date))

    generated, skipped = 0, 0
    async with pool.acquire() as conn:
        invoices = await conn.fetch(Q.FETCH_INVOICES_DUE_IN_DAYS, target_invoice_date)
        for inv in invoices:
            if await process_invoice_reminder(conn, dict(inv), tipo, cfg, today_bo, sys_config):
                generated += 1
            else:
                skipped += 1

    log.info("reminder_job.done", tipo=tipo,
             generated=generated, skipped_duplicates=skipped)
