"""
OutboxWorker — consume notif_outbox y envía mensajes WhatsApp.

Organización por responsabilidad (SRP):
  CONFIG    — leer parámetros de entorno con validación
  TIME      — utilidades de ventana horaria (mockables en tests)
  BATCH     — toma atómica del outbox (FOR UPDATE SKIP LOCKED)
  FINALIZE  — escritura de estados finales en BD
  SEND      — envío y manejo de errores post-envío
  RECORD    — orquestador de un registro (config → plantilla → envío)
  LOOP      — ciclo principal del worker

Principios:
  SRP — cada función hace exactamente una cosa
  OCP — depende de NotificationChannel (Protocol), no de WhatsAppClient concreto
  DIP — WhatsAppClient inyectado como NotificationChannel; callbacks de métricas
        inyectados desde main.py (no hay import de health.py en esta capa)
"""
import asyncio
import os
from datetime import datetime, time as dt_time, timedelta
from typing import Callable, Optional

import pytz
import structlog

from app.db.connection import get_pool
from app.db import queries as Q
from app.domain.db_types import Pool, Connection
from app.domain.records import OutboxRecord, RawOutboxRow
from app.domain.types import TimeValue
from app.services.notification_channel import NotificationChannel
from app.services.template import render, parse_context
from app.utils.pii import mask_phone

log = structlog.get_logger(__name__)

BOLIVIA_TZ = pytz.timezone("America/La_Paz")


# ── CONFIG ────────────────────────────────────────────────────────────────────

def get_batch_size() -> int:
    """Registros por ciclo. Configurable con BATCH_SIZE (default 50)."""
    return int(os.environ.get("BATCH_SIZE", "50"))


def get_max_attempts() -> int:
    """Intentos máximos antes de FALLIDO definitivo. MAX_ATTEMPTS (default 4)."""
    return int(os.environ.get("MAX_ATTEMPTS", "4"))


def get_backoff_seconds() -> list[int]:
    """Tiempos de backoff por intento. BACKOFF_SECONDS (default 60,300,1800,7200)."""
    raw = os.environ.get("BACKOFF_SECONDS", "60,300,1800,7200")
    return [int(x.strip()) for x in raw.split(",")]


# ── TIME ──────────────────────────────────────────────────────────────────────

def now_bolivia() -> datetime:
    """Hora actual en Bolivia. Extraída para poder mockear en tests."""
    return datetime.now(BOLIVIA_TZ)


def is_within_window(hora_inicio: TimeValue, hora_fin: TimeValue) -> bool:
    """True si la hora actual de Bolivia cae dentro de [hora_inicio, hora_fin]."""
    now_time = now_bolivia().time().replace(second=0, microsecond=0)
    if hasattr(hora_inicio, "hour"):
        start = hora_inicio.replace(second=0, microsecond=0)  # type: ignore[union-attr]
        end   = hora_fin.replace(second=0, microsecond=0)    # type: ignore[union-attr]
    else:
        h, m  = str(hora_inicio)[:5].split(":")
        start = dt_time(int(h), int(m))
        h, m  = str(hora_fin)[:5].split(":")
        end   = dt_time(int(h), int(m))
    return start <= now_time <= end


def seconds_until_next_window(hora_inicio: TimeValue) -> int:
    """Segundos hasta el inicio de la próxima ventana horaria (hoy o mañana)."""
    now = now_bolivia()
    if hasattr(hora_inicio, "hour"):
        h, m = hora_inicio.hour, hora_inicio.minute   # type: ignore[union-attr]
    else:
        parts = str(hora_inicio)[:5].split(":")
        h, m  = int(parts[0]), int(parts[1])
    window = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if window <= now:
        window += timedelta(days=1)
    return max(1, int((window - now).total_seconds()))


# ── BATCH ─────────────────────────────────────────────────────────────────────

async def fetch_and_claim_batch(pool: Pool, batch_size: int) -> list[RawOutboxRow]:
    """
    Toma hasta batch_size registros pendientes y los marca Publicado=TRUE
    en una sola transacción atómica (FOR UPDATE SKIP LOCKED).
    Retorna una lista de RawOutboxRow tipados en lugar de dicts.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            records = await conn.fetch(Q.FETCH_PENDING_BATCH, batch_size)
            if records:
                await conn.execute(Q.CLAIM_BATCH, [r["id"] for r in records])
    return [RawOutboxRow.from_db_row(dict(r)) for r in records] if records else []


# ── FINALIZE ──────────────────────────────────────────────────────────────────

async def finalize_sent(conn: Connection, rec: OutboxRecord) -> None:
    """Persiste estado ENVIADO y registra el log de éxito."""
    await conn.execute(Q.FINALIZE_OK, rec.outbox_id, rec.intentos + 1)
    await conn.execute(Q.INSERT_LOG,
        rec.outbox_id, rec.cliente_id, rec.tipo, rec.phone, rec.mensaje,
        "ENVIADO", rec.intentos + 1, None)


async def finalize_type_disabled(conn: Connection, row: RawOutboxRow) -> None:
    """Persiste FALLIDO: el tipo de notificación está desactivado en config."""
    await conn.execute(Q.FINALIZE_FAILED, row.id, row.intentos)
    await conn.execute(Q.INSERT_LOG,
        row.id, row.cliente_id, row.tipo, row.phone_number, "",
        "FALLIDO", row.intentos, "Tipo desactivado en notif_config")


async def finalize_no_template(conn: Connection, row: RawOutboxRow) -> None:
    """Persiste FALLIDO: no existe plantilla activa para este tipo."""
    await conn.execute(Q.FINALIZE_FAILED, row.id, row.intentos + 1)
    await conn.execute(Q.INSERT_LOG,
        row.id, row.cliente_id, row.tipo, row.phone_number, "",
        "FALLIDO", row.intentos + 1, "Sin plantilla activa para el tipo")


async def return_to_pool_until_window(
    conn: Connection, row: RawOutboxRow, hora_inicio: TimeValue
) -> None:
    """Devuelve al outbox con ProximoIntento al inicio de la próxima ventana."""
    secs = seconds_until_next_window(hora_inicio)
    await conn.execute(Q.RETURN_TO_POOL, row.id, str(secs))
    log.debug("worker.outside_window", tipo=row.tipo, outbox_id=row.id,
              next_window_in_seconds=secs)


async def return_to_pool_rate_limited(
    pool: Pool, outbox_id: str, retry_after: int
) -> None:
    """Devuelve al outbox con ProximoIntento=Retry-After. No consume intento."""
    async with pool.acquire() as conn:
        await conn.execute(Q.RETURN_TO_POOL, outbox_id, str(retry_after))
    log.info("worker.rate_limited_retry", outbox_id=outbox_id,
             retry_after_seconds=retry_after)


async def apply_backoff_or_fail(
    pool: Pool, rec: OutboxRecord, error_msg: str
) -> bool:
    """
    Registra el fallo y decide entre reintentar (backoff) o FALLIDO definitivo.
    Retorna True si es definitivo, False si habrá reintento.
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


# ── SEND ──────────────────────────────────────────────────────────────────────

async def _send_and_finalize(
    pool: Pool,
    channel: NotificationChannel,
    rec: OutboxRecord,
    on_sent:   Optional[Callable[[], None]],
    on_failed: Optional[Callable[[], None]],
) -> None:
    """Realiza el envío y registra el resultado. Maneja rate limit y errores."""
    from app.services.whatsapp import RateLimitError
    try:
        await channel.send(rec.phone, rec.mensaje)
        async with pool.acquire() as conn:
            await finalize_sent(conn, rec)
        if on_sent:
            on_sent()
        log.info("worker.sent", tipo=rec.tipo, phone=rec.phone_log,
                 outbox_id=rec.outbox_id)

    except RateLimitError as exc:
        await return_to_pool_rate_limited(pool, rec.outbox_id, exc.retry_after)

    except Exception as exc:
        log.warning("worker.send_failed", tipo=rec.tipo, outbox_id=rec.outbox_id,
                    intento=rec.intentos + 1, error=str(exc))
        definitive = await apply_backoff_or_fail(pool, rec, str(exc))
        if definitive and on_failed:
            on_failed()


# ── RECORD ────────────────────────────────────────────────────────────────────

async def _validate_config(
    conn: Connection, row: RawOutboxRow
) -> Optional[dict]:
    """Carga config y verifica activa + ventana. None = descartar el registro."""
    config = await conn.fetchrow(Q.FETCH_CONFIG, row.tipo)
    if not config or not config["activo"]:
        log.warning("worker.type_disabled", tipo=row.tipo, outbox_id=row.id)
        await finalize_type_disabled(conn, row)
        return None
    if not config["inmediato"] and not is_within_window(
        config["hora_inicio"], config["hora_fin"]
    ):
        await return_to_pool_until_window(conn, row, config["hora_inicio"])
        return None
    return config


async def _render_message(
    conn: Connection, row: RawOutboxRow, contexto: dict
) -> Optional[str]:
    """Carga plantilla y renderiza. None = descartar el registro."""
    plantilla = await conn.fetchrow(Q.FETCH_PLANTILLA, row.tipo)
    if not plantilla:
        log.error("worker.no_template", tipo=row.tipo, outbox_id=row.id)
        await finalize_no_template(conn, row)
        return None
    mensaje, missing = render(plantilla["texto"], contexto)
    if missing:
        log.warning("worker.template_missing_vars", tipo=row.tipo,
                    outbox_id=row.id, missing_vars=missing)
    return mensaje


async def _load_and_render(pool: Pool, row: RawOutboxRow) -> Optional[OutboxRecord]:
    """
    Orquesta _validate_config → _render_message.
    Retorna un OutboxRecord tipado o None si el registro fue descartado.
    """
    contexto = parse_context(row.contexto_json or "{}")
    async with pool.acquire() as conn:
        if not await _validate_config(conn, row):
            return None
        mensaje = await _render_message(conn, row, contexto)
        if mensaje is None:
            return None

    return OutboxRecord(
        outbox_id=row.id,
        cliente_id=row.cliente_id,
        tipo=row.tipo,
        phone=row.phone_number,
        intentos=row.intentos,
        phone_log=mask_phone(row.phone_number),
        mensaje=mensaje,
    )


async def process_record(
    pool: Pool,
    channel: NotificationChannel,
    row: RawOutboxRow,
    on_sent:   Optional[Callable[[], None]] = None,
    on_failed: Optional[Callable[[], None]] = None,
) -> None:
    """
    Ciclo de vida completo de un registro: cargar → renderizar → enviar.
    Depende de NotificationChannel (no de WhatsAppClient concreto).
    """
    rec = await _load_and_render(pool, row)
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
    """Toma un lote y procesa todos los registros en paralelo."""
    rows = await fetch_and_claim_batch(pool, batch_size)
    if not rows:
        return
    log.info("outbox_worker.batch", count=len(rows))
    tasks   = [process_record(pool, channel, r, on_sent=on_sent, on_failed=on_failed)
               for r in rows]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for i, result in enumerate(results):
        if isinstance(result, Exception):
            log.error("outbox_worker.task_exception",
                      record_index=i, error=str(result))


async def _interruptible_sleep(
    seconds: float, event: Optional[asyncio.Event]
) -> None:
    """Duerme seconds segundos o hasta que event se active."""
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
    Loop principal. Corre hasta que shutdown_event se active (SIGTERM).
    Instancia WhatsAppClient internamente pero lo expone como NotificationChannel.
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
