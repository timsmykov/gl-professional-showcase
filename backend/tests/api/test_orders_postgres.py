"""Synthetic order-intent invariants on the isolated PostgreSQL 16 test DB."""

import asyncio
import os
import socket
from pathlib import Path
from uuid import uuid4

import aiohttp
import asyncpg
import httpx
import pytest
import uvicorn

from gl_api.app import create_app
from gl_api.config import Settings
from gl_api.orders import OrderRepository, OrderRequestConflict, OrderUnavailable
from gl_bot.api_repository import ApiRepository, BackendError

pytestmark = pytest.mark.skipif(
    os.getenv("GL_API_TEST_DATABASE") != "1", reason="isolated PostgreSQL test container required"
)

TOKEN = "synthetic-internal-token-1234567890"


@pytest.mark.asyncio
async def test_bot_http_order_path_is_authenticated_idempotent_and_unpaid(tmp_path: Path, caplog):
    token_file = tmp_path / "internal-token"
    token_file.write_text(TOKEN)
    migrator = await asyncpg.connect(
        Settings(
            database_user="gl_api_migrator",
            database_password_file=Path(os.environ["GL_API_TEST_MIGRATOR_PASSWORD_FILE"]),
        ).database_url()
    )
    telegram_id = -int.from_bytes(uuid4().bytes[:7], "big")
    update_id = -telegram_id + 100
    user_id = webinar_id = None
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    api = create_app(Settings(bot_token_file=token_file))
    server = uvicorn.Server(
        uvicorn.Config(api, log_level="error", access_log=False, lifespan="on")
    )
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        for _ in range(100):
            if server.started:
                break
            if task.done():
                await task
            await asyncio.sleep(0.05)
        assert server.started
        user_id = await migrator.fetchval(
            "INSERT INTO users(telegram_id) VALUES ($1) RETURNING id", telegram_id
        )
        webinar_id = await migrator.fetchval(
            "INSERT INTO webinars(title, price_kopeks, active) "
            "VALUES ('Synthetic order HTTP', 12000, true) RETURNING id"
        )
        base_url = f"http://127.0.0.1:{listener.getsockname()[1]}"
        async with (
            aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session,
            httpx.AsyncClient(base_url=base_url) as client,
        ):
            bot = ApiRepository(session, base_url, TOKEN)
            path = "/internal/order-intents"
            body = {
                "telegram_id": telegram_id,
                "webinar_id": webinar_id,
                "source_update_id": update_id,
            }
            assert (await client.post(path, json=body)).status_code == 401
            first = await bot.create_order_intent(telegram_id, webinar_id, update_id)
            assert first["amount_kopeks"] == 12000
            assert first["currency"] == "RUB"
            assert first["status"] == "pending"
            assert "user_id" not in first and "payment_url" not in first
            assert (
                await migrator.fetchval(
                    "SELECT amount_kopeks FROM purchase_orders WHERE id = $1", first["id"]
                )
                == 12000
            )
            await migrator.execute(
                "UPDATE webinars SET price_kopeks = 15000 WHERE id = $1", webinar_id
            )
            assert await bot.create_order_intent(telegram_id, webinar_id, update_id) == first
            concurrent = await asyncio.gather(
                bot.create_order_intent(telegram_id, webinar_id, update_id + 1),
                bot.create_order_intent(telegram_id, webinar_id, update_id + 1),
            )
            assert concurrent[0] == concurrent[1]
            assert concurrent[0] == first
            assert await bot.create_order_intent(telegram_id, webinar_id, update_id + 2) == first
            assert (
                await migrator.fetchval(
                    "SELECT count(*) FROM purchase_orders WHERE user_id = $1", user_id
                )
                == 1
            )
            assert (
                await migrator.fetchval(
                    "SELECT count(*) FROM order_intent_requests WHERE order_id = $1", first["id"]
                )
                == 3
            )
            for changed in (
                {**body, "webinar_id": webinar_id + 1},
                {**body, "telegram_id": telegram_id - 1},
            ):
                response = await client.post(
                    path, json=changed, headers={"Authorization": f"Bearer {TOKEN}"}
                )
                assert response.status_code == 409
                assert response.json() == {"detail": "order request conflict"}
            assert (
                await client.post(
                    path,
                    json={**body, "source_update_id": update_id + 3, "webinar_id": webinar_id + 1},
                    headers={"Authorization": f"Bearer {TOKEN}"},
                )
            ).status_code == 404
            with pytest.raises(BackendError, match="backend status 404"):
                await bot.create_order_intent(telegram_id - 1, webinar_id, update_id + 3)
            await migrator.execute(
                "UPDATE purchase_orders SET status = 'cancelled' WHERE id = $1", first["id"]
            )
            replacement = await bot.create_order_intent(telegram_id, webinar_id, update_id + 4)
            assert replacement["id"] != first["id"]
            assert replacement["amount_kopeks"] == 15000
            assert (
                await migrator.fetchval(
                    "SELECT count(*) FROM purchase_orders WHERE user_id = $1", user_id
                )
                == 2
            )
            assert await migrator.fetchval("SELECT count(*) FROM payment_events") == 0
            assert (
                await migrator.fetchval(
                    "SELECT count(*) FROM webinar_entitlements WHERE user_id = $1", user_id
                )
                == 0
            )
        assert TOKEN not in caplog.text
        assert str(telegram_id) not in caplog.text
    finally:
        server.should_exit = True
        await task
        listener.close()
        if user_id is not None:
            await migrator.execute(
                "DELETE FROM order_intent_requests WHERE order_id IN "
                "(SELECT id FROM purchase_orders WHERE user_id = $1)",
                user_id,
            )
            await migrator.execute("DELETE FROM purchase_orders WHERE user_id = $1", user_id)
        if webinar_id is not None:
            await migrator.execute("DELETE FROM webinars WHERE id = $1", webinar_id)
        if user_id is not None:
            await migrator.execute("DELETE FROM users WHERE id = $1", user_id)
        await migrator.close()


@pytest.mark.asyncio
async def test_orders_snapshot_backend_price_and_replay_once_without_grants(caplog):
    app_pool = await asyncpg.create_pool(Settings().database_url(), min_size=1, max_size=4)
    migrator = await asyncpg.connect(
        Settings(
            database_user="gl_api_migrator",
            database_password_file=Path(os.environ["GL_API_TEST_MIGRATOR_PASSWORD_FILE"]),
        ).database_url()
    )
    base = int.from_bytes(uuid4().bytes[:7], "big")
    users: list[int] = []
    webinars: list[int] = []
    try:
        for telegram_id in (-base, -(base + 1)):
            users.append(
                await migrator.fetchval(
                    "INSERT INTO users(telegram_id) VALUES ($1) RETURNING id", telegram_id
                )
            )
        for title, price, active in (
            ("active", 1000, True),
            ("inactive", 2000, False),
            ("free", 0, True),
            ("unpriced", None, True),
            ("owned", 3000, True),
        ):
            webinars.append(
                await migrator.fetchval(
                    "INSERT INTO webinars(title, price_kopeks, active) "
                    "VALUES ($1, $2, $3) RETURNING id",
                    f"Synthetic {title}",
                    price,
                    active,
                )
            )
        await migrator.execute(
            "INSERT INTO webinar_entitlements(user_id, webinar_id, source) "
            "VALUES ($1, $2, 'imported')",
            users[0],
            webinars[4],
        )
        repo = OrderRepository(app_pool)
        first = await repo.create_pending_order(-base, webinars[0], base + 10)
        assert first["user_id"] == users[0]
        assert first["amount_kopeks"] == 1000
        assert first["currency"] == "RUB"
        assert first["status"] == "pending"
        assert first["source_update_id"] == base + 10
        assert await migrator.fetchval("SELECT count(*) FROM payment_events") == 0
        assert (
            await migrator.fetchval(
                "SELECT count(*) FROM webinar_entitlements WHERE user_id = $1", users[0]
            )
            == 1
        )

        await migrator.execute(
            "UPDATE webinars SET price_kopeks = 2500 WHERE id = $1", webinars[0]
        )
        assert await repo.create_pending_order(-base, webinars[0], base + 10) == first
        assert await repo.create_pending_order(-base, webinars[0], base + 11) == first
        assert (
            await migrator.fetchval(
                "SELECT count(*) FROM order_intent_requests WHERE order_id = $1", first["id"]
            )
            == 2
        )

        with pytest.raises(OrderRequestConflict):
            await repo.create_pending_order(-(base + 1), webinars[0], base + 10)
        with pytest.raises(OrderRequestConflict):
            await repo.create_pending_order(-base, webinars[4], base + 10)

        await migrator.execute(
            "UPDATE purchase_orders SET status = 'cancelled' WHERE id = $1", first["id"]
        )
        assert (await repo.create_pending_order(-base, webinars[0], base + 10))["status"] == (
            "cancelled"
        )
        second = await repo.create_pending_order(-base, webinars[0], base + 12)
        assert second["id"] != first["id"]
        assert second["amount_kopeks"] == 2500
        concurrent = await asyncio.gather(
            repo.create_pending_order(-base, webinars[0], base + 13),
            repo.create_pending_order(-base, webinars[0], base + 14),
        )
        assert concurrent[0]["id"] == concurrent[1]["id"]
        assert concurrent[0]["id"] == second["id"]
        first_clicks = await asyncio.gather(
            repo.create_pending_order(-(base + 1), webinars[0], base + 15),
            repo.create_pending_order(-(base + 1), webinars[0], base + 16),
        )
        assert first_clicks[0]["id"] == first_clicks[1]["id"]
        assert first_clicks[0]["user_id"] == users[1]
        assert (
            await migrator.fetchval(
                "SELECT count(*) FROM purchase_orders WHERE user_id = $1", users[1]
            )
            == 1
        )
        with pytest.raises(OrderRequestConflict):
            await repo.create_pending_order(-base, webinars[0], base + 16)
        assert (
            await migrator.fetchval(
                "SELECT count(*) FROM purchase_orders WHERE user_id = $1", users[0]
            )
            == 2
        )
        await migrator.execute(
            "INSERT INTO webinar_entitlements(user_id, webinar_id, source) "
            "VALUES ($1, $2, 'imported')",
            users[0],
            webinars[0],
        )
        assert (await repo.create_pending_order(-base, webinars[0], base + 10))["id"] == first[
            "id"
        ]
        for telegram_id, webinar_id in (
            (-(base + 2), webinars[0]),
            (-base, webinars[0]),
            (-base, webinars[1]),
            (-base, webinars[2]),
            (-base, webinars[3]),
            (-base, webinars[4]),
            (-base, 2**63 - 1),
        ):
            with pytest.raises(OrderUnavailable):
                await repo.create_pending_order(telegram_id, webinar_id, base + 20)
        assert (
            await migrator.fetchval(
                "SELECT count(*) FROM purchase_orders WHERE user_id = $1", users[0]
            )
            == 2
        )
        assert str(base) not in caplog.text
    finally:
        if users:
            await migrator.execute(
                "DELETE FROM order_intent_requests WHERE order_id IN "
                "(SELECT id FROM purchase_orders WHERE user_id = ANY($1::bigint[]))",
                users,
            )
            await migrator.execute(
                "DELETE FROM purchase_orders WHERE user_id = ANY($1::bigint[])", users
            )
            await migrator.execute(
                "DELETE FROM webinar_entitlements WHERE user_id = ANY($1::bigint[])", users
            )
        if webinars:
            await migrator.execute("DELETE FROM webinars WHERE id = ANY($1::bigint[])", webinars)
        if users:
            await migrator.execute("DELETE FROM users WHERE id = ANY($1::bigint[])", users)
        await migrator.close()
        await app_pool.close()
