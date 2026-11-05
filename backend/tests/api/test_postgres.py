import asyncio
import os
import socket
from datetime import datetime
from pathlib import Path

import aiohttp
import asyncpg
import httpx
import pytest
import uvicorn

from gl_api.app import create_app
from gl_api.config import Settings
from gl_bot.api_repository import ApiRepository, BackendError

pytestmark = pytest.mark.skipif(
    os.getenv("GL_API_TEST_DATABASE") != "1", reason="isolated PostgreSQL test container required"
)


def migrator_settings() -> Settings:
    return Settings(
        database_user="gl_api_migrator",
        database_password_file=Path(os.environ["GL_API_TEST_MIGRATOR_PASSWORD_FILE"]),
    )


@pytest.mark.asyncio
async def test_migrated_schema_catalog_and_permissions():
    settings = Settings()
    app_pool = await asyncpg.create_pool(dsn=settings.database_url(), min_size=1, max_size=2)
    migrator_pool = await asyncpg.create_pool(
        dsn=migrator_settings().database_url(), min_size=1, max_size=2
    )
    webinar_id = None
    try:
        async with app_pool.acquire() as conn:
            assert not await conn.fetchval(
                "SELECT has_table_privilege(current_user, 'public.webinars', 'INSERT')"
            )
            assert await conn.fetchval(
                "SELECT has_table_privilege(current_user, 'public.webinars', 'SELECT')"
            )
            assert await conn.fetchval(
                "SELECT has_table_privilege(current_user, 'public.purchase_orders', 'SELECT')"
            )
            assert not await conn.fetchval(
                "SELECT has_schema_privilege(current_user, 'public', 'CREATE')"
            )
        async with migrator_pool.acquire() as conn:
            assert await conn.fetchval("SELECT version_num FROM alembic_version") == (
                "0011_recording_codes"
            )
            assert await conn.fetchval("SELECT to_regclass('public.processed_updates')")
            webinar_id = await conn.fetchval(
                "INSERT INTO webinars(title, description, price_kopeks, active) "
                "VALUES ($1, $2, $3, true) RETURNING id",
                "Synthetic catalog item",
                "Only a test fixture",
                12300,
            )
        app = create_app(settings)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client,
        ):
            assert (await client.get("/readyz")).status_code == 200
            detail = await client.get(f"/api/webinars/{webinar_id}")
            assert detail.status_code == 200
            assert detail.json()["price_kopeks"] == 12300
            assert any(
                row["id"] == webinar_id for row in (await client.get("/api/webinars")).json()
            )
            assert (await client.get("/api/webinars/0")).status_code == 422
            assert (await client.get("/api/webinars/1%20OR%201=1")).status_code == 422
        async with migrator_pool.acquire() as conn:
            assert await conn.fetchval("SELECT to_regclass('public.purchase_orders')")
            assert await conn.fetchval("SELECT to_regclass('public.payment_events')")
    finally:
        if webinar_id is not None:
            await migrator_pool.execute("DELETE FROM webinars WHERE id = $1", webinar_id)
        await migrator_pool.close()
        await app_pool.close()


@pytest.mark.asyncio
async def test_payment_event_constraint_with_synthetic_records():
    settings = migrator_settings()
    connection = await asyncpg.connect(settings.database_url())
    transaction = connection.transaction()
    await transaction.start()
    try:
        user_id = await connection.fetchval(
            "INSERT INTO users(telegram_id) VALUES ($1) RETURNING id", -9_000_000_000_000_001
        )
        webinar_id = await connection.fetchval(
            "INSERT INTO webinars(title, price_kopeks) VALUES ($1, $2) RETURNING id",
            "Synthetic",
            100,
        )
        order_id = await connection.fetchval(
            "INSERT INTO purchase_orders(user_id, webinar_id, amount_kopeks) "
            "VALUES ($1, $2, $3) RETURNING id",
            user_id,
            webinar_id,
            100,
        )
        await connection.execute(
            "INSERT INTO payment_events(provider, provider_event_id, order_id, event_type) "
            "VALUES ($1, $2, $3, $4)",
            "synthetic",
            "event-1",
            order_id,
            "confirmed",
        )
        with pytest.raises(asyncpg.UniqueViolationError):
            async with connection.transaction():
                await connection.execute(
                    "INSERT INTO payment_events(provider, provider_event_id, order_id, event_type) "
                    "VALUES ($1, $2, $3, $4)",
                    "synthetic",
                    "event-1",
                    order_id,
                    "confirmed",
                )
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM payment_events WHERE order_id = $1", order_id
            )
            == 1
        )
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM webinar_entitlements WHERE user_id = $1", user_id
            )
            == 0
        )
    finally:
        await transaction.rollback()
        await connection.close()


@pytest.mark.asyncio
async def test_internal_bot_operations_use_api_role_and_migrated_database(tmp_path: Path):
    token_file = tmp_path / "internal_token"
    token_file.write_text("synthetic-internal-token-1234567890")
    app = create_app(Settings(bot_token_file=token_file))
    migrator = await asyncpg.connect(migrator_settings().database_url())
    synthetic_uid = -9_000_000_000_000_099
    headers = {"Authorization": "Bearer synthetic-internal-token-1234567890"}
    try:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client,
        ):

            async def call(operation: str, *args):
                response = await client.post(
                    f"/internal/bot/{operation}", json={"args": list(args)}, headers=headers
                )
                assert response.status_code == 200, response.text
                return response.json()["result"]

            user = await call("get_user", synthetic_uid)
            assert user["telegram_id"] == synthetic_uid
            await call("select_section", user["id"], "pro")
            assert await call("has_section", user["id"], "pro") is True
            claim = await call("claim_update", 9_000_000_000_000_099)
            assert claim["status"] == "claimed"
            assert await call("complete_update", 9_000_000_000_000_099, claim["lease"])
            assert await call("claim_update", 9_000_000_000_000_099) == "done"
            assert await call("list_webinars") == []
            assert await call("list_entitlements", user["id"]) == []
    finally:
        await migrator.execute(
            "DELETE FROM processed_updates WHERE update_id = $1", 9_000_000_000_000_099
        )
        await migrator.execute("DELETE FROM users WHERE telegram_id = $1", synthetic_uid)
        await migrator.close()


@pytest.mark.asyncio
async def test_bot_http_adapter_round_trip_with_migrated_database(tmp_path: Path):
    token_file = tmp_path / "internal_token"
    token_file.write_text("synthetic-internal-token-1234567890")
    app = create_app(Settings(bot_token_file=token_file))
    migrator = await asyncpg.connect(migrator_settings().database_url())
    synthetic_uid = -9_000_000_000_000_199
    service_id = slot_id = booking_id = None
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    server = uvicorn.Server(
        uvicorn.Config(app, log_level="error", access_log=False, lifespan="on")
    )
    server_task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        for _ in range(100):
            if server.started:
                break
            if server_task.done():
                await server_task
            await asyncio.sleep(0.05)
        assert server.started
        service_id = await migrator.fetchval(
            "INSERT INTO services(title, active) VALUES ($1, true) RETURNING id",
            "Synthetic integration service",
        )
        slot_id = await migrator.fetchval(
            "INSERT INTO appointment_slots(service_id, starts_at, ends_at) "
            "VALUES ($1, now() + interval '5 days', now() + interval '5 days 1 hour') RETURNING id",
            service_id,
        )
        base_url = f"http://127.0.0.1:{listener.getsockname()[1]}"
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session:
            repo = ApiRepository(session, base_url, token_file.read_text())
            assert await repo.health() is None
            user = await repo.get_user(synthetic_uid)
            assert user["telegram_id"] == synthetic_uid
            await repo.select_section(user["id"], "client")
            assert await repo.has_section(user["id"], "client") is True
            slots = await repo.list_slots(service_id)
            assert len(slots) == 1
            assert isinstance(slots[0]["starts_at"], datetime)
            created = await repo.create_booking(user["id"], slot_id, 9_000_000_000_000_199)
            booking_id = created["id"]
            booking = await repo.get_booking(booking_id)
            assert booking["telegram_id"] == synthetic_uid
            assert isinstance(booking["starts_at"], datetime)
            assert (await repo.list_bookings(user["id"]))[0]["id"] == booking_id
            with pytest.raises(BackendError, match="backend status 401"):
                await ApiRepository(session, base_url, "wrong-token").health()
            with pytest.raises(ValueError, match="invalid bot repository call"):
                await repo.select_section(user["id"], "admin")
    finally:
        server.should_exit = True
        await server_task
        listener.close()
        if booking_id is not None:
            await migrator.execute("DELETE FROM booking_requests WHERE id = $1", booking_id)
        if slot_id is not None:
            await migrator.execute("DELETE FROM appointment_slots WHERE id = $1", slot_id)
        if service_id is not None:
            await migrator.execute("DELETE FROM services WHERE id = $1", service_id)
        await migrator.execute("DELETE FROM users WHERE telegram_id = $1", synthetic_uid)
        await migrator.close()
