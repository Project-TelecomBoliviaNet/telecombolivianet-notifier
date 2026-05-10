"""
Tests para las utilidades de main.py:
  - _validate_env
  - _parse_args
  - _setup_logging
"""
import os
import sys
import logging
from unittest.mock import patch, MagicMock
import pytest

import importlib.util

def _import_main():
    spec = importlib.util.spec_from_file_location(
        "main",
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

main = _import_main()


class TestValidateEnv:
    def test_exits_when_database_url_missing(self):
        mock_log = MagicMock()
        clean = {k: v for k, v in os.environ.items() if k != "DATABASE_URL"}
        with patch.dict(os.environ, clean, clear=True), \
             pytest.raises(SystemExit) as exc_info:
            main._validate_env(mock_log)
        assert exc_info.value.code == 1

    def test_no_exit_when_database_url_present(self):
        mock_log = MagicMock()
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://localhost/test"}):
            main._validate_env(mock_log)  # no debe lanzar

    def test_warns_when_whatsapp_token_missing(self):
        mock_log = MagicMock()
        clean = {k: v for k, v in os.environ.items()
                 if k not in ("WHATSAPP_TOKEN", "WHATSAPP_PHONE_NUMBER_ID")}
        clean["DATABASE_URL"] = "postgresql://localhost/test"
        with patch.dict(os.environ, clean, clear=True):
            main._validate_env(mock_log)
        mock_log.warning.assert_called()

    def test_no_warning_when_all_vars_present(self):
        mock_log = MagicMock()
        with patch.dict(os.environ, {
            "DATABASE_URL":             "postgresql://localhost/test",
            "WHATSAPP_TOKEN":           "tok",
            "WHATSAPP_PHONE_NUMBER_ID": "pid",
        }):
            main._validate_env(mock_log)
        mock_log.error.assert_not_called()

    def test_logs_defaults_for_optional_vars(self):
        mock_log = MagicMock()
        clean = {"DATABASE_URL": "postgresql://localhost/test",
                 "WHATSAPP_TOKEN": "tok", "WHATSAPP_PHONE_NUMBER_ID": "pid"}
        for k in ("POLL_INTERVAL_SECONDS", "TIMEZONE", "LOG_LEVEL", "HEALTH_PORT"):
            clean.pop(k, None)
        with patch.dict(os.environ, clean, clear=True):
            main._validate_env(mock_log)
        mock_log.info.assert_called()


class TestParseArgs:
    def test_no_args_returns_none_reminder_date(self):
        with patch("sys.argv", ["main.py"]):
            args = main._parse_args()
        assert args.reminder_date is None

    def test_reminder_date_arg_parsed(self):
        with patch("sys.argv", ["main.py", "--reminder-date", "2025-06-08"]):
            args = main._parse_args()
        assert args.reminder_date == "2025-06-08"

    def test_invalid_flag_exits(self):
        with patch("sys.argv", ["main.py", "--unknown-flag"]), \
             pytest.raises(SystemExit):
            main._parse_args()


class TestSetupLogging:
    def test_does_not_raise(self):
        """_setup_logging() debe ejecutarse sin errores."""
        with patch.dict(os.environ, {"LOG_LEVEL": "INFO"}):
            main._setup_logging()

    def test_invalid_level_does_not_raise(self):
        """Nivel inválido → no lanza excepción (usa getattr default INFO)."""
        with patch.dict(os.environ, {"LOG_LEVEL": "NOTEXIST"}):
            main._setup_logging()

    def test_configures_structlog(self):
        """Después de _setup_logging(), structlog.get_logger() funciona."""
        import structlog
        with patch.dict(os.environ, {"LOG_LEVEL": "INFO"}):
            main._setup_logging()
        assert structlog.get_logger("test") is not None
