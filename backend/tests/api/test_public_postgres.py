"""Public API behavior against the isolated migrated PostgreSQL 16 service."""

import os
from pathlib import Path
from uuid import uuid4

import asyncpg
import httpx
import pytest

from gl_api.app import create_app
from gl_api.config import Settings

pytestmark = pytest.mark.skipif(
    os.getenv("GL_API_TEST_DATABASE") != "1", reason="isolated PostgreSQL test container required"
)


def migrator_settings() -> Settings:
    return Settings(
        database_user="gl_api_migrator",
        database_password_file=Path(os.environ["GL_API_TEST_MIGRATOR_PASSWORD_FILE"]),
    )


@pytest.mark.asyncio
async def test_public_catalog_filters_orders_limits_and_hides_inactive_webinars():
    connection = await asyncpg.connect(migrator_settings().database_url())
    created_ids: list[int] = []
    prefix = f"Synthetic catalog {uuid4().hex} "
    try:
        rows = await connection.fetch(
            "INSERT INTO webinars(title, description, price_kopeks, active) "
            "SELECT $1 || n::text, 'Synthetic fixture', 100 + n, true "
            "FROM generate_series(1, 105) AS n "
            "RETURNING id, title, price_kopeks",
            prefix,
        )
        created_ids.extend(row["id"] for row in rows)
        hidden_id = await connection.fetchval(
            "INSERT INTO webinars(title, description, price_kopeks, active) "
            "VALUES ($1, 'Hidden fixture', 999, false) RETURNING id",
            prefix + "hidden",
        )
        created_ids.append(hidden_id)
        free_id = await connection.fetchval(
            "INSERT INTO webinars(title, description, price_kopeks, active) "
            "VALUES ($1, 'No price fixture', NULL, true) RETURNING id",
            prefix + "free",
        )
        created_ids.append(free_id)
        all_rows = await connection.fetch("SELECT id, active FROM webinars")
        expected_ids = sorted((row["id"] for row in all_rows if row["active"]), reverse=True)[:100]

        app = create_app(Settings(pool_max_size=2))
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client,
        ):
            assert app.state.pool.get_size() == 0
            assert app.state.pool.get_min_size() == 0
            assert app.state.pool.get_max_size() == 2
            assert (await client.get("/readyz")).json() == {"status": "ready"}
            response = await client.get("/api/webinars")
            assert response.status_code == 200
            catalog = response.json()
            assert [webinar["id"] for webinar in catalog] == expected_ids
            assert len(catalog) == 100
            assert hidden_id not in expected_ids
            assert catalog[0] == {
                "id": free_id,
                "title": prefix + "free",
                "description": "No price fixture",
                "price_kopeks": None,
            }
            detail = await client.get(f"/api/webinars/{rows[0]['id']}")
            assert detail.status_code == 200
            assert detail.json() == {
                "id": rows[0]["id"],
                "title": rows[0]["title"],
                "description": "Synthetic fixture",
                "price_kopeks": rows[0]["price_kopeks"],
            }
            assert (await client.get(f"/api/webinars/{hidden_id}")).status_code == 404
            assert (await client.get(f"/api/webinars/{max(created_ids) + 1}")).status_code == 404
            assert (await client.get("/api/webinars/0")).status_code == 422
            assert app.state.pool.get_size() <= app.state.pool.get_max_size()
        with pytest.raises(asyncpg.InterfaceError):
            async with app.state.pool.acquire():
                pass
    finally:
        if created_ids:
            await connection.execute(
                "DELETE FROM webinars WHERE id = ANY($1::bigint[])", created_ids
            )
        await connection.close()


@pytest.mark.asyncio
async def test_unreachable_database_keeps_liveness_and_returns_safe_503(caplog):
    settings = Settings(database_host="127.0.0.1", database_port=1)
    password = settings.database_password_file.read_text().strip()
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client,
    ):
        assert (await client.get("/healthz")).json() == {"status": "alive"}
        for path in ("/readyz", "/api/webinars", "/api/webinars/1"):
            response = await client.get(path)
            assert response.status_code == 503
            assert response.json() == {"detail": "database unavailable"}
            assert password not in response.text
        assert (await client.get("/healthz")).status_code == 200
    assert password not in caplog.text
    assert "127.0.0.1" not in caplog.text


@pytest.mark.asyncio
async def test_readiness_requires_catalog_schema():
    settings = Settings(database_name="postgres")
    inspector = await asyncpg.connect(
        migrator_settings().model_copy(update={"database_name": "postgres"}).database_url()
    )
    try:
        assert await inspector.fetchval("SELECT to_regclass('public.webinars')") is None
    finally:
        await inspector.close()

    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client,
    ):
        assert (await client.get("/healthz")).status_code == 200
        assert (await client.get("/readyz")).status_code == 503
        assert (await client.get("/api/webinars")).status_code == 503


@pytest.mark.asyncio
async def test_pool_exhaustion_times_out_then_recovers_without_leaking_connection(caplog):
    settings = Settings(pool_max_size=1)
    password = settings.database_password_file.read_text().strip()
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client,
    ):
        async with app.state.pool.acquire():
            assert (await client.get("/healthz")).status_code == 200
            timed_out = await client.get("/readyz")
            assert timed_out.status_code == 503
            assert timed_out.json() == {"detail": "database unavailable"}
        assert (await client.get("/readyz")).json() == {"status": "ready"}
        assert (await client.get("/api/webinars")).status_code == 200
        assert app.state.pool.get_size() == 1
    assert password not in caplog.text
