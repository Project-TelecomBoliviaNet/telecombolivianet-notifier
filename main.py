"""
Punto de entrada del Worker de TelecomBoliviaNet.

Responsabilidades separadas (SRP):
  _setup_logging()   — configura structlog una sola vez
  _parse_args()      — parsea argumentos CLI
  _validate_env()    — valida variables de entorno al arrancar
  _connect_db()      — crea el pool de conexiones con manejo de error
  _run_manual_mode() — ejecuta el ReminderJob para una fecha concreta y termina
  _register_signals()— registra SIGTERM/SIGINT para graceful shutdown
  _run_workers()     — arranca los dos loops concurrentemente
  main()             — orquesta las funciones anteriores (≤ 20 líneas)
"""
import argparse
import asyncio
import logging
import os
import signal
import sys
from datetime import date
from pathlib import Path
from typing import Optional

import structlog
from dotenv import load_dotenv
from app.domain.db_types import Pool

from app.db.connection import close_pool, get_pool
from app.jobs.outbox_worker import run_outbox_worker
from app.jobs.reminder_job import run_reminder_scheduler, run_once
from app.services.health import start_health_server, record_sent, record_failed


# ── Constantes de configuración ───────────────────────────────────────────────

_REQUIRED_VARS      = ["DATABASE_URL"]
_REQUIRED_PROD_VARS = ["WHATSAPP_TOKEN", "WHATSAPP_PHONE_NUMBER_ID"]
_OPTIONAL_VARS      = {
    "POLL_INTERVAL_SECONDS": "30",
    "TIMEZONE":              "America/La_Paz",
    "LOG_LEVEL":             "INFO",
    "HEALTH_PORT":           "8080",
    "WHATSAPP_API_VERSION":  "v19.0",
}


# ── Funciones de arranque ─────────────────────────────────────────────────────

def _setup_logging() -> None:
    """Configura logging estándar + structlog con timestamps ISO y nivel configurable."""
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(format="%(message)s", stream=sys.stdout,
                        level=getattr(logging, level, logging.INFO))
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
    )


def _parse_args() -> argparse.Namespace:
    """Parsea argumentos de línea de comandos."""
    parser = argparse.ArgumentParser(
        description="TelecomBoliviaNet — Worker de Notificaciones")
    parser.add_argument(
        "--reminder-date", metavar="YYYY-MM-DD",
        help="Ejecuta el ReminderJob para esa fecha y termina (US-05).")
    return parser.parse_args()


def _validate_env(log: structlog.BoundLogger) -> None:
    """
    Valida variables de entorno al arrancar (US-10).
    Termina con sys.exit(1) si falta DATABASE_URL o si algún valor numérico
    configurable es inválido (evita bugs silenciosos en producción).
    """
    missing = [v for v in _REQUIRED_VARS if not os.environ.get(v)]
    if missing:
        log.error("startup.missing_required_env", missing=missing)
        sys.exit(1)

    missing_prod = [v for v in _REQUIRED_PROD_VARS if not os.environ.get(v)]
    if missing_prod:
        log.warning("startup.missing_prod_env", missing=missing_prod,
                    note="Worker will run in STUB mode")

    _validate_numeric_config(log)

    defaults = {k: v for k, v in _OPTIONAL_VARS.items() if not os.environ.get(k)}
    if defaults:
        log.info("startup.using_defaults", defaults=defaults)


def _validate_numeric_config(log: structlog.BoundLogger) -> None:
    """
    Valida que las variables numéricas opcionales tengan valores coherentes.
    Un MAX_ATTEMPTS=0 causaría que todos los mensajes fallen definitivamente
    en el primer intento. Un BATCH_SIZE=0 causaría un bucle vacío infinito.
    """
    errors = []

    batch = _parse_positive_int("BATCH_SIZE", default=50)
    if batch is None or batch <= 0:
        errors.append("BATCH_SIZE debe ser un entero positivo (ej: 50)")

    attempts = _parse_positive_int("MAX_ATTEMPTS", default=4)
    if attempts is None or attempts <= 0:
        errors.append("MAX_ATTEMPTS debe ser un entero positivo (ej: 4)")

    backoff_raw = os.environ.get("BACKOFF_SECONDS", "60,300,1800,7200")
    try:
        backoff = [int(x.strip()) for x in backoff_raw.split(",")]
        if not backoff or any(b <= 0 for b in backoff):
            errors.append("BACKOFF_SECONDS debe ser una lista de enteros positivos (ej: 60,300,1800,7200)")
    except ValueError:
        errors.append(f"BACKOFF_SECONDS valor inválido: '{backoff_raw}'")

    if errors:
        for err in errors:
            log.error("startup.invalid_config", error=err)
        sys.exit(1)


def _parse_positive_int(env_var: str, default: int) -> int | None:
    """Parsea una variable de entorno como entero. Retorna None si no es válido."""
    raw = os.environ.get(env_var, str(default))
    try:
        return int(raw)
    except ValueError:
        return None


async def _run_migrations(pool: Pool, log: structlog.BoundLogger) -> None:
    """Aplica scripts SQL del directorio migrations/ en orden. Son idempotentes (IF NOT EXISTS)."""
    migrations_dir = Path(__file__).parent / "migrations"
    if not migrations_dir.exists():
        return
    sql_files = sorted(migrations_dir.glob("*.sql"))
    if not sql_files:
        return
    async with pool.acquire() as conn:
        for f in sql_files:
            await conn.execute(f.read_text())
            log.info("worker.migration_applied", file=f.name)


async def _connect_db(log: structlog.BoundLogger) -> Pool:
    """Crea el pool de BD. Termina con sys.exit(1) si falla la conexión."""
    try:
        pool = await get_pool()
        log.info("worker.db_connected")
        return pool
    except Exception as exc:
        log.error("worker.db_connection_failed", error=str(exc))
        sys.exit(1)


async def _run_manual_mode(pool: Pool, reminder_date: str, log: structlog.BoundLogger) -> None:
    """Ejecuta el ReminderJob para una fecha concreta y cierra el pool (US-05)."""
    try:
        target = date.fromisoformat(reminder_date)
    except ValueError:
        log.error("startup.invalid_date", value=reminder_date,
                  expected_format="YYYY-MM-DD")
        sys.exit(1)

    log.info("worker.manual_reminder_run", target_date=str(target))
    await run_once(pool, target_date=target)
    await close_pool()
    log.info("worker.manual_reminder_done")


def _register_signals(loop: asyncio.AbstractEventLoop, shutdown_event: asyncio.Event, log: structlog.BoundLogger) -> None:
    """Registra SIGTERM y SIGINT para activar el graceful shutdown (US-08)."""
    def _on_signal(sig_name: str) -> None:
        if not shutdown_event.is_set():
            log.info("worker.shutdown_requested", signal=sig_name)
            shutdown_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _on_signal, sig.name)


async def _run_workers(pool: Pool, loop: asyncio.AbstractEventLoop, shutdown_event: asyncio.Event) -> None:
    """Arranca OutboxWorker y ReminderScheduler concurrentemente."""
    start_health_server(pool, loop)
    try:
        await asyncio.gather(
            run_outbox_worker(
                shutdown_event=shutdown_event,
                on_sent=record_sent,
                on_failed=record_failed,
            ),
            run_reminder_scheduler(shutdown_event=shutdown_event),
        )
    finally:
        await close_pool()


# ── Punto de entrada ──────────────────────────────────────────────────────────

async def main() -> None:
    load_dotenv()
    _setup_logging()
    log = structlog.get_logger("main")
    args = _parse_args()
    _validate_env(log)

    pool = await _connect_db(log)
    await _run_migrations(pool, log)

    if args.reminder_date:
        await _run_manual_mode(pool, args.reminder_date, log)
        return

    loop           = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()
    _register_signals(loop, shutdown_event, log)
    log.info("worker.starting",
             poll_interval=os.environ.get("POLL_INTERVAL_SECONDS", "30"))
    await _run_workers(pool, loop, shutdown_event)


if __name__ == "__main__":
    asyncio.run(main())
