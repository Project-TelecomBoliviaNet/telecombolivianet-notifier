import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
"""
Tests de run_once y run_reminder_scheduler del ReminderJob.
Tests detallados de helpers en test_reminder_helpers.py.
"""
import asyncio
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

from helpers import run, make_invoice

from app.jobs.reminder_job import run_once, run_reminder_scheduler
from app.jobs.reminder_context import build_period_label


class TestPeriodLabel:
    def test_mensualidad(self):
        assert build_period_label(2025, 5, "Mensualidad") == "Mayo 2025"

    def test_instalacion(self):
        assert build_period_label(2025, 1, "Instalacion") == "Instalación"

    def test_all_months(self):
        expected = ["Enero","Febrero","Marzo","Abril","Mayo","Junio",
                    "Julio","Agosto","Septiembre","Octubre","Noviembre","Diciembre"]
        for i, name in enumerate(expected, start=1):
            assert build_period_label(2025, i, "Mensualidad") == f"{name} 2025"
