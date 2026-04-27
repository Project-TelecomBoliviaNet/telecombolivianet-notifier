"""
Tests para funciones de configuración del outbox_worker.
Verifica que los valores de entorno se lean correctamente con defaults.
"""
import os
from unittest.mock import patch

from app.jobs.outbox_worker import get_batch_size, get_max_attempts, get_backoff_seconds


class TestGetBatchSize:
    def test_default_is_50(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BATCH_SIZE", None)
            assert get_batch_size() == 50

    def test_reads_from_env(self):
        with patch.dict(os.environ, {"BATCH_SIZE": "100"}):
            assert get_batch_size() == 100

    def test_custom_small_value(self):
        with patch.dict(os.environ, {"BATCH_SIZE": "5"}):
            assert get_batch_size() == 5


class TestGetMaxAttempts:
    def test_default_is_4(self):
        os.environ.pop("MAX_ATTEMPTS", None)
        assert get_max_attempts() == 4

    def test_reads_from_env(self):
        with patch.dict(os.environ, {"MAX_ATTEMPTS": "6"}):
            assert get_max_attempts() == 6


class TestGetBackoffSeconds:
    def test_default_values(self):
        os.environ.pop("BACKOFF_SECONDS", None)
        assert get_backoff_seconds() == [60, 300, 1800, 7200]

    def test_reads_from_env(self):
        with patch.dict(os.environ, {"BACKOFF_SECONDS": "10,20,30"}):
            assert get_backoff_seconds() == [10, 20, 30]

    def test_strips_whitespace(self):
        with patch.dict(os.environ, {"BACKOFF_SECONDS": "10, 20, 30"}):
            assert get_backoff_seconds() == [10, 20, 30]
