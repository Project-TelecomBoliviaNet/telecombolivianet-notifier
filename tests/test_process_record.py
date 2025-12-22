"""
Tests de process_record con las nuevas firmas (RawOutboxRow en lugar de dict).
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datetime import time
from unittest.mock import AsyncMock, MagicMock, patch

from helpers import run, make_pool_from_conns
from app.domain.records import RawOutboxRow
from app.jobs.outbox_worker import process_record
from app.services.whatsapp import RateLimitError


def _make_row(**kwargs) -> RawOutboxRow:
    d = dict(id="outbox-001", tipo="AVISO_CORTE", cliente_id="cli-001",
             phone_number="76543210", intentos=0,
             # 3 items so enrich_context_from_db is NOT triggered (threshold < 3)
             contexto_json='{"nombre":"Ana","monto":"100.00","periodos":"1"}')
    d.update(kwargs)
    return RawOutboxRow(**d)


def _make_config(activo=True, inmediato=True, hora_inicio=None, hora_fin=None):
    return {"tipo": "AVISO_CORTE", "activo": activo, "inmediato": inmediato,
            "hora_inicio": hora_inicio or time(8, 0),
            "hora_fin":    hora_fin    or time(20, 0),
            "delay_segundos": 0, "dias_antes": None}


def _make_plantilla(texto="Hola {{nombre}}, debes Bs. {{monto}}."):
    return {
        "id": "p-001",
        "texto": texto,
        "meta_template_name": None,
        "hsm_status": None,
        "meta_param_order": None,
        "meta_language_code": None,
    }


def _pool_two_conns(config, plantilla):
    conn1 = AsyncMock()
    conn1.fetchrow = AsyncMock(side_effect=[config, plantilla])
    conn1.execute  = AsyncMock()
    conn2 = AsyncMock(); conn2.execute = AsyncMock()
    return make_pool_from_conns(conn1, conn2), conn1, conn2


class TestProcessRecordSuccess:
    def test_successful_send_calls_on_sent(self):
        pool, _, _ = _pool_two_conns(_make_config(), _make_plantilla())
        wa = AsyncMock(); on_sent = MagicMock(); on_failed = MagicMock()
        run(process_record(pool, wa, _make_row(), {}, on_sent=on_sent, on_failed=on_failed))
        wa.send.assert_called_once()
        on_sent.assert_called_once()
        on_failed.assert_not_called()

    def test_successful_send_writes_finalize_ok_and_log(self):
        pool, _, conn2 = _pool_two_conns(_make_config(), _make_plantilla())
        run(process_record(pool, AsyncMock(), _make_row(), {}))
        assert conn2.execute.call_count == 2

    def test_missing_template_vars_logs_warning_but_still_sends(self):
        pool, _, _ = _pool_two_conns(
            _make_config(), _make_plantilla(texto="Hola {{nombre}} tu plan es {{plan_x}}"))
        wa = AsyncMock()
        with patch("app.jobs.message_renderer.log") as mock_log:
            run(process_record(pool, wa, _make_row(), {}))
        wa.send.assert_called_once()
        warnings = [c for c in mock_log.warning.call_args_list
                    if "template_missing_vars" in str(c)]
        assert len(warnings) == 1


class TestProcessRecordTypeDisabled:
    def test_disabled_type_does_not_send(self):
        conn = AsyncMock(); conn.fetchrow = AsyncMock(return_value=_make_config(activo=False))
        conn.execute = AsyncMock()
        wa = AsyncMock()
        run(process_record(make_pool_from_conns(conn), wa, _make_row(), {}))
        wa.send.assert_not_called()

    def test_none_config_treated_as_disabled(self):
        conn = AsyncMock(); conn.fetchrow = AsyncMock(return_value=None)
        conn.execute = AsyncMock()
        wa = AsyncMock()
        run(process_record(make_pool_from_conns(conn), wa, _make_row(), {}))
        wa.send.assert_not_called()

    def test_disabled_type_writes_two_executes(self):
        conn = AsyncMock(); conn.fetchrow = AsyncMock(return_value=_make_config(activo=False))
        conn.execute = AsyncMock()
        run(process_record(make_pool_from_conns(conn), AsyncMock(), _make_row(), {}))
        assert conn.execute.call_count == 2


class TestProcessRecordNoTemplate:
    def test_no_template_does_not_send(self):
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(side_effect=[_make_config(), None])
        conn.execute  = AsyncMock()
        wa = AsyncMock()
        run(process_record(make_pool_from_conns(conn), wa, _make_row(), {}))
        wa.send.assert_not_called()

    def test_no_template_writes_finalize_failed(self):
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(side_effect=[_make_config(), None])
        conn.execute  = AsyncMock()
        run(process_record(make_pool_from_conns(conn), AsyncMock(), _make_row(), {}))
        assert conn.execute.call_count == 2


class TestProcessRecordWindowHandling:
    def test_outside_window_returns_to_pool_with_proximo_intento(self):
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value=_make_config(inmediato=False))
        conn.execute  = AsyncMock()
        with patch("app.jobs.message_renderer.is_within_window", return_value=False), \
             patch("app.jobs.outbox_batch_fetcher.seconds_until_next_window", return_value=3600):
            run(process_record(make_pool_from_conns(conn), AsyncMock(), _make_row(), {}))
        assert "3600" in str(conn.execute.call_args)

    def test_outside_window_does_not_send(self):
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value=_make_config(inmediato=False))
        conn.execute  = AsyncMock()
        wa = AsyncMock()
        with patch("app.jobs.message_renderer.is_within_window", return_value=False), \
             patch("app.jobs.outbox_batch_fetcher.seconds_until_next_window", return_value=100):
            run(process_record(make_pool_from_conns(conn), wa, _make_row(), {}))
        wa.send.assert_not_called()

    def test_inmediato_ignores_window(self):
        pool, _, conn2 = _pool_two_conns(_make_config(inmediato=True), _make_plantilla())
        wa = AsyncMock()
        with patch("app.jobs.message_renderer.is_within_window", return_value=False):
            run(process_record(pool, wa, _make_row(), {}))
        wa.send.assert_called_once()


class TestProcessRecordRateLimit:
    def test_rate_limit_does_not_consume_attempt(self):
        pool, _, conn2 = _pool_two_conns(_make_config(), _make_plantilla())
        wa = AsyncMock(); wa.send = AsyncMock(side_effect=RateLimitError(retry_after=120))
        run(process_record(pool, wa, _make_row(intentos=0), {}))
        assert conn2.execute.call_count == 1
        assert "120" in str(conn2.execute.call_args)

    def test_rate_limit_does_not_call_on_failed(self):
        pool, _, _ = _pool_two_conns(_make_config(), _make_plantilla())
        wa = AsyncMock(); wa.send = AsyncMock(side_effect=RateLimitError(retry_after=60))
        on_failed = MagicMock()
        run(process_record(pool, wa, _make_row(), {}, on_failed=on_failed))
        on_failed.assert_not_called()


class TestProcessRecordFailures:
    def _pool(self, config, plantilla):
        conn1 = AsyncMock(); conn1.fetchrow = AsyncMock(side_effect=[config, plantilla])
        conn1.execute = AsyncMock()
        conn2 = AsyncMock(); conn2.execute = AsyncMock()
        return make_pool_from_conns(conn1, conn2), conn2

    def test_first_failure_schedules_retry(self):
        pool, conn2 = self._pool(_make_config(), _make_plantilla())
        wa = AsyncMock(); wa.send = AsyncMock(side_effect=Exception("network"))
        with patch("app.jobs.outbox_batch_fetcher.get_max_attempts", return_value=4), \
             patch("app.jobs.outbox_batch_fetcher.get_backoff_seconds", return_value=[60,300,1800,7200]):
            run(process_record(pool, wa, _make_row(intentos=0), {}))
        assert "60" in str(conn2.execute.call_args_list)

    def test_fourth_failure_marks_permanent(self):
        pool, _ = self._pool(_make_config(), _make_plantilla())
        wa = AsyncMock(); wa.send = AsyncMock(side_effect=Exception("fatal"))
        on_failed = MagicMock()
        with patch("app.jobs.outbox_batch_fetcher.get_max_attempts", return_value=4):
            run(process_record(pool, wa, _make_row(intentos=3), {}, on_failed=on_failed))
        on_failed.assert_called_once()

    def test_on_failed_not_called_on_retry(self):
        pool, _ = self._pool(_make_config(), _make_plantilla())
        wa = AsyncMock(); wa.send = AsyncMock(side_effect=Exception("transient"))
        on_failed = MagicMock()
        with patch("app.jobs.outbox_batch_fetcher.get_max_attempts", return_value=4), \
             patch("app.jobs.outbox_batch_fetcher.get_backoff_seconds", return_value=[60]):
            run(process_record(pool, wa, _make_row(intentos=0), {}, on_failed=on_failed))
        on_failed.assert_not_called()
