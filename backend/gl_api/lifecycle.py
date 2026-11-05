"""Bounded database waits and shutdown; operational logs contain event names only."""

import asyncio
import logging

import asyncpg
from fastapi import HTTPException, Request

ACQUIRE_TIMEOUT_SECONDS = 3
READINESS_TIMEOUT_SECONDS = 3
POOL_CLOSE_TIMEOUT_SECONDS = 5
logger = logging.getLogger(__name__)


class DatabasePool:
    """Bound acquisition as well as queries for the API's pool convenience methods."""

    def __init__(self, pool: asyncpg.Pool, acquire_timeout: float = ACQUIRE_TIMEOUT_SECONDS):
        self._pool = pool
        self.acquire_timeout = acquire_timeout

    def acquire(self, *, timeout: float | None = None):
        return self._pool.acquire(timeout=self.acquire_timeout if timeout is None else timeout)

    async def _query(self, method: str, query: str, *args, **kwargs):
        async with self.acquire() as connection:
            return await getattr(connection, method)(query, *args, **kwargs)

    async def fetch(self, query: str, *args, **kwargs):
        return await self._query("fetch", query, *args, **kwargs)

    async def fetchrow(self, query: str, *args, **kwargs):
        return await self._query("fetchrow", query, *args, **kwargs)

    async def fetchval(self, query: str, *args, **kwargs):
        return await self._query("fetchval", query, *args, **kwargs)

    async def execute(self, query: str, *args, **kwargs):
        return await self._query("execute", query, *args, **kwargs)

    def get_size(self):
        return self._pool.get_size()

    def get_min_size(self):
        return self._pool.get_min_size()

    def get_max_size(self):
        return self._pool.get_max_size()


async def require_serving(request: Request):
    if request.app.state.stopping:
        raise HTTPException(status_code=503, detail="service stopping")


def terminate_pool(pool: asyncpg.Pool):
    try:
        pool.terminate()
    except Exception:  # noqa: BLE001 - cleanup errors must not expose driver inputs.
        raise RuntimeError("database pool termination failed") from None


async def close_pool(pool: asyncpg.Pool, timeout: float | None = None):
    try:
        async with asyncio.timeout(POOL_CLOSE_TIMEOUT_SECONDS if timeout is None else timeout):
            await pool.close()
    except asyncio.CancelledError:
        terminate_pool(pool)
        raise
    except TimeoutError:
        logger.warning("database_pool_close_timeout")
        terminate_pool(pool)
    except Exception:  # noqa: BLE001 - final cleanup boundary, no private driver exception.
        logger.error("database_pool_close_failed")
        terminate_pool(pool)
