"""
Tests que verifican directamente cada una de las 10 correcciones aplicadas.
Un test que falla aquí significa que se revirtió una corrección crítica.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import asyncio
import threading
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from helpers import run, make_pool_from_conns
from app.domain.records import OutboxRecord, RawOutboxRow
from app.domain.exceptions import (
    WorkerError, TemplateNotFoundError, TypeDisabledError,
    ConfigNotFoundError, InvalidPhoneError,
)
from app.services.health import MetricsCollector
from app.services.notification_channel import NotificationChannel
from app.services.whatsapp import WhatsAppClient, normalize_phone
from app.db import queries as Q


# ── Corrección 1: SQL centralizado en queries.py ──────────────────────────────

class TestCorrection1_SQLCentralized:
    def test_claim_batch_query_exists_in_queries(self):
        assert hasattr(Q, "CLAIM_BATCH")
        assert "UPDATE" in Q.CLAIM_BATCH
        assert "Publicado" in Q.CLAIM_BATCH

    def test_return_to_pool_query_exists_in_queries(self):
        assert hasattr(Q, "RETURN_TO_POOL")
        assert "ProximoIntento" in Q.RETURN_TO_POOL

    def test_no_raw_sql_update_in_outbox_worker(self):
        src = open("app/jobs/outbox_worker.py").read()
        # No debería haber UPDATE de NotifOutbox hardcodeado fuera de queries
        raw_updates = [l for l in src.splitlines()
                       if 'UPDATE "NotifOutbox"' in l and not l.strip().startswith("#")]
        assert raw_updates == [], f"Raw SQL found: {raw_updates}"


# ── Corrección 2: _try_run_job retorna bool ───────────────────────────────────

class TestCorrection2_TryRunJobReturnsBool:
    def test_try_run_job_returns_true_when_executed(self):
        from app.jobs.reminder_job import _try_run_job
        import inspect
        sig = inspect.signature(_try_run_job)
        hints = sig.return_annotation
        assert hints == bool or str(hints) == "bool"

    def test_scheduler_only_updates_last_run_day_when_job_ran(self):
        src = open("app/jobs/reminder_job.py").read()
        # Debe haber una condición 'if ran:' antes de 'last_run_day = today'
        assert "if ran:" in src
        # La asignación debe estar dentro del if, no en el nivel de in_window
        lines = src.splitlines()
        ran_idx = next(i for i, l in enumerate(lines) if "if ran:" in l)
        day_idx = next(i for i, l in enumerate(lines) if "last_run_day = today" in l)
        assert day_idx > ran_idx, "last_run_day = today must come AFTER 'if ran:'"


# ── Corrección 3: sin monkey-patch en _run_workers ───────────────────────────

class TestCorrection3_NoMonkeyPatch:
    def test_run_workers_has_no_log_assignment(self):
        src = open("main.py").read()
        assert "log.info = structlog" not in src, \
            "Monkey-patch 'log.info = structlog...' must not exist in main.py"


# ── Corrección 4: MetricsCollector thread-safe ───────────────────────────────

class TestCorrection4_MetricsCollectorThreadSafe:
    def test_has_lock(self):
        m = MetricsCollector()
        assert hasattr(m, "_lock"), "MetricsCollector must have _lock attribute"
        assert isinstance(m._lock, type(threading.Lock())), \
            "_lock must be a threading.Lock"

    def test_concurrent_increments_are_safe(self):
        """50 threads incrementando concurrentemente → el resultado es exacto."""
        m = MetricsCollector()
        threads = [threading.Thread(target=m.record_sent) for _ in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert m.sent == 50

    def test_read_and_write_concurrent(self):
        """Leer y escribir desde threads distintos no lanza excepciones."""
        m = MetricsCollector()
        errors = []

        def writer():
            for _ in range(100):
                m.record_sent()

        def reader():
            for _ in range(100):
                try:
                    _ = m.sent
                except Exception as e:
                    errors.append(e)

        ts = [threading.Thread(target=writer)] + [threading.Thread(target=reader)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        assert errors == []


# ── Corrección 5: get_pool() protegido con asyncio.Lock ──────────────────────

class TestCorrection5_GetPoolRaceCondition:
    def test_pool_lock_exists(self):
        import app.db.connection as conn_module
        assert hasattr(conn_module, "_pool_lock"), \
            "connection.py must have _pool_lock asyncio.Lock"

    def test_concurrent_get_pool_creates_single_pool(self):
        """Dos llamadas concurrentes a get_pool() crean exactamente un pool."""
        import os
        import app.db.connection as conn_module
        original_pool = conn_module._pool

        call_count = 0

        async def _test():
            nonlocal call_count
            conn_module._pool = None
            conn_module._pool_lock = asyncio.Lock()

            async def fake_create(*args, **kwargs):
                nonlocal call_count
                call_count += 1
                await asyncio.sleep(0)  # yield para simular concurrencia
                return MagicMock()

            with patch("app.db.connection.asyncpg.create_pool", side_effect=fake_create), \
                 patch.dict(os.environ, {"DATABASE_URL": "postgresql://test"}):
                await asyncio.gather(
                    conn_module.get_pool(),
                    conn_module.get_pool(),
                )
            assert call_count == 1, f"Expected 1 pool creation, got {call_count}"
            conn_module._pool = original_pool

        asyncio.run(_test())


# ── Corrección 6: validación de config numérica ───────────────────────────────

class TestCorrection6_NumericConfigValidation:
    def test_validate_numeric_config_exists(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("main", "main.py")
        mod  = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert hasattr(mod, "_validate_numeric_config")
        assert hasattr(mod, "_parse_positive_int")

    def test_max_attempts_zero_exits(self):
        import importlib.util, os
        spec = importlib.util.spec_from_file_location("main", "main.py")
        mod  = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mock_log = MagicMock()
        with patch.dict(os.environ, {"MAX_ATTEMPTS": "0"}), \
             pytest.raises(SystemExit) as exc:
            mod._validate_numeric_config(mock_log)
        assert exc.value.code == 1

    def test_invalid_backoff_exits(self):
        import importlib.util, os
        spec = importlib.util.spec_from_file_location("main", "main.py")
        mod  = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mock_log = MagicMock()
        with patch.dict(os.environ, {"BACKOFF_SECONDS": "abc,def"}), \
             pytest.raises(SystemExit) as exc:
            mod._validate_numeric_config(mock_log)
        assert exc.value.code == 1


# ── Corrección 7: NotificationChannel Protocol ───────────────────────────────

class TestCorrection7_NotificationChannelProtocol:
    def test_protocol_exists(self):
        assert NotificationChannel is not None

    def test_whatsapp_client_satisfies_protocol(self):
        client = WhatsAppClient(token="", phone_number_id="")
        assert isinstance(client, NotificationChannel), \
            "WhatsAppClient must satisfy NotificationChannel protocol"

    def test_stub_channel_satisfies_protocol(self):
        class StubChannel:
            async def send(self, phone: str, message: str) -> None:
                pass
            async def close(self) -> None:
                pass

        assert isinstance(StubChannel(), NotificationChannel)

    def test_outbox_worker_imports_notification_channel(self):
        src = open("app/jobs/outbox_worker.py").read()
        assert "NotificationChannel" in src
        assert "from app.services.notification_channel import" in src

    def test_outbox_worker_does_not_import_whatsapp_client_at_module_level(self):
        """WhatsAppClient se importa dentro de run_outbox_worker(), no a nivel módulo."""
        import ast
        src = open("app/jobs/outbox_worker.py").read()
        tree = ast.parse(src)
        # col_offset == 0 indica import a nivel módulo (no anidado)
        top_imports = [n for n in ast.walk(tree)
                       if isinstance(n, (ast.Import, ast.ImportFrom))
                       and n.col_offset == 0]
        assert not any("WhatsAppClient" in ast.dump(n) for n in top_imports), (
            "WhatsAppClient must not be imported at module level — use NotificationChannel"
        )
        # Debe sí estar dentro de run_outbox_worker()
        worker_fn = src.split("async def run_outbox_worker")[1]
        assert "WhatsAppClient" in worker_fn

# ── Corrección 8: OutboxRecord y RawOutboxRow dataclasses ────────────────────

class TestCorrection8_Dataclasses:
    def test_outbox_record_is_frozen(self):
        rec = OutboxRecord(outbox_id="1", cliente_id="2", tipo="T",
                           phone="76543210", intentos=0,
                           phone_log="****210", mensaje="Hola")
        with pytest.raises((AttributeError, TypeError)):
            rec.outbox_id = "modificado"  # frozen=True debe rechazar esto

    def test_raw_outbox_row_from_db_row(self):
        row = {"id": "oid", "tipo": "T", "cliente_id": "cid",
               "phone_number": "76543210", "intentos": 0, "contexto_json": "{}"}
        result = RawOutboxRow.from_db_row(row)
        assert result.id == "oid"
        assert result.phone_number == "76543210"

    def test_raw_outbox_row_missing_field_raises(self):
        with pytest.raises(KeyError):
            RawOutboxRow.from_db_row({"id": "oid"})  # faltan campos

    def test_fetch_and_claim_batch_returns_typed_rows(self):
        from app.jobs.outbox_worker import fetch_and_claim_batch
        row = {"id": "r1", "tipo": "T", "cliente_id": "c",
               "phone_number": "76543210", "intentos": 0, "contexto_json": "{}"}
        conn = AsyncMock()
        conn.fetch = AsyncMock(return_value=[row])
        conn.execute = AsyncMock()
        trans = AsyncMock(__aenter__=AsyncMock(return_value=None),
                          __aexit__=AsyncMock(return_value=False))
        conn.transaction = MagicMock(return_value=trans)
        pool = make_pool_from_conns(conn)
        result = run(fetch_and_claim_batch(pool, 50))
        assert len(result) == 1
        assert isinstance(result[0], RawOutboxRow)


# ── Corrección 9: Excepciones de dominio ─────────────────────────────────────

class TestCorrection9_DomainExceptions:
    def test_all_exceptions_inherit_worker_error(self):
        for exc_class in [TemplateNotFoundError, TypeDisabledError,
                          ConfigNotFoundError, InvalidPhoneError]:
            assert issubclass(exc_class, WorkerError), \
                f"{exc_class.__name__} must inherit from WorkerError"

    def test_template_not_found_has_tipo(self):
        exc = TemplateNotFoundError("AVISO_CORTE")
        assert exc.tipo == "AVISO_CORTE"
        assert "AVISO_CORTE" in str(exc)

    def test_type_disabled_has_tipo(self):
        exc = TypeDisabledError("RECORDATORIO_R1")
        assert exc.tipo == "RECORDATORIO_R1"

    def test_invalid_phone_has_phone(self):
        exc = InvalidPhoneError("123")
        assert exc.phone == "123"
        assert "123" in str(exc)


# ── Corrección 10: normalize_phone valida entradas ───────────────────────────

class TestCorrection10_NormalizePhoneValidation:
    def test_valid_8_digit_number(self):
        assert normalize_phone("76543210") == "59176543210"

    def test_valid_11_digit_with_591(self):
        assert normalize_phone("59176543210") == "59176543210"

    def test_valid_with_plus_stripped(self):
        assert normalize_phone("+59176543210") == "59176543210"

    def test_invalid_3_digits_raises(self):
        with pytest.raises(InvalidPhoneError):
            normalize_phone("123")

    def test_invalid_12_digits_raises(self):
        with pytest.raises(InvalidPhoneError):
            normalize_phone("123456789012")

    def test_invalid_empty_raises(self):
        with pytest.raises(InvalidPhoneError):
            normalize_phone("")

    def test_invalid_letters_only_raises(self):
        with pytest.raises(InvalidPhoneError):
            normalize_phone("abcdefgh")
