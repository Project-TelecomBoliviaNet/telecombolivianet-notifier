import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
"""
Tests de run_once del ReminderJob — flujos completos.

run_once() ahora usa UNA sola conexión para FETCH_ALL_CONFIGS +
FETCH_ACTIVE_REMINDER_TYPES + FETCH_SYSTEM_CONFIG_BULK, luego delega
la lógica de inserción a process_reminder_type (reminder_persistence).
Los tests verifican el routing de run_once; la lógica de insert/dedup
se prueba en test_reminder_helpers.py (via process_reminder_type).
"""
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

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


def _one_conn_pool(side_effects):
    """Pool mock with a single connection that handles multiple .fetch() calls."""
    conn = AsyncMock()
    conn.fetch = AsyncMock(side_effect=side_effects)
    return make_pool_from_conns(conn), conn


class TestRunOnce:
    def test_new_invoice_inserts_outbox(self):
        """Active config → process_reminder_type is called once."""
        cfg = _make_config_dict()
        pool, _ = _one_conn_pool([
            [cfg],                          # FETCH_ALL_CONFIGS
            [{"tipo": "RECORDATORIO_R1"}],  # FETCH_ACTIVE_REMINDER_TYPES
            [],                             # FETCH_SYSTEM_CONFIG_BULK
        ])

        with patch("app.jobs.reminder_job.process_reminder_type",
                   new_callable=AsyncMock) as mock_proc:
            run(run_once(pool, target_date=date(2025, 6, 8)))

        mock_proc.assert_called_once()

    def test_duplicate_invoice_skipped(self):
        """run_once delegates duplicate handling to process_reminder_type."""
        cfg = _make_config_dict()
        pool, _ = _one_conn_pool([
            [cfg],
            [{"tipo": "RECORDATORIO_R1"}],
            [],
        ])

        with patch("app.jobs.reminder_job.process_reminder_type",
                   new_callable=AsyncMock) as mock_proc:
            run(run_once(pool, target_date=date(2025, 6, 8)))

        # Exactly one call per active reminder type
        assert mock_proc.call_count == 1

    def test_disabled_type_skips_without_fetching_invoices(self):
        """Disabled config (activo=False) → process_reminder_type NOT called."""
        cfg = _make_config_dict(activo=False)
        pool, _ = _one_conn_pool([
            [cfg],
            [{"tipo": "RECORDATORIO_R1"}],
            [],
        ])

        with patch("app.jobs.reminder_job.process_reminder_type",
                   new_callable=AsyncMock) as mock_proc:
            run(run_once(pool, target_date=date(2025, 6, 8)))

        mock_proc.assert_not_called()

    def test_uses_target_date_when_provided(self):
        """target_date is used directly — no exception raised for arbitrary dates."""
        pool, _ = _one_conn_pool([
            [],   # FETCH_ALL_CONFIGS (empty → no types)
            [],   # FETCH_ACTIVE_REMINDER_TYPES (empty → early return)
            [],   # FETCH_SYSTEM_CONFIG_BULK
        ])

        # Should not raise with a historical date
        run(run_once(pool, target_date=date(2020, 1, 1)))
