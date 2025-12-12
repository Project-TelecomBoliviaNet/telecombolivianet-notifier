"""
OutboxWorker — consume notif_outbox y envía mensajes WhatsApp.

This module is the orchestrator. Pure logic lives in sub-modules:
  outbox_batch_fetcher  — atomic batch claiming, finalize, backoff
  context_enricher      — build template variables from DB
  message_renderer      — config validation, template rendering → OutboxRecord
  _time_window          — Bolivia timezone helpers (mockable in tests)

Responsibilities kept here:
  CONFIG  — get_batch_size, poll_interval
  SEND    — _send_and_finalize (needs WhatsAppClient)
  RECORD  — process_record (4-line orchestrator)
  LOOP    — _process_batch, _interruptible_sleep, run_outbox_worker
"""
import asyncio
import os
from typing import Callable, Optional

import structlog

from app.db import queries as Q
from app.db.connection import get_pool
from app.domain.db_types import Pool
from app.domain.records import OutboxRecord, RawOutboxRow
from app.services.notification_channel import NotificationChannel
from app.services.whatsapp import MetaApiUnavailableError
from app.jobs.outbox_batch_fetcher import (
    get_max_attempts,
    fetch_and_claim_batch,
    finalize_sent,
    return_to_pool_rate_limited,
    apply_backoff_or_fail,
)
from app.jobs.message_renderer import fetch_sys_config, load_and_render

log = structlog.get_logger(__name__)


# ── CONFIG ────────────────────────────────────────────────────────────────────

def get_batch_size() -> int:
    """Records per cycle. Configurable via BATCH_SIZE (default 50)."""
    return int(os.environ.get("BATCH_SIZE", "50"))


# ── SEND ──────────────────────────────────────────────────────────────────────

async def _send_and_finalize(
    pool: Pool,
    channel: NotificationChannel,
    rec: OutboxRecord,
    on_sent:   Optional[Callable[[], None]],
    on_failed: Optional[Callable[[], None]],
) -> None:
    """
    Sends the message and persists the result.
    Handles rate-limit, Meta API unavailability, and generic errors.
    """
    from app.services.whatsapp import RateLimitError
    try:
        if rec.is_template and hasattr(channel, "send_template"):
            await channel.send_template(          # type: ignore[attr-defined]
                rec.phone,
                rec.meta_template_name,
                rec.meta_language_code or "es",
                list(rec.meta_params or ()),
            )
        else:
            await channel.send(rec.phone, rec.mensaje)

        async with pool.acquire() as conn:
            await finalize_sent(conn, rec)
        if on_sent:
            on_sent()
        log.info("worker.sent", tipo=rec.tipo, phone=rec.phone_log,
                 outbox_id=rec.outbox_id, is_template=rec.is_template)

    except RateLimitError as exc:
        await return_to_pool_rate_limited(pool, rec.outbox_id, exc.retry_after)

    except MetaApiUnavailableError as exc:
        # Circuit-open: infrastructure failure — don't consume attempts, retry in 60s.
        async with pool.acquire() as conn:
            await conn.execute(Q.RETURN_TO_POOL, rec.outbox_id, "60")
        log.warning("worker.circuit_open_retry", tipo=rec.tipo, outbox_id=rec.outbox_id,
                    reason=str(exc))

    except Exception as exc:
        log.warning("worker.send_failed", tipo=rec.tipo, outbox_id=rec.outbox_id,
                    intento=rec.intentos + 1, error=str(exc))
        definitive = await apply_backoff_or_fail(pool, rec, str(exc))
        if definitive and on_failed:
            on_failed()


# ── RECORD ────────────────────────────────────────────────────────────────────

async def process_record(
    pool: Pool,
    channel: NotificationChannel,
    row: RawOutboxRow,
    sys_config: dict,
    on_sent:   Optional[Callable[[], None]] = None,
    on_failed: Optional[Callable[[], None]] = None,
) -> None:
    """Full lifecycle for one record: load → render → send."""
    rec = await load_and_render(pool, row, sys_config)
    if rec is None:
        return
    await _send_and_finalize(pool, channel, rec, on_sent, on_failed)


# ── LOOP ──────────────────────────────────────────────────────────────────────

async def _process_batch(
    pool: Pool,
    channel: NotificationChannel,
    batch_size: int,
    on_sent:   Optional[Callable[[], None]],
    on_failed: Optional[Callable[[], None]],
) -> None:
    """
    Claims a batch and processes all records in parallel.
    On SIGTERM (CancelledError), releases unclaimed records so the next
    worker picks them up — no message stays in limbo.
    """
    rows = await fetch_and_claim_batch(pool, batch_size)
    if not rows:
        return
    log.info("outbox_worker.batch", count=len(rows))

    row_ids    = [r.id for r in rows]
    sys_config = await fetch_sys_config(pool)
    tasks      = [process_record(pool, channel, r, sys_config, on_sent=on_sent, on_failed=on_failed)
                  for r in rows]
    try:
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                log.error("outbox_worker.task_exception",
                          record_index=i, error=str(result))
    except asyncio.CancelledError:
        async with pool.acquire() as conn:
            await conn.execute(Q.RELEASE_BATCH, row_ids)
        log.warning("outbox_worker.batch_cancelled_released", count=len(row_ids))
        raise


async def _interruptible_sleep(
    seconds: float, event: Optional[asyncio.Event]
) -> None:
    """Sleeps for `seconds` or until `event` is set."""
    try:
        if event:
            await asyncio.wait_for(event.wait(), timeout=seconds)
        else:
            await asyncio.sleep(seconds)
    except asyncio.TimeoutError:
        pass


async def run_outbox_worker(
    shutdown_event: Optional[asyncio.Event] = None,
    on_sent:        Optional[Callable[[], None]] = None,
    on_failed:      Optional[Callable[[], None]] = None,
) -> None:
    """
    Main loop. Runs until shutdown_event is set (SIGTERM).
    Instantiates WhatsAppClient internally and exposes it as NotificationChannel.
    """
    from app.services.whatsapp import WhatsAppClient
    poll_interval = int(os.environ.get("POLL_INTERVAL_SECONDS", "30"))
    batch_size    = get_batch_size()
    channel: NotificationChannel = WhatsAppClient()
    pool          = await get_pool()

    log.info("outbox_worker.started", poll_interval=poll_interval,
             batch_size=batch_size, max_attempts=get_max_attempts())

    try:
        while not (shutdown_event and shutdown_event.is_set()):
            try:
                await _process_batch(pool, channel, batch_size, on_sent, on_failed)
            except Exception as exc:
                log.error("outbox_worker.loop_error", error=str(exc))
            await _interruptible_sleep(poll_interval, shutdown_event)
    finally:
        await channel.close()
        log.info("outbox_worker.stopped")
