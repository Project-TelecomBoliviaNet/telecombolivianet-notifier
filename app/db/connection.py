"""
Gestión del pool de conexiones a PostgreSQL (asyncpg).
El Worker comparte la misma base de datos que el monolito .NET.
No existe ninguna llamada directa entre procesos — la comunicación
es exclusivamente a través de las tablas notif_outbox y notif_config.
"""
import asyncio
import os
import asyncpg
import structlog

log = structlog.get_logger(__name__)

_pool: asyncpg.Pool | None = None
_pool_lock = asyncio.Lock()


async def get_pool() -> asyncpg.Pool:
    """
    Retorna el pool singleton de conexiones a PostgreSQL.
    Usa asyncio.Lock para evitar la race condition en la que dos corutinas
    concurrentes pueden ver _pool=None simultáneamente y crear dos pools.
    """
    global _pool
    async with _pool_lock:
        if _pool is None:
            dsn = os.environ["DATABASE_URL"]
            _pool = await asyncpg.create_pool(
                dsn,
                min_size=2,
                max_size=10,
                command_timeout=30,
            )
            log.info("db.pool_created")
    return _pool


async def close_pool() -> None:
    """Cierra el pool y libera todas las conexiones."""
    global _pool
    async with _pool_lock:
        if _pool:
            await _pool.close()
            _pool = None
            log.info("db.pool_closed")
