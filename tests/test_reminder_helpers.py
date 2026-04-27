import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
"""
Tests para los helpers del ReminderJob:
  - build_period_label
  - build_reminder_context
  - get_last_run_day
  - persist_job_run
  - try_acquire_lock
  - release_lock
  - process_invoice_reminder
"""
import asyncio
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

from helpers import run, make_invoice, make_conn_mock, make_pool_from_conns

from app.jobs.reminder_job import (
    build_period_label,
    build_reminder_context,
    get_last_run_day,
    persist_job_run,
    try_acquire_lock,
    release_lock,
    process_invoice_reminder,
)


class TestBuildPeriodLabel:
    def test_mensualidad_returns_month_year(self):
        assert build_period_label(2025, 5, "Mensualidad") == "Mayo 2025"

    def test_instalacion_returns_label(self):
        assert build_period_label(2025, 1, "Instalacion") == "Instalación"

    def test_all_twelve_months(self):
        names = ["Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
                 "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre"]
        for i, name in enumerate(names, start=1):
            assert build_period_label(2025, i, "Mensualidad") == f"{name} 2025"

    def test_year_is_included(self):
        assert "2024" in build_period_label(2024, 3, "Mensualidad")

    def test_other_type_uses_month_year(self):
        assert build_period_label(2025, 6, "OtroTipo") == "Junio 2025"


class TestBuildReminderContext:
    def test_all_keys_present(self):
        inv = make_invoice()
        ctx = build_reminder_context(inv, date(2025, 6, 15), pending_months=2)
        assert set(ctx.keys()) == {
            "nombre", "monto", "fecha_vencimiento",
            "periodo", "meses_pendientes", "plan"
        }

    def test_monto_formatted_with_two_decimals(self):
        inv = make_invoice()
        inv["amount"] = 199.0
        ctx = build_reminder_context(inv, date(2025, 6, 15), pending_months=1)
        assert ctx["monto"] == "199.00"

    def test_fecha_vencimiento_format(self):
        inv = make_invoice()
        ctx = build_reminder_context(inv, date(2025, 6, 15), pending_months=1)
        assert ctx["fecha_vencimiento"] == "15/06/2025"

    def test_nombre_fallback_to_empty_string(self):
        inv = make_invoice()
        inv["nombre"] = None
        ctx = build_reminder_context(inv, date(2025, 6, 15), pending_months=1)
        assert ctx["nombre"] == ""

    def test_plan_fallback_to_internet(self):
        inv = make_invoice()
        inv["plan_name"] = None
        ctx = build_reminder_context(inv, date(2025, 6, 15), pending_months=1)
        assert ctx["plan"] == "internet"

    def test_pending_months_as_string(self):
        inv = make_invoice()
        ctx = build_reminder_context(inv, date(2025, 6, 15), pending_months=3)
        assert ctx["meses_pendientes"] == "3"

    def test_pending_months_zero_becomes_one(self):
        """0 meses pendientes → usa 1 para evitar mensaje confuso."""
        inv = make_invoice()
        ctx = build_reminder_context(inv, date(2025, 6, 15), pending_months=0)
        assert ctx["meses_pendientes"] == "1"


class TestGetLastRunDay:
    def test_returns_date_when_exists(self):
        mock_date = date(2025, 6, 7)
        row = MagicMock()
        row.__getitem__ = MagicMock(return_value=mock_date)
        row.__bool__ = MagicMock(return_value=True)

        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value={"last_run": mock_date})
        pool = make_pool_from_conns(conn)

        result = run(get_last_run_day(pool))
        assert result == mock_date

    def test_returns_none_when_no_record(self):
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value=None)
        pool = make_pool_from_conns(conn)

        result = run(get_last_run_day(pool))
        assert result is None

    def test_returns_none_on_db_exception(self):
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(side_effect=Exception("DB error"))
        pool = make_pool_from_conns(conn)

        result = run(get_last_run_day(pool))
        assert result is None


class TestPersistJobRun:
    def test_calls_upsert(self):
        conn = AsyncMock()
        pool = make_pool_from_conns(conn)
        run(persist_job_run(pool, date(2025, 6, 8)))
        conn.execute.assert_called_once()

    def test_silently_handles_db_exception(self):
        conn = AsyncMock()
        conn.execute = AsyncMock(side_effect=Exception("DB error"))
        pool = make_pool_from_conns(conn)
        # No debe lanzar excepción
        run(persist_job_run(pool, date(2025, 6, 8)))


class TestTryAcquireLock:
    def test_returns_true_when_lock_acquired(self):
        conn = AsyncMock()
        conn.fetchval = AsyncMock(return_value=True)
        result = run(try_acquire_lock(conn))
        assert result is True

    def test_returns_false_when_lock_busy(self):
        conn = AsyncMock()
        conn.fetchval = AsyncMock(return_value=False)
        result = run(try_acquire_lock(conn))
        assert result is False

    def test_returns_false_on_none(self):
        conn = AsyncMock()
        conn.fetchval = AsyncMock(return_value=None)
        result = run(try_acquire_lock(conn))
        assert result is False


class TestReleaseLock:
    def test_calls_advisory_unlock(self):
        conn = AsyncMock()
        run(release_lock(conn))
        conn.fetchval.assert_called_once()


class TestProcessInvoiceReminder:
    def _make_cfg(self, dias_antes=7, delay=0):
        return {"dias_antes": dias_antes, "delay_segundos": delay}

    def test_new_invoice_inserts_and_returns_true(self):
        inv = make_invoice()
        conn = AsyncMock()
        conn.fetchval = AsyncMock(side_effect=[None, 1])  # not exists, 1 pending
        conn.execute  = AsyncMock()

        result = run(process_invoice_reminder(
            conn, inv, "RECORDATORIO_R1", self._make_cfg(), date(2025, 6, 8)
        ))
        assert result is True
        conn.execute.assert_called_once()

    def test_duplicate_invoice_returns_false(self):
        inv = make_invoice()
        conn = AsyncMock()
        conn.fetchval = AsyncMock(return_value=1)  # already exists
        conn.execute  = AsyncMock()

        result = run(process_invoice_reminder(
            conn, inv, "RECORDATORIO_R1", self._make_cfg(), date(2025, 6, 8)
        ))
        assert result is False
        conn.execute.assert_not_called()

    def test_context_json_is_valid_json(self):
        import json
        inv = make_invoice()
        conn = AsyncMock()
        conn.fetchval = AsyncMock(side_effect=[None, 2])
        conn.execute  = AsyncMock()

        run(process_invoice_reminder(
            conn, inv, "RECORDATORIO_R1", self._make_cfg(), date(2025, 6, 8)
        ))
        call_args = conn.execute.call_args.args
        # El 5to argumento es el contexto JSON
        ctx = json.loads(call_args[5])
        assert "nombre" in ctx
        assert "monto" in ctx
