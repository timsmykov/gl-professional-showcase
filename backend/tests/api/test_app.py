from pathlib import Path

import asyncpg
import httpx
import pytest
from pydantic import ValidationError

from gl_api.app import create_app
from gl_api.config import Settings


class OfflinePool:
    def acquire(self, **_kwargs):
        raise OSError("connection unavailable")


class ClosedPool:
    def acquire(self, **_kwargs):
        raise asyncpg.InterfaceError("pool is closed")


@pytest.mark.asyncio
@pytest.mark.parametrize("pool", [OfflinePool(), ClosedPool()])
async def test_health_and_db_failure_are_safe(tmp_path: Path, caplog, pool):
    secret = tmp_path / "password"
    secret.write_text("test-only-value")
    app = create_app(Settings(database_password_file=secret))
    app.state.pool = pool
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get("/healthz")).json() == {"status": "alive"}
        for path in ("/readyz", "/api/webinars", "/api/webinars/1"):
            response = await client.get(path)
            assert response.status_code == 503
            assert response.json() == {"detail": "database unavailable"}
            assert "test-only-value" not in response.text
        assert (
            await client.post("/api/payment-events/test", json={"paid": True})
        ).status_code == 404
        assert (await client.post("/api/orders", json={"amount_kopeks": 0})).status_code == 404
    assert "test-only-value" not in caplog.text
    assert "connection unavailable" not in caplog.text
    assert "pool is closed" not in caplog.text


@pytest.mark.asyncio
async def test_catalog_rejects_invalid_ids_before_database_access(tmp_path: Path):
    secret = tmp_path / "password"
    secret.write_text("test-only-value")
    app = create_app(Settings(database_password_file=secret))
    app.state.pool = OfflinePool()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        for value in ("0", "-1", str(2**63), "abc", "1%20OR%201=1"):
            assert (await client.get(f"/api/webinars/{value}")).status_code == 422


def test_password_is_url_encoded_and_not_in_repr(tmp_path: Path):
    secret = tmp_path / "password"
    secret.write_text("a:/@?#")
    settings = Settings(database_password_file=secret)
    assert "a%3A%2F%40%3F%23" in settings.database_url()
    assert "a:/@?#" not in repr(settings)


def test_settings_reject_invalid_pool_and_empty_password(tmp_path: Path):
    with pytest.raises(ValidationError):
        Settings(pool_max_size=0)
    with pytest.raises(ValidationError):
        Settings(pool_max_size=21)
    with pytest.raises(ValidationError):
        Settings(database_port=0)
    secret = tmp_path / "empty-password"
    secret.write_text("\n")
    with pytest.raises(ValueError, match="database password file is empty"):
        Settings(database_password_file=secret).database_url()
