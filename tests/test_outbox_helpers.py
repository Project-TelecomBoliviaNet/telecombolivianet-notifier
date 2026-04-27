"""
Tests unitarios para los helpers de outbox_worker.
Actualizados para las nuevas firmas con OutboxRecord y RawOutboxRow.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from helpers import run, make_pool_from_conns
from app.domain.records import OutboxRecord, RawOutboxRow
from app.jobs.outbox_worker import (
    fetch_and_claim_batch,
    finalize_sent,
    finalize_type_disabled,
    finalize_no_template,
    return_to_pool_until_window,
    return_to_pool_rate_limited,
    apply_backoff_or_fail,
)


def _make_raw_row(**kwargs) -> RawOutboxRow:
    defaults = dict(id="oid", tipo="TIPO", cliente_id="cid",
                    phone_number="76543210", intentos=0, contexto_json="{}")
    defaults.update(kwargs)
    return RawOutboxRow(**defaults)


def _make_rec(**kwargs) -> OutboxRecord:
    defaults = dict(outbox_id="oid", cliente_id="cid", tipo="TIPO",
                    phone="76543210", intentos=0, phone_log="****210",
                    mensaje="Hola Ana")
    defaults.update(kwargs)
    return OutboxRecord(**defaults)


class TestFetchAndClaimBatch:
    def test_returns_empty_when_no_records(self):
        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[])
        conn.execute = AsyncMock()
        trans = AsyncMock(__aenter__=AsyncMock(return_value=None),
                          __aexit__=AsyncMock(return_value=False))
        conn.transaction = MagicMock(return_value=trans)
        pool = make_pool_from_conns(conn)
        result = run(fetch_and_claim_batch(pool, 10))
        assert result == []
        conn.execute.assert_not_called()

    def test_marks_records_as_taken(self):
        row = {"id": "rec-001", "tipo": "T", "cliente_id": "c",
               "phone_number": "76543210", "intentos": 0, "contexto_json": "{}"}
        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[row])
        conn.execute = AsyncMock()
        trans = AsyncMock(__aenter__=AsyncMock(return_value=None),
                          __aexit__=AsyncMock(return_value=False))
        conn.transaction = MagicMock(return_value=trans)
        pool = make_pool_from_conns(conn)
        result = run(fetch_and_claim_batch(pool, 50))
        conn.execute.assert_called_once()
        assert len(result) == 1
        assert isinstance(result[0], RawOutboxRow)

    def test_respects_batch_size_parameter(self):
        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[])
        conn.execute = AsyncMock()
        trans = AsyncMock(__aenter__=AsyncMock(return_value=None),
                          __aexit__=AsyncMock(return_value=False))
        conn.transaction = MagicMock(return_value=trans)
        pool = make_pool_from_conns(conn)
        run(fetch_and_claim_batch(pool, 25))
        assert conn.fetch.call_args.args[1] == 25


class TestFinalizeSent:
    def test_calls_finalize_ok_and_insert_log(self):
        conn = AsyncMock()
        run(finalize_sent(conn, _make_rec()))
        assert conn.execute.call_count == 2

    def test_increments_intentos(self):
        conn = AsyncMock()
        run(finalize_sent(conn, _make_rec(intentos=2)))
        first_args = conn.execute.call_args_list[0].args
        assert 3 in first_args  # intentos + 1 = 3


class TestFinalizeTypeDisabled:
    def test_calls_finalize_failed_and_log(self):
        conn = AsyncMock()
        run(finalize_type_disabled(conn, _make_raw_row()))
        assert conn.execute.call_count == 2

    def test_does_not_increment_intentos(self):
        conn = AsyncMock()
        run(finalize_type_disabled(conn, _make_raw_row(intentos=2)))
        first_args = conn.execute.call_args_list[0].args
        assert 2 in first_args  # intentos sin incrementar


class TestFinalizeNoTemplate:
    def test_calls_finalize_failed_and_log(self):
        conn = AsyncMock()
        run(finalize_no_template(conn, _make_raw_row()))
        assert conn.execute.call_count == 2

    def test_increments_intentos(self):
        conn = AsyncMock()
        run(finalize_no_template(conn, _make_raw_row(intentos=1)))
        first_args = conn.execute.call_args_list[0].args
        assert 2 in first_args  # intentos + 1


class TestReturnToPoolUntilWindow:
    def test_sets_proximo_intento(self):
        conn = AsyncMock()
        with patch("app.jobs.outbox_worker.seconds_until_next_window", return_value=3600):
            run(return_to_pool_until_window(conn, _make_raw_row(), "08:00"))
        conn.execute.assert_called_once()
        assert "3600" in str(conn.execute.call_args)

    def test_uses_calculated_seconds(self):
        conn = AsyncMock()
        with patch("app.jobs.outbox_worker.seconds_until_next_window", return_value=7200):
            run(return_to_pool_until_window(conn, _make_raw_row(), "08:00"))
        assert "7200" in str(conn.execute.call_args)


class TestReturnToPoolRateLimited:
    def test_sets_proximo_intento_with_retry_after(self):
        conn = AsyncMock()
        pool = make_pool_from_conns(conn)
        run(return_to_pool_rate_limited(pool, "oid", retry_after=120))
        assert "120" in str(conn.execute.call_args)

    def test_does_not_finalize(self):
        conn = AsyncMock()
        pool = make_pool_from_conns(conn)
        run(return_to_pool_rate_limited(pool, "oid", retry_after=60))
        assert conn.execute.call_count == 1


class TestApplyBackoffOrFail:
    def test_first_attempt_schedules_retry_returns_false(self):
        conn = AsyncMock()
        pool = make_pool_from_conns(conn)
        with patch("app.jobs.outbox_worker.get_max_attempts", return_value=4), \
             patch("app.jobs.outbox_worker.get_backoff_seconds", return_value=[60, 300, 1800, 7200]):
            result = run(apply_backoff_or_fail(pool, _make_rec(intentos=0), "timeout"))
        assert result is False
        assert conn.execute.call_count == 2
        assert "60" in str(conn.execute.call_args_list[1])

    def test_at_max_attempts_finalizes_and_returns_true(self):
        conn = AsyncMock()
        pool = make_pool_from_conns(conn)
        with patch("app.jobs.outbox_worker.get_max_attempts", return_value=4):
            result = run(apply_backoff_or_fail(pool, _make_rec(intentos=3), "fail"))
        assert result is True
        assert conn.execute.call_count == 2

    def test_second_attempt_uses_second_backoff(self):
        conn = AsyncMock()
        pool = make_pool_from_conns(conn)
        with patch("app.jobs.outbox_worker.get_max_attempts", return_value=4), \
             patch("app.jobs.outbox_worker.get_backoff_seconds", return_value=[60, 300, 1800, 7200]):
            run(apply_backoff_or_fail(pool, _make_rec(intentos=1), "err"))
        assert "300" in str(conn.execute.call_args_list[1])

    def test_error_message_truncated_to_500_chars(self):
        conn = AsyncMock()
        pool = make_pool_from_conns(conn)
        with patch("app.jobs.outbox_worker.get_max_attempts", return_value=4), \
             patch("app.jobs.outbox_worker.get_backoff_seconds", return_value=[60]):
            run(apply_backoff_or_fail(pool, _make_rec(intentos=0), "x" * 600))
        assert "x" * 501 not in str(conn.execute.call_args_list[0])
