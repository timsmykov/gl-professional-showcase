"""Real pool exhaustion and forced shutdown against disposable PostgreSQL 16."""

import asyncio
import os

import asyncpg
import httpx
import pytest

from gl_api.app import create_app
from gl_api.config import Settings
from gl_api.lifecycle import DatabasePool, close_pool

pytestmark = pytest.mark.skipif(
    os.getenv("GL_API_TEST_DATABASE") != "1", reason="isolated PostgreSQL test container required"
)


@pytest.mark.asyncio
async def test_exhausted_pool_wait_is_bounded_and_recovers(tmp_path):
    pool = await asyncpg.create_pool(Settings().database_url(), min_size=0, max_size=1)
    bounded = DatabasePool(pool, acquire_timeout=0.03)
    connection = await pool.acquire()
    try:
        async with asyncio.timeout(0.5):
            with pytest.raises(TimeoutError):
                await bounded.fetchval("SELECT 1")
        token = tmp_path / "token"
        token.write_text("synthetic-internal-token-1234567890")
        app = create_app(Settings(bot_token_file=token))
        app.state.pool = bounded
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            async with asyncio.timeout(0.5):
                response = await client.post(
                    "/internal/bot/get_user",
                    json={"args": [-9_800_000_777_001]},
                    headers={"Authorization": "Bearer synthetic-internal-token-1234567890"},
                )
            assert response.status_code == 503
            assert response.json() == {"detail": "database unavailable"}
        await pool.release(connection)
        assert await bounded.fetchval("SELECT 1") == 1
    finally:
        await pool.release(connection)
        await close_pool(pool)


@pytest.mark.asyncio
async def test_shutdown_terminates_real_held_connection(caplog):
    pool = await asyncpg.create_pool(Settings().database_url(), min_size=0, max_size=1)
    connection = await pool.acquire()
    try:
        await connection.execute("BEGIN")
        await connection.execute("INSERT INTO users(telegram_id) VALUES ($1)", -9_800_000_777_002)
        async with asyncio.timeout(0.5):
            await close_pool(pool, timeout=0.03)
        with pytest.raises(asyncpg.InterfaceError):
            await connection.fetchval("SELECT 1")
        assert "database_pool_close_timeout" in caplog.text
        verification = await asyncpg.connect(Settings().database_url(), command_timeout=3)
        try:
            assert (
                await verification.fetchval(
                    "SELECT count(*) FROM users WHERE telegram_id = $1", -9_800_000_777_002
                )
                == 0
            )
            # Reuse proves the old uncommitted insert released its unique-key lock.
            await verification.execute("BEGIN")
            await verification.execute(
                "INSERT INTO users(telegram_id) VALUES ($1)", -9_800_000_777_002
            )
            await verification.execute("ROLLBACK")
        finally:
            await verification.close()
    finally:
        await pool.release(connection)
        await close_pool(pool)
