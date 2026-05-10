"""
Tests para utilidades de tiempo del outbox_worker:
  - is_within_window
  - seconds_until_next_window
  - now_bolivia
"""
from datetime import time, datetime
from unittest.mock import patch, MagicMock

import pytz

from app.jobs._time_window import is_within_window, seconds_until_next_window, now_bolivia

BOLIVIA_TZ = pytz.timezone("America/La_Paz")


def _mock_now(hour, minute):
    """Parchea now_bolivia para devolver una hora fija."""
    dt = MagicMock()
    dt.time.return_value = time(hour, minute).replace(second=0, microsecond=0)
    dt.hour   = hour
    dt.minute = minute
    dt.replace = lambda **kw: datetime(2025, 6, 8, hour, minute, 0,
                                       tzinfo=BOLIVIA_TZ).replace(**kw)
    return dt


class TestIsWithinWindow:
    def _patch(self, hour, minute):
        return patch("app.jobs._time_window.now_bolivia",
                     return_value=_mock_now(hour, minute))

    def test_exactly_at_start_is_inside(self):
        with self._patch(8, 0):
            assert is_within_window(time(8, 0), time(20, 0)) is True

    def test_exactly_at_end_is_inside(self):
        with self._patch(20, 0):
            assert is_within_window(time(8, 0), time(20, 0)) is True

    def test_midday_is_inside(self):
        with self._patch(14, 30):
            assert is_within_window(time(8, 0), time(20, 0)) is True

    def test_before_window_is_outside(self):
        with self._patch(7, 59):
            assert is_within_window(time(8, 0), time(20, 0)) is False

    def test_after_window_is_outside(self):
        with self._patch(20, 1):
            assert is_within_window(time(8, 0), time(20, 0)) is False

    def test_midnight_is_outside(self):
        with self._patch(0, 0):
            assert is_within_window(time(8, 0), time(20, 0)) is False

    def test_accepts_string_format(self):
        """hora_inicio/hora_fin pueden venir como string desde asyncpg."""
        with self._patch(10, 0):
            assert is_within_window("08:00", "20:00") is True

    def test_accepts_string_format_outside(self):
        with self._patch(7, 0):
            assert is_within_window("08:00", "20:00") is False


class TestSecondsUntilNextWindow:
    def test_window_later_today(self):
        """Si la ventana es en 2 horas, retorna ~7200 segundos."""
        fixed = datetime(2025, 6, 8, 6, 0, 0, tzinfo=BOLIVIA_TZ)
        with patch("app.jobs._time_window.now_bolivia", return_value=fixed):
            secs = seconds_until_next_window(time(8, 0))
            assert 7190 <= secs <= 7210  # ~2 horas en segundos

    def test_window_already_passed_returns_tomorrow(self):
        """Si la ventana ya pasó hoy, calcula para mañana (~23h)."""
        fixed = datetime(2025, 6, 8, 21, 0, 0, tzinfo=BOLIVIA_TZ)
        with patch("app.jobs._time_window.now_bolivia", return_value=fixed):
            secs = seconds_until_next_window(time(8, 0))
            # 11 horas hasta las 8am del día siguiente
            assert 39_000 <= secs <= 40_000

    def test_minimum_is_1(self):
        """Nunca retorna 0 o negativo."""
        fixed = datetime(2025, 6, 8, 8, 0, 0, tzinfo=BOLIVIA_TZ)
        with patch("app.jobs._time_window.now_bolivia", return_value=fixed):
            secs = seconds_until_next_window(time(8, 0))
            assert secs >= 1

    def test_accepts_string_hora_inicio(self):
        fixed = datetime(2025, 6, 8, 6, 0, 0, tzinfo=BOLIVIA_TZ)
        with patch("app.jobs._time_window.now_bolivia", return_value=fixed):
            secs = seconds_until_next_window("08:00")
            assert secs > 0


class TestNowBolivia:
    def test_returns_datetime_with_tz(self):
        dt = now_bolivia()
        assert dt.tzinfo is not None
        assert str(dt.tzinfo) == "America/La_Paz"
