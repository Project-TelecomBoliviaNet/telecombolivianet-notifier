"""
Servidor HTTP de observabilidad: /healthz y /metrics.

Responsabilidades separadas por clase (SRP):
  MetricsCollector — almacena contadores sent/failed en memoria
  DbProbe          — ejecuta SELECT 1 y COUNT pendientes contra el pool
  _RequestHandler  — parsea path y escribe respuestas HTTP (routing puro)
  HealthServer     — une las piezas y arranca el thread del servidor

DIP: el pool y el loop se inyectan en el constructor, no se usan globales.
OCP: agregar un nuevo endpoint (p.ej. /ready) no requiere tocar DbProbe
     ni MetricsCollector — solo _RequestHandler._route().
"""
import asyncio
import json
import os
import threading
from typing import Any, Coroutine
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

import structlog
from app.domain.db_types import Pool

log = structlog.get_logger(__name__)


# ── Contadores de métricas ────────────────────────────────────────────────────

class MetricsCollector:
    """
    Contadores en memoria para el proceso actual.
    Thread-safe: record_sent/record_failed se llaman desde el loop asyncio,
    mientras que sent/failed se leen desde el thread del servidor HTTP.
    """

    def __init__(self) -> None:
        self._sent   = 0
        self._failed = 0
        self._lock   = threading.Lock()

    def record_sent(self) -> None:
        with self._lock:
            self._sent += 1

    def record_failed(self) -> None:
        with self._lock:
            self._failed += 1

    @property
    def sent(self) -> int:
        with self._lock:
            return self._sent

    @property
    def failed(self) -> int:
        with self._lock:
            return self._failed


# Instancia de proceso — única fuente de verdad para este worker
_metrics = MetricsCollector()


def record_sent() -> None:
    """API pública: incrementa el contador de mensajes enviados."""
    _metrics.record_sent()


def record_failed() -> None:
    """API pública: incrementa el contador de fallos definitivos."""
    _metrics.record_failed()


# ── Sonda de base de datos ────────────────────────────────────────────────────

class DbProbe:
    """
    Ejecuta comprobaciones asíncronas contra el pool de BD.
    Responsabilidad única: saber si la BD está accesible y cuántos
    mensajes están pendientes.
    """

    def __init__(self, pool: Pool, loop: asyncio.AbstractEventLoop) -> None:
        self._pool = pool
        self._loop = loop

    def _run(self, coro: Coroutine, timeout: float = 5.0) -> Any:
        """Ejecuta una corutina desde un thread no-asyncio sin get_event_loop()."""
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

    def ping(self) -> bool:
        """Retorna True si el pool responde a SELECT 1, False en caso contrario."""
        try:
            self._run(self._async_ping())
            return True
        except Exception as exc:
            log.warning("health.db_ping_failed", error=str(exc))
            return False

    def count_pending(self) -> int | None:
        """Retorna el total de mensajes pendientes en NotifOutbox, o None si falla."""
        try:
            return self._run(self._async_count_pending())
        except Exception as exc:
            log.warning("health.count_pending_failed", error=str(exc))
            return None

    async def _async_ping(self) -> None:
        async with self._pool.acquire() as conn:
            await conn.fetchval("SELECT 1")

    async def _async_count_pending(self) -> int:
        from app.db import queries as Q
        async with self._pool.acquire() as conn:
            return await conn.fetchval(Q.COUNT_OUTBOX_PENDING)


# ── Handler HTTP ──────────────────────────────────────────────────────────────

class _RequestHandler(BaseHTTPRequestHandler):
    """
    Parsea la petición HTTP y escribe la respuesta JSON.
    Responsabilidad única: routing y serialización.
    Toda la lógica de negocio vive en DbProbe y MetricsCollector.
    """

    def __init__(self, probe: DbProbe, *args, **kwargs) -> None:
        self._probe = probe
        super().__init__(*args, **kwargs)

    def log_message(self, format: str, *args: Any) -> None:
        pass  # silenciar logs de acceso HTTP en stdout

    def do_GET(self) -> None:
        routes = {
            "/healthz": self._healthz,
            "/metrics": self._metrics,
        }
        handler = routes.get(self.path)
        if handler:
            handler()
        else:
            self._write_json(404, {"error": "not found"})

    def _healthz(self) -> None:
        """GET /healthz — 200 si BD ok, 503 si no. No expone detalles del error."""
        if self._probe.ping():
            self._write_json(200, {"status": "ok", "db": "ok"})
        else:
            self._write_json(503, {"status": "degraded", "db": "error"})

    def _metrics(self) -> None:
        """GET /metrics — contadores en memoria + pendientes en BD."""
        self._write_json(200, {
            "worker_messages_sent_total":   _metrics.sent,
            "worker_messages_failed_total": _metrics.failed,
            "outbox_pending_total":         self._probe.count_pending(),
        })

    def _write_json(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


# ── Servidor ──────────────────────────────────────────────────────────────────

class HealthServer:
    """
    Arranca y gestiona el servidor HTTP en un thread daemon.
    Responsabilidad única: ciclo de vida del servidor.
    """

    def __init__(self, pool: Pool, loop: asyncio.AbstractEventLoop) -> None:
        self._probe = DbProbe(pool, loop)
        self._port  = int(os.environ.get("HEALTH_PORT", "8080"))

    def start(self) -> None:
        """Arranca el servidor en un thread daemon. No bloqueante."""
        probe = self._probe

        def factory(*args, **kwargs):
            return _RequestHandler(probe, *args, **kwargs)

        server = HTTPServer(("0.0.0.0", self._port), factory)
        Thread(target=server.serve_forever, daemon=True).start()
        log.info("health_server.started", port=self._port,
                 endpoints=["/healthz", "/metrics"])


def start_health_server(pool: Pool, loop: asyncio.AbstractEventLoop) -> None:
    """
    Función de conveniencia para arrancar el servidor desde main.py.
    Recibe el loop explícitamente para evitar asyncio.get_event_loop() en threads.
    """
    HealthServer(pool, loop).start()
