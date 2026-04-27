import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
"""
Tests de run_once del ReminderJob — flujos completos.
(Los helpers individuales están en test_reminder_helpers.py)
"""
import asyncio
from datetime import date
from unittest.mock import AsyncMock, MagicMock

from helpers import run, make_invoice, make_conn_mock, make_pool_from_conns

from app.jobs.reminder_job import run_once


def _make_config_dict(activo=True, dias_antes=7):
    return {
        "tipo":           "RECORDATORIO_R1",
        "activo":         activo,
        "delay_segundos": 0,
        "hora_inicio":    None,
        "hora_fin":       None,
        "inmediato":      True,
        "dias_antes":     dias_antes,
    }


class TestRunOnce:
    def test_new_invoice_inserts_outbox(self):
        cfg = _make_config_dict()
        inv = make_invoice()

        conn_cfg  = AsyncMock(); conn_cfg.fetch  = AsyncMock(return_value=[cfg])
        conn_inv  = AsyncMock(); conn_inv.fetch  = AsyncMock(return_value=[inv])
        conn_per  = AsyncMock()
        conn_per.fetchval = AsyncMock(side_effect=[None, 1])
        conn_per.execute  = AsyncMock()

        calls = iter([
            AsyncMock(__aenter__=AsyncMock(return_value=conn_cfg), __aexit__=AsyncMock(return_value=False)),
            AsyncMock(__aenter__=AsyncMock(return_value=conn_inv), __aexit__=AsyncMock(return_value=False)),
            AsyncMock(__aenter__=AsyncMock(return_value=conn_per), __aexit__=AsyncMock(return_value=False)),
        ])
        pool = AsyncMock(); pool.acquire = MagicMock(side_effect=lambda: next(calls))

        run(run_once(pool, target_date=date(2025, 6, 8)))
        conn_per.execute.assert_called_once()

    def test_duplicate_invoice_skipped(self):
        cfg = _make_config_dict()
        inv = make_invoice()

        conn_cfg  = AsyncMock(); conn_cfg.fetch  = AsyncMock(return_value=[cfg])
        conn_inv  = AsyncMock(); conn_inv.fetch  = AsyncMock(return_value=[inv])
        conn_per  = AsyncMock()
        conn_per.fetchval = AsyncMock(return_value=1)  # already exists
        conn_per.execute  = AsyncMock()

        calls = iter([
            AsyncMock(__aenter__=AsyncMock(return_value=conn_cfg), __aexit__=AsyncMock(return_value=False)),
            AsyncMock(__aenter__=AsyncMock(return_value=conn_inv), __aexit__=AsyncMock(return_value=False)),
            AsyncMock(__aenter__=AsyncMock(return_value=conn_per), __aexit__=AsyncMock(return_value=False)),
        ])
        pool = AsyncMock(); pool.acquire = MagicMock(side_effect=lambda: next(calls))

        run(run_once(pool, target_date=date(2025, 6, 8)))
        conn_per.execute.assert_not_called()

    def test_disabled_type_skips_without_fetching_invoices(self):
        cfg = _make_config_dict(activo=False)
        conn_cfg = AsyncMock(); conn_cfg.fetch = AsyncMock(return_value=[cfg])

        calls = iter([
            AsyncMock(__aenter__=AsyncMock(return_value=conn_cfg), __aexit__=AsyncMock(return_value=False)),
        ])
        pool = AsyncMock(); pool.acquire = MagicMock(side_effect=lambda: next(calls))

        run(run_once(pool, target_date=date(2025, 6, 8)))
        # Solo se llamó una vez (para config), nunca para invoices
        assert pool.acquire.call_count == 1

    def test_uses_target_date_when_provided(self):
        """target_date se pasa directamente sin consultar la fecha actual."""
        cfg = _make_config_dict()
        conn_cfg = AsyncMock(); conn_cfg.fetch = AsyncMock(return_value=[cfg])
        conn_inv = AsyncMock(); conn_inv.fetch = AsyncMock(return_value=[])

        calls = iter([
            AsyncMock(__aenter__=AsyncMock(return_value=conn_cfg), __aexit__=AsyncMock(return_value=False)),
            AsyncMock(__aenter__=AsyncMock(return_value=conn_inv), __aexit__=AsyncMock(return_value=False)),
        ])
        pool = AsyncMock(); pool.acquire = MagicMock(side_effect=lambda: next(calls))

        # No lanza excepción con fecha específica
        run(run_once(pool, target_date=date(2020, 1, 1)))
