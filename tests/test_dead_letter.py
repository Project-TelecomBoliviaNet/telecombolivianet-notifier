"""
FIX-24 — Dead Letter Queue: tests unitarios para classify_error y move_to_dlq.
Sin dependencias externas — la conexión asyncpg está completamente mockeada.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from unittest.mock import AsyncMock

from app.services.dead_letter import classify_error, move_to_dlq

# ──────────────────────────────────────────────────────────────────────────────
# classify_error()
# ──────────────────────────────────────────────────────────────────────────────

class TestClassifyError:
    def test_invalid_phone_por_keyword(self):
        assert classify_error("invalid_phone number") == "INVALID_PHONE"

    def test_invalid_phone_por_does_not_exist(self):
        assert classify_error("number does not exist on WhatsApp") == "INVALID_PHONE"

    def test_invalid_phone_por_invalidphone_compuesto(self):
        assert classify_error("InvalidPhone: +591799") == "INVALID_PHONE"

    def test_template_error_por_template(self):
        assert classify_error("template not found") == "TEMPLATE_ERROR"

    def test_template_error_por_variable(self):
        assert classify_error("variable mismatch in payload") == "TEMPLATE_ERROR"

    def test_template_error_por_plantilla(self):
        assert classify_error("plantilla inválida") == "TEMPLATE_ERROR"

    def test_rate_limit_por_rate_limit(self):
        assert classify_error("rate limit exceeded") == "RATE_LIMIT"

    def test_rate_limit_por_429(self):
        assert classify_error("HTTP 429 Too Many Requests") == "RATE_LIMIT"

    def test_rate_limit_por_ratelimit(self):
        assert classify_error("RateLimit hit on endpoint") == "RATE_LIMIT"

    def test_circuit_open_por_circuit(self):
        assert classify_error("circuit breaker open") == "CIRCUIT_OPEN"

    def test_circuit_open_por_unavailable(self):
        assert classify_error("service unavailable") == "CIRCUIT_OPEN"

    def test_unknown_para_error_no_reconocido(self):
        assert classify_error("connection timeout") == "UNKNOWN"

    def test_unknown_para_error_vacio(self):
        assert classify_error("") == "UNKNOWN"

    def test_clasificacion_es_case_insensitive(self):
        assert classify_error("INVALID_PHONE") == "INVALID_PHONE"
        assert classify_error("RATE LIMIT exceeded") == "RATE_LIMIT"  # space variant
        assert classify_error("TEMPLATE mismatch") == "TEMPLATE_ERROR"


# ──────────────────────────────────────────────────────────────────────────────
# move_to_dlq()
# ──────────────────────────────────────────────────────────────────────────────

OUTBOX_ID   = "aaaaaaaa-0001-0001-0001-aaaaaaaaaaaa"
TIPO        = "AVISO_CORTE"
PHONE       = "59176543210"
CONTEXTO    = '{"nombre":"Ana"}'


def make_conn() -> AsyncMock:
    conn = AsyncMock()
    conn.execute = AsyncMock()
    return conn


def run(coro):
    import asyncio
    return asyncio.run(coro)


class TestMoveToDlq:
    def test_inserta_en_dead_letter_y_marca_fallido_dlq(self):
        conn = make_conn()
        run(move_to_dlq(conn, OUTBOX_ID, TIPO, PHONE, "invalid_phone", CONTEXTO))

        assert conn.execute.call_count == 2

        # Primera llamada: INSERT INTO NotifDeadLetter
        first_call_args = conn.execute.call_args_list[0].args
        assert "NotifDeadLetter" in first_call_args[0]
        assert first_call_args[1] == OUTBOX_ID    # OriginalOutboxId
        assert first_call_args[2] == TIPO
        assert first_call_args[3] == PHONE
        assert first_call_args[4] == "INVALID_PHONE"  # error clasificado
        assert "invalid_phone" in first_call_args[5]  # ErrorMessage
        assert first_call_args[6] == CONTEXTO

        # Segunda llamada: UPDATE NotifOutbox SET EstadoFinal='FALLIDO_DLQ'
        second_call_args = conn.execute.call_args_list[1].args
        assert "FALLIDO_DLQ" in second_call_args[0]
        assert second_call_args[1] == OUTBOX_ID

    def test_clasifica_error_correctamente_antes_de_insertar(self):
        conn = make_conn()
        run(move_to_dlq(conn, OUTBOX_ID, TIPO, PHONE, "rate limit exceeded", CONTEXTO))

        first_args = conn.execute.call_args_list[0].args
        assert first_args[4] == "RATE_LIMIT"

    def test_trunca_error_message_a_500_chars(self):
        conn = make_conn()
        long_error = "e" * 1000
        run(move_to_dlq(conn, OUTBOX_ID, TIPO, PHONE, long_error, CONTEXTO))

        first_args = conn.execute.call_args_list[0].args
        assert len(first_args[5]) == 500

    def test_usa_contexto_vacio_si_none(self):
        conn = make_conn()
        run(move_to_dlq(conn, OUTBOX_ID, TIPO, PHONE, "error", None))

        first_args = conn.execute.call_args_list[0].args
        assert first_args[6] == "{}"

    def test_preserva_contexto_json_si_presente(self):
        conn = make_conn()
        ctx = '{"monto":"200.50","nombre":"Roberto"}'
        run(move_to_dlq(conn, OUTBOX_ID, TIPO, PHONE, "template error", ctx))

        first_args = conn.execute.call_args_list[0].args
        assert first_args[6] == ctx

    def test_error_desconocido_clasifica_como_unknown(self):
        conn = make_conn()
        run(move_to_dlq(conn, OUTBOX_ID, TIPO, PHONE, "algo completamente distinto", CONTEXTO))

        first_args = conn.execute.call_args_list[0].args
        assert first_args[4] == "UNKNOWN"
