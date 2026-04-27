"""
Shared test helpers. Import as: from tests.helpers import run, make_record, ...
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio
from unittest.mock import AsyncMock, MagicMock
from datetime import time, date


def run(coro):
    """Runs a coroutine synchronously. Replaces deprecated get_event_loop()."""
    return asyncio.run(coro)


def make_conn_mock():
    conn = AsyncMock()
    conn.execute = AsyncMock()
    return conn


def make_pool_from_conns(*conns):
    def _ctx(c):
        return AsyncMock(
            __aenter__=AsyncMock(return_value=c),
            __aexit__=AsyncMock(return_value=False),
        )
    it = iter([_ctx(c) for c in conns])
    pool = AsyncMock()
    pool.acquire = MagicMock(side_effect=lambda: next(it))
    return pool


def make_record(intentos=0, tipo="AVISO_CORTE",
                contexto_json='{"nombre":"Ana","monto":"100.00"}'):
    return {
        "id":            "outbox-001",
        "tipo":          tipo,
        "cliente_id":    "cliente-001",
        "phone_number":  "76543210",
        "intentos":      intentos,
        "enviar_desde":  None,
        "contexto_json": contexto_json,
        "referencia_id": None,
    }


def make_config(activo=True, inmediato=True,
                hora_inicio=None, hora_fin=None,
                delay_segundos=0, dias_antes=None):
    return {
        "tipo":           "AVISO_CORTE",
        "activo":         activo,
        "inmediato":      inmediato,
        "hora_inicio":    hora_inicio or time(8, 0),
        "hora_fin":       hora_fin    or time(20, 0),
        "delay_segundos": delay_segundos,
        "dias_antes":     dias_antes,
    }


def make_plantilla(texto="Hola {{nombre}}, debes Bs. {{monto}}."):
    return {"id": "plantilla-001", "texto": texto}


def make_invoice():
    return {
        "invoice_id":   "inv-001",
        "cliente_id":   "cli-001",
        "amount":       199.0,
        "due_date":     date(2025, 6, 15),
        "year":         2025,
        "month":        6,
        "type":         "Mensualidad",
        "phone_number": "76543210",
        "nombre":       "Ana López",
        "plan_name":    "Plan Oro",
    }
