"""
OutboxBatchFetcher — atomic DB operations for the outbox worker.

Responsibilities:
  - Claim a batch of pending records (FOR UPDATE SKIP LOCKED).
  - Persist final states: ENVIADO, FALLIDO.
  - Return records to the pool (rate-limit, window wait, cancelled batch).
  - Backoff scheduling.
"""
import os

import structlog

from app.db import queries as Q
from app.domain.db_types import Pool, Connection
from app.domain.records import OutboxRecord, RawOutboxRow
from app.jobs._time_window import seconds_until_next_window

log = structlog.get_logger(__name__)


# ── CONFIG ────────────────────────────────────────────────────────────────────

def get_max_attempts() -> int:
    """Maximum retries before permanent FALLIDO. MAX_ATTEMPTS (default 4)."""
    return int(os.environ.get("MAX_ATTEMPTS", "4"))


def get_backoff_seconds() -> list[int]:
    """Backoff delays per attempt. BACKOFF_SECONDS (default 60,300,1800,7200)."""
    raw = os.environ.get("BACKOFF_SECONDS", "60,300,1800,7200")
    return [int(x.strip()) for x in raw.split(",")]


# ── BATCH ─────────────────────────────────────────────────────────────────────

async def fetch_and_claim_batch(pool: Pool, batch_size: int) -> list[RawOutboxRow]:
    """
    Claims up to batch_size pending records and marks Publicado=TRUE
    in a single atomic transaction (FOR UPDATE SKIP LOCKED).
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            records = await conn.fetch(Q.FETCH_PENDING_BATCH, batch_size)
            if records:
                await conn.execute(Q.CLAIM_BATCH, [r["id"] for r in records])
    return [RawOutboxRow.from_db_row(dict(r)) for r in records] if records else []


# ── FINALIZE ──────────────────────────────────────────────────────────────────

async def finalize_sent(conn: Connection, rec: OutboxRecord) -> None:
    """Persists ENVIADO state and inserts the success log entry."""
    await conn.execute(Q.FINALIZE_OK, rec.outbox_id, rec.intentos + 1)
    await conn.execute(Q.INSERT_LOG,
        rec.outbox_id, rec.cliente_id, rec.tipo, rec.phone, rec.mensaje,
        "ENVIADO", rec.intentos + 1, None)


async def finalize_type_disabled(conn: Connection, row: RawOutboxRow) -> None:
    """Persists FALLIDO: notification type is disabled in notif_config."""
    await conn.execute(Q.FINALIZE_FAILED, row.id, row.intentos)
    await conn.execute(Q.INSERT_LOG,
        row.id, row.cliente_id, row.tipo, row.phone_number, "",
        "FALLIDO", row.intentos, "Tipo desactivado en notif_config")


async def finalize_no_template(conn: Connection, row: RawOutboxRow) -> None:
    """Persists FALLIDO: no active template found for this notification type."""
    await conn.execute(Q.FINALIZE_FAILED, row.id, row.intentos + 1)
    await conn.execute(Q.INSERT_LOG,
        row.id, row.cliente_id, row.tipo, row.phone_number, "",
        "FALLIDO", row.intentos + 1, "Sin plantilla activa para el tipo")


async def return_to_pool_until_window(
    conn: Connection, row: RawOutboxRow, hora_inicio
) -> None:
    """Returns record to outbox with ProximoIntento set to next window start."""
    secs = seconds_until_next_window(hora_inicio)
    await conn.execute(Q.RETURN_TO_POOL, row.id, str(secs))
    log.debug("worker.outside_window", tipo=row.tipo, outbox_id=row.id,
              next_window_in_seconds=secs)


async def return_to_pool_rate_limited(
    pool: Pool, outbox_id: str, retry_after: int
) -> None:
    """Returns record to outbox with ProximoIntento=Retry-After (no attempt consumed)."""
    async with pool.acquire() as conn:
        await conn.execute(Q.RETURN_TO_POOL, outbox_id, str(retry_after))
    log.info("worker.rate_limited_retry", outbox_id=outbox_id,
             retry_after_seconds=retry_after)


async def apply_backoff_or_fail(
    pool: Pool, rec: OutboxRecord, error_msg: str
) -> bool:
    """
    Logs the failure and decides between backoff retry or permanent FALLIDO.
    Returns True if permanently failed, False if a retry is scheduled.
    """
    new_intentos = rec.intentos + 1
    async with pool.acquire() as conn:
        await conn.execute(Q.INSERT_LOG,
            rec.outbox_id, rec.cliente_id, rec.tipo, rec.phone, "",
            "FALLIDO", new_intentos, error_msg[:500])

        if new_intentos >= get_max_attempts():
            await conn.execute(Q.FINALIZE_FAILED, rec.outbox_id, new_intentos)
            log.error("worker.permanently_failed", tipo=rec.tipo,
                      outbox_id=rec.outbox_id, attempts=new_intentos)
            return True

        backoff = get_backoff_seconds()
        delay   = backoff[min(new_intentos - 1, len(backoff) - 1)]
        await conn.execute(Q.RETRY_WITH_BACKOFF, rec.outbox_id, new_intentos, str(delay))
        log.info("worker.scheduled_retry", outbox_id=rec.outbox_id,
                 next_in_seconds=delay)
        return False
