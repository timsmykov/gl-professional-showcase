import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID

import httpx
import pytest

from gl_api.app import create_app
from gl_api.config import Settings
from gl_api.lifecycle import close_pool

CANARY = "synthetic-private-lifecycle-value"
TOKEN = "synthetic-internal-token-1234567890"


class Pool:
    def __init__(self, delay=0):
        self.delay = delay
        self.queries = 0
        self.closed = False
        self.terminated = False
        self.close_started = asyncio.Event()
        self.hang = False
        self.fail_close = False

    @asynccontextmanager
    async def acquire(self, **_kwargs):
        yield self

    async def fetchval(self, *_args, **_kwargs):
        self.queries += 1
        await asyncio.sleep(self.delay)
        return 1

    async def close(self):
        self.close_started.set()
        if self.hang:
            await asyncio.Event().wait()
        if self.fail_close:
            raise RuntimeError(CANARY)
        self.closed = True

    def terminate(self):
        self.terminated = True


def app_settings(tmp_path: Path):
    password = tmp_path / "password"
    password.write_text(CANARY)
    token = tmp_path / "token"
    token.write_text(TOKEN)
    return Settings(database_password_file=password, bot_token_file=token)


@pytest.mark.asyncio
async def test_readiness_has_one_deadline_for_all_schema_checks(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr("gl_api.app.READINESS_TIMEOUT_SECONDS", 0.03)
    app = create_app(app_settings(tmp_path))
    pool = Pool(delay=0.02)
    app.state.pool = pool
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        async with asyncio.timeout(0.5):
            response = await client.get("/readyz")
        assert response.status_code == 503
        assert response.json() == {"detail": "database unavailable"}
        assert pool.queries < 13
        pool.delay = 0
        assert (await client.get("/readyz")).status_code == 200
        assert (await client.get("/healthz")).status_code == 200
    assert CANARY not in caplog.text


@pytest.mark.asyncio
async def test_pool_timeout_forces_termination_and_hides_errors(caplog):
    pool = Pool()
    pool.hang = True
    async with asyncio.timeout(0.5):
        await close_pool(pool, timeout=0.01)
    assert pool.terminated and not pool.closed
    assert "database_pool_close_timeout" in caplog.text


@pytest.mark.asyncio
async def test_pool_close_failure_forces_termination_without_private_text(caplog):
    pool = Pool()
    pool.fail_close = True
    await close_pool(pool)
    assert pool.terminated
    assert "database_pool_close_failed" in caplog.text
    assert CANARY not in caplog.text


@pytest.mark.asyncio
async def test_cancelled_shutdown_terminates_pool_and_preserves_cancellation():
    pool = Pool()
    pool.hang = True
    task = asyncio.create_task(close_pool(pool))
    await pool.close_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert pool.terminated


@pytest.mark.asyncio
async def test_start_stop_closes_owned_pool_and_rejects_new_database_work(tmp_path, monkeypatch):
    pool = Pool()

    async def factory(**_kwargs):
        return pool

    monkeypatch.setattr("gl_api.app.asyncpg.create_pool", factory)
    app = create_app(app_settings(tmp_path))
    async with app.router.lifespan_context(app):
        assert not app.state.stopping
    assert pool.closed and not pool.terminated
    before = pool.queries
    headers = {"Authorization": f"Bearer {TOKEN}"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get("/healthz")).status_code == 200
        for path in ["/readyz", "/api/webinars", "/api/webinars/1"]:
            assert (await client.get(path)).status_code == 503
        assert (await client.post("/internal/bot/health", json={"args": []})).status_code == 401
        assert (
            await client.post("/internal/bot/health", json={"args": []}, headers=headers)
        ).status_code == 503
        response = await client.post(
            "/internal/order-intents",
            json={"telegram_id": 1, "webinar_id": 1, "source_update_id": 1},
            headers=headers,
        )
        assert response.status_code == 503
    assert pool.queries == before


@pytest.mark.asyncio
async def test_startup_failure_is_sanitized(tmp_path, monkeypatch, caplog):
    async def factory(**_kwargs):
        raise RuntimeError(CANARY)

    monkeypatch.setattr("gl_api.app.asyncpg.create_pool", factory)
    app = create_app(app_settings(tmp_path))
    with pytest.raises(RuntimeError, match="^API startup failed$"):
        async with app.router.lifespan_context(app):
            pytest.fail("failed startup entered serving state")
    assert app.state.stopping
    assert CANARY not in caplog.text


@pytest.mark.asyncio
async def test_request_ids_are_generated_not_taken_from_input(tmp_path, caplog):
    app = create_app(app_settings(tmp_path))

    @app.get("/synthetic-failure")
    async def fail():
        raise RuntimeError(CANARY)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        responses = [
            await client.get("/healthz", headers={"X-Request-ID": CANARY}),
            await client.post("/internal/bot/health", json={"args": []}),
            await client.get("/synthetic-failure", headers={"X-Request-ID": CANARY}),
        ]
    ids = [response.headers["x-request-id"] for response in responses]
    assert len(set(ids)) == 3
    assert all(UUID(value).version == 4 for value in ids)
    assert ids[-1] in caplog.text
    assert CANARY not in caplog.text
