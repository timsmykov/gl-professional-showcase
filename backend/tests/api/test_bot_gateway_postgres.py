"""A synthetic Telegram webhook crossing bot, HTTP API and PostgreSQL 16."""

import asyncio
import os
import socket
from pathlib import Path

import aiohttp
import asyncpg
import pytest
import uvicorn
from aiohttp.test_utils import TestClient, TestServer

from gl_api.app import create_app as create_api
from gl_api.config import Settings
from gl_bot.api_repository import ApiRepository
from gl_bot.flow import BotFlow
from gl_bot.server import create_app as create_bot

pytestmark = pytest.mark.skipif(
    os.getenv("GL_API_TEST_DATABASE") != "1", reason="isolated PostgreSQL test container required"
)

TOKEN = "synthetic-internal-token-1234567890"
WEBHOOK_SECRET = "synthetic_webhook_secret"
CLIENT_ID = -9_400_000_000_001
ADMIN_ID = -9_400_000_000_002
UPDATE_IDS = tuple(range(9_400_000_000_101, 9_400_000_000_109))


class Messenger:
    def __init__(self):
        self.messages = []

    async def send(self, chat_id, text, buttons=None):
        self.messages.append((chat_id, text, buttons))

    async def answer(self, _callback_id):
        return None


def telegram_update(update_id: int, sender_id: int, action: str | None = None) -> dict:
    user = {"id": sender_id, "is_bot": False, "first_name": "Synthetic"}
    chat = {"id": sender_id, "type": "private", "first_name": "Synthetic"}
    message = {"message_id": update_id, "date": 1790280000, "chat": chat, "from": user}
    if action is None:
        return {"update_id": update_id, "message": {**message, "text": "/start"}}
    return {
        "update_id": update_id,
        "callback_query": {
            "id": str(update_id),
            "from": user,
            "chat_instance": "synthetic-instance",
            "message": message,
            "data": action,
        },
    }


@pytest.mark.asyncio
async def test_webhook_to_api_to_db_booking_confirmation_and_api_outage(tmp_path: Path, caplog):
    token_file = tmp_path / "internal-token"
    token_file.write_text(TOKEN)
    api = create_api(Settings(bot_token_file=token_file))
    migrator_settings = Settings(
        database_user="gl_api_migrator",
        database_password_file=Path(os.environ["GL_API_TEST_MIGRATOR_PASSWORD_FILE"]),
    )
    migrator = await asyncpg.connect(migrator_settings.database_url())
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    server = uvicorn.Server(
        uvicorn.Config(api, log_level="error", access_log=False, lifespan="on")
    )
    api_task = asyncio.create_task(server.serve(sockets=[listener]))
    service_id = slot_id = booking_id = None
    try:
        for _ in range(100):
            if server.started:
                break
            if api_task.done():
                await api_task
            await asyncio.sleep(0.05)
        assert server.started
        service_id = await migrator.fetchval(
            "INSERT INTO services(title, active) VALUES ('Synthetic webhook service', true) RETURNING id"
        )
        slot_id = await migrator.fetchval(
            "INSERT INTO appointment_slots(service_id, starts_at, ends_at) "
            "VALUES ($1, now() + interval '5 days', now() + interval '5 days 1 hour') "
            "RETURNING id",
            service_id,
        )
        base_url = f"http://127.0.0.1:{listener.getsockname()[1]}"
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session:
            repo = ApiRepository(session, base_url, TOKEN)
            messenger = Messenger()
            bot = create_bot(
                WEBHOOK_SECRET,
                repo=repo,
                flow=BotFlow(repo, messenger, german_telegram_id=ADMIN_ID),
            )
            async with TestClient(TestServer(bot)) as client:

                async def post(update_id, sender_id, action=None):
                    return await client.post(
                        "/telegram/webhook",
                        json=telegram_update(update_id, sender_id, action),
                        headers={"X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET},
                    )

                assert (await post(UPDATE_IDS[0], CLIENT_ID)).status == 200
                assert (await post(UPDATE_IDS[1], CLIENT_ID, "section:client")).status == 200
                assert (await post(UPDATE_IDS[2], CLIENT_ID, "client:book")).status == 200
                assert (
                    await post(UPDATE_IDS[3], CLIENT_ID, f"service:slots:{service_id}")
                ).status == 200
                assert (
                    await post(UPDATE_IDS[4], CLIENT_ID, f"slot:request:{slot_id}")
                ).status == 200
                assert messenger.messages[-2][0] == ADMIN_ID
                assert messenger.messages[-1][0] == CLIENT_ID
                booking_id = await migrator.fetchval(
                    "SELECT id FROM booking_requests WHERE slot_id = $1", slot_id
                )
                assert booking_id is not None
                assert (
                    await migrator.fetchval(
                        "SELECT status FROM booking_requests WHERE id = $1", booking_id
                    )
                    == "pending"
                )
                sent_count = len(messenger.messages)
                assert (
                    await post(UPDATE_IDS[4], CLIENT_ID, f"slot:request:{slot_id}")
                ).status == 200
                assert len(messenger.messages) == sent_count
                assert (
                    await post(UPDATE_IDS[5], CLIENT_ID, f"admin:confirm:{booking_id}")
                ).status == 200
                assert (
                    await migrator.fetchval(
                        "SELECT status FROM booking_requests WHERE id = $1", booking_id
                    )
                    == "pending"
                )
                assert (
                    await post(UPDATE_IDS[6], ADMIN_ID, f"admin:confirm:{booking_id}")
                ).status == 200
                assert messenger.messages[-2][0] == CLIENT_ID
                assert (
                    await migrator.fetchval(
                        "SELECT client_notified_at IS NOT NULL FROM booking_requests WHERE id = $1",
                        booking_id,
                    )
                    is True
                )
                sent_count = len(messenger.messages)
                assert (
                    await post(UPDATE_IDS[6], ADMIN_ID, f"admin:confirm:{booking_id}")
                ).status == 200
                assert len(messenger.messages) == sent_count

                server.should_exit = True
                await api_task
                assert (await client.get("/healthz")).status == 503
                assert (await post(UPDATE_IDS[7], CLIENT_ID)).status == 503
                assert (
                    await migrator.fetchval(
                        "SELECT count(*) FROM processed_updates WHERE update_id = $1",
                        UPDATE_IDS[7],
                    )
                    == 0
                )
        assert TOKEN not in caplog.text
        assert WEBHOOK_SECRET not in caplog.text
        assert str(CLIENT_ID) not in caplog.text
    finally:
        server.should_exit = True
        await api_task
        listener.close()
        if booking_id is not None:
            await migrator.execute("DELETE FROM booking_requests WHERE id = $1", booking_id)
        await migrator.execute(
            "DELETE FROM processed_updates WHERE update_id = ANY($1::bigint[])", UPDATE_IDS
        )
        await migrator.execute(
            "DELETE FROM users WHERE telegram_id = ANY($1::bigint[])", [CLIENT_ID, ADMIN_ID]
        )
        if slot_id is not None:
            await migrator.execute("DELETE FROM appointment_slots WHERE id = $1", slot_id)
        if service_id is not None:
            await migrator.execute("DELETE FROM services WHERE id = $1", service_id)
        await migrator.close()


@pytest.mark.asyncio
async def test_professional_catalog_webhook_reads_only_published_records(tmp_path: Path, caplog):
    token_file = tmp_path / "internal-token"
    token_file.write_text(TOKEN)
    api = create_api(Settings(bot_token_file=token_file))
    migrator_settings = Settings(
        database_user="gl_api_migrator",
        database_password_file=Path(os.environ["GL_API_TEST_MIGRATOR_PASSWORD_FILE"]),
    )
    migrator = await asyncpg.connect(migrator_settings.database_url())
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    server = uvicorn.Server(uvicorn.Config(api, log_level="error", access_log=False))
    api_task = asyncio.create_task(server.serve(sockets=[listener]))
    buyer_id = -9_400_000_000_011
    update_ids = tuple(range(9_400_000_000_211, 9_400_000_000_220))
    webinar_ids = []
    product_ids = []
    try:
        for _ in range(100):
            if server.started:
                break
            if api_task.done():
                await api_task
            await asyncio.sleep(0.05)
        assert server.started
        active_webinar = await migrator.fetchval(
            "INSERT INTO webinars(title, description, price_kopeks, active) "
            "VALUES ('Synthetic professional webinar', 'Synthetic program', 12345, true) "
            "RETURNING id"
        )
        hidden_webinar = await migrator.fetchval(
            "INSERT INTO webinars(title, active) VALUES ('Synthetic hidden webinar', false) "
            "RETURNING id"
        )
        webinar_ids.extend((active_webinar, hidden_webinar))
        active_product = await migrator.fetchval(
            "INSERT INTO products(title, description, active) "
            "VALUES ('Synthetic professional product', 'Synthetic description', true) "
            "RETURNING id"
        )
        hidden_product = await migrator.fetchval(
            "INSERT INTO products(title, active) VALUES ('Synthetic hidden product', false) "
            "RETURNING id"
        )
        product_ids.extend((active_product, hidden_product))

        base_url = f"http://127.0.0.1:{listener.getsockname()[1]}"
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session:
            repo = ApiRepository(session, base_url, TOKEN)
            messenger = Messenger()
            bot = create_bot(WEBHOOK_SECRET, repo=repo, flow=BotFlow(repo, messenger))
            async with TestClient(TestServer(bot)) as client:

                async def post(update_id, action=None):
                    return await client.post(
                        "/telegram/webhook",
                        json=telegram_update(update_id, buyer_id, action),
                        headers={"X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET},
                    )

                assert (await post(update_ids[0])).status == 200
                assert (await post(update_ids[1], "section:pro")).status == 200
                assert (await post(update_ids[2], "pro:webinars")).status == 200
                webinar_buttons = messenger.messages[-1][2]
                assert any(
                    button["callback_data"] == f"webinar:info:{active_webinar}"
                    for row in webinar_buttons
                    for button in row
                )
                assert not any(
                    button.get("callback_data") == f"webinar:info:{hidden_webinar}"
                    for row in webinar_buttons
                    for button in row
                )
                assert (await post(update_ids[3], f"webinar:info:{active_webinar}")).status == 200
                assert "Synthetic program" in messenger.messages[-1][1]
                assert "123.45 ₽" in messenger.messages[-1][1]

                await migrator.execute(
                    "UPDATE webinars SET active = false WHERE id = $1", active_webinar
                )
                assert (await post(update_ids[4], f"webinar:info:{active_webinar}")).status == 200
                assert messenger.messages[-1][1] == "Вебинар больше недоступен."

                assert (await post(update_ids[5], "pro:products")).status == 200
                product_buttons = messenger.messages[-1][2]
                assert any(
                    button.get("callback_data") == f"product:info:{active_product}"
                    for row in product_buttons
                    for button in row
                )
                assert not any(
                    button.get("callback_data") == f"product:info:{hidden_product}"
                    for row in product_buttons
                    for button in row
                )
                assert (await post(update_ids[6], f"product:info:{active_product}")).status == 200
                assert "Synthetic description" in messenger.messages[-1][1]
                assert "только лично" in messenger.messages[-1][1]
                sent_count = len(messenger.messages)
                assert (await post(update_ids[6], f"product:info:{active_product}")).status == 200
                assert len(messenger.messages) == sent_count

        user_id = await migrator.fetchval("SELECT id FROM users WHERE telegram_id = $1", buyer_id)
        assert user_id is not None
        assert (
            await migrator.fetchval("SELECT last_section FROM users WHERE id = $1", user_id)
            == "pro"
        )
        assert (
            await migrator.fetchval(
                "SELECT count(*) FROM purchase_orders WHERE user_id = $1", user_id
            )
            == 0
        )
        assert TOKEN not in caplog.text
        assert WEBHOOK_SECRET not in caplog.text
        assert str(buyer_id) not in caplog.text
    finally:
        server.should_exit = True
        await api_task
        listener.close()
        await migrator.execute(
            "DELETE FROM processed_updates WHERE update_id = ANY($1::bigint[])", update_ids
        )
        await migrator.execute("DELETE FROM users WHERE telegram_id = $1", buyer_id)
        if webinar_ids:
            await migrator.execute(
                "DELETE FROM webinars WHERE id = ANY($1::bigint[])", webinar_ids
            )
        if product_ids:
            await migrator.execute(
                "DELETE FROM products WHERE id = ANY($1::bigint[])", product_ids
            )
        await migrator.close()
