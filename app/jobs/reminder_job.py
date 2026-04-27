"""
ReminderJob — recordatorios de pago R1/R2/R3.

Organización por responsabilidad (SRP):
  Sección DOMAIN    — lógica pura de dominio (periodo, contexto)
  Sección PERSIST   — estado del job en BD (última ejecución)
  Sección LOCK      — distributed lock con pg_try_advisory_lock
  Sección INVOICE   — procesar una factura individual
  Sección JOB       — run_once: iteración por tipo de recordatorio
  Sección SCHEDULER — loop diario con jitter y graceful shutdown
"""
import asyncio
import json
import random
from datetime import date, timedelta, datetime
from typing import Optional

import pytz
import structlog

from app.db.connection import get_pool
from app.db import queries as Q
from app.domain.db_types import Pool, Connection

log = structlog.get_logger(__name__)

BOLIVIA_TZ     = pytz.timezone("America/La_Paz")
# REMINDER_TYPES ya no es una constante hardcodeada.
# Los tipos activos se leen de NotifConfigs con FETCH_ACTIVE_REMINDER_TYPES,
# por lo que agregar RECORDATORIO_R4 solo requiere un INSERT en BD.

_MONTH_NAMES = [
    "", "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
    "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre",
]


# ── DOMAIN ────────────────────────────────────────────────────────────────────

def build_period_label(year: int, month: int, invoice_type: str) -> str:
    """'Mayo 2025' para mensualidades, 'Instalación' para instalaciones."""
    if invoice_type == "Instalacion":
        return "Instalación"
    return f"{_MONTH_NAMES[month]} {year}"


def build_reminder_context(inv: dict, target_date: date, pending_months: int) -> dict:
    """Diccionario listo para sustituir variables en la plantilla del mensaje."""
    return {
        "nombre":            inv["nombre"] or "",
        "monto":             f"{inv['amount']:.2f}",
        "fecha_vencimiento": target_date.strftime("%d/%m/%Y"),
        "periodo":           build_period_label(inv["year"], inv["month"], inv["type"]),
        "meses_pendientes":  str(pending_months or 1),
        "plan":              inv["plan_name"] or "internet",
    }


# ── PERSIST ───────────────────────────────────────────────────────────────────

async def get_last_run_day(pool: Pool) -> Optional[date]:
    """Fecha de la última ejecución exitosa del job. None si nunca corrió."""
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
    """Registra que el job corrió exitosamente en run_date."""
    try:
        async with pool.acquire() as conn:
            await conn.execute(Q.UPSERT_JOB_RUN, "REMINDER_JOB", run_date)
    except Exception as exc:
        log.warning("reminder_job.persist_run_failed", error=str(exc))


# ── LOCK ──────────────────────────────────────────────────────────────────────

async def try_acquire_lock(conn: Connection) -> bool:
    """
    Adquiere pg_try_advisory_lock (operación atómica en PostgreSQL).
    True → este worker ejecuta. False → otra instancia ya tiene el lock.
    """
    return bool(await conn.fetchval(Q.ADVISORY_LOCK_REMINDER))


async def release_lock(conn: Connection) -> None:
    """Libera el advisory lock al finalizar run_once()."""
    await conn.fetchval(Q.ADVISORY_UNLOCK_REMINDER)


# ── INVOICE ───────────────────────────────────────────────────────────────────

async def process_invoice_reminder(conn: Connection, inv: dict, tipo: str,
                                    cfg: dict, today_bo: date) -> bool:
    """
    Verifica duplicados y encola el recordatorio de una factura en NotifOutbox.
    True si se insertó, False si ya existía un recordatorio (duplicate).
    """
    target_date = today_bo + timedelta(days=cfg["dias_antes"])

    if await conn.fetchval(Q.EXISTS_REMINDER_LOG,
                           inv["cliente_id"], tipo, inv["invoice_id"]):
        log.debug("reminder_job.duplicate", tipo=tipo,
                  invoice_id=str(inv["invoice_id"]))
        return False

    pending_months = await conn.fetchval(Q.COUNT_PENDING_MONTHS, inv["cliente_id"])
    contexto = build_reminder_context(inv, target_date, pending_months or 1)

    await conn.execute(Q.INSERT_OUTBOX,
        tipo, inv["cliente_id"], inv["phone_number"],
        str(cfg["delay_segundos"]), json.dumps(contexto), inv["invoice_id"])
    return True


async def _process_reminder_type(pool: Pool, tipo: str, cfg: dict, today_bo: date) -> None:
    """Procesa todas las facturas de un tipo de recordatorio para hoy."""
    target_invoice_date = today_bo + timedelta(days=cfg["dias_antes"])
    log.info("reminder_job.processing", tipo=tipo,
             target_date=str(target_invoice_date))

    async with pool.acquire() as conn:
        invoices = await conn.fetch(Q.FETCH_INVOICES_DUE_IN_DAYS, target_invoice_date)

    generated, skipped = 0, 0
    for inv in invoices:
        async with pool.acquire() as conn:
            if await process_invoice_reminder(conn, dict(inv), tipo, cfg, today_bo):
                generated += 1
            else:
                skipped += 1

    log.info("reminder_job.done", tipo=tipo,
             generated=generated, skipped_duplicates=skipped)


# ── JOB ───────────────────────────────────────────────────────────────────────

async def run_once(pool: Pool, target_date: Optional[date] = None) -> None:
    """
    Ejecuta los recordatorios para target_date (hoy si no se especifica).
    Llamable desde CLI (US-05) o desde el scheduler.

    Los tipos de recordatorio se leen dinámicamente de NotifConfigs
    (REMINDER_TYPES ya no está hardcodeado), por lo que agregar R4 o
    cambiar la nomenclatura no requiere modificar código Python.
    """
    today_bo = target_date or datetime.now(BOLIVIA_TZ).date()
    log.info("reminder_job.running", date=str(today_bo))

    async with pool.acquire() as conn:
        configs    = await conn.fetch(Q.FETCH_ALL_CONFIGS)
        config_map = {r["tipo"]: dict(r) for r in configs}
        # Tipos activos leídos de BD — no hardcodeados
        reminder_rows = await conn.fetch(Q.FETCH_ACTIVE_REMINDER_TYPES)
        reminder_types = [r["tipo"] for r in reminder_rows]

    if not reminder_types:
        log.warning("reminder_job.no_active_types")
        return

    for tipo in reminder_types:
        cfg = config_map.get(tipo)
        if not cfg or not cfg["activo"] or not cfg["dias_antes"]:
            log.debug("reminder_job.skip", tipo=tipo, reason="disabled or no dias_antes")
            continue
        await _process_reminder_type(pool, tipo, cfg, today_bo)


# ── SCHEDULER ─────────────────────────────────────────────────────────────────

async def _try_run_job(pool: Pool, today: date) -> bool:
    """
    Intenta ejecutar run_once() adquiriendo el distributed lock primero.
    Retorna True si el job se ejecutó, False si el lock estaba ocupado.
    El caller solo debe actualizar last_run_day cuando retorna True.
    """
    async with pool.acquire() as lock_conn:
        if not await try_acquire_lock(lock_conn):
            log.debug("reminder_scheduler.lock_busy")
            return False
        try:
            await run_once(pool)
            await persist_job_run(pool, today)
            return True
        finally:
            await release_lock(lock_conn)


async def _scheduler_sleep(shutdown_event: Optional[asyncio.Event], seconds: float) -> None:
    """Duerme seconds segundos o hasta que shutdown_event se active."""
    try:
        if shutdown_event:
            await asyncio.wait_for(shutdown_event.wait(), timeout=seconds)
        else:
            await asyncio.sleep(seconds)
    except asyncio.TimeoutError:
        pass


async def run_reminder_scheduler(shutdown_event: Optional[asyncio.Event] = None) -> None:
    """Loop diario a las 08:01 Bolivia con lock distribuido, persistencia y jitter."""
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
        await _scheduler_sleep(shutdown_event, max(40, 60 + random.uniform(-10, 10)))
