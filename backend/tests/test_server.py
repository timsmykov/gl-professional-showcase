import asyncio
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

from gl_bot.api_repository import BackendError
from gl_bot.flow import BotFlow, UpdateBusyError
from gl_bot.server import create_app, dispatch_notifications, resources


@pytest.mark.asyncio
async def test_notification_worker_recovers_without_logging_private_errors(monkeypatch, caplog):
    class Flow:
        calls = 0

        async def deliver_pending(self):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("synthetic private delivery detail")
            if self.calls == 2:
                return True
            raise asyncio.CancelledError()

    async def no_delay(_seconds):
        return None

    monkeypatch.setattr("gl_bot.server.asyncio.sleep", no_delay)
    flow = Flow()
    with pytest.raises(asyncio.CancelledError):
        await dispatch_notifications(flow)
    assert flow.calls == 3
    assert "synthetic private delivery detail" not in caplog.text


class FakeFlow:
    def __init__(self):
        self.handled = 0

    async def handle(self, update):
        self.handled += 1


@pytest.mark.asyncio
async def test_webhook_rejects_missing_secret_and_malformed_update():
    flow = FakeFlow()
    app = create_app("test_secret", repo=object(), flow=flow)
    async with TestClient(TestServer(app)) as client:
        unauthorized = await client.post("/telegram/webhook", json={"update_id": 1})
        assert unauthorized.status == 401
        malformed = await client.post(
            "/telegram/webhook",
            data="{",
            headers={
                "content-type": "application/json",
                "X-Telegram-Bot-Api-Secret-Token": "test_secret",
            },
        )
        assert malformed.status == 400
        valid = await client.post(
            "/telegram/webhook",
            json={"update_id": 1},
            headers={"X-Telegram-Bot-Api-Secret-Token": "test_secret"},
        )
        assert valid.status == 200
        assert flow.handled == 1


@pytest.mark.asyncio
async def test_webhook_routes_a_real_telegram_start_update():
    class Repo:
        async def claim_update(self, update_id):
            return {"status": "claimed", "lease": "2026-09-28T00:00:00+00:00"}

        async def get_user(self, telegram_id):
            assert telegram_id == 10
            return {"id": 1, "last_section": None}

        async def complete_update(self, update_id, lease):
            return True

        async def release_update(self, update_id, lease):
            pass

    class Messenger:
        def __init__(self):
            self.sent = []

        async def send(self, chat_id, text, buttons=None):
            self.sent.append((chat_id, text, buttons))

        async def answer(self, callback_id):
            pass

    repo = Repo()
    messenger = Messenger()
    app = create_app("test_secret", repo=repo, flow=BotFlow(repo, messenger))
    update = {
        "update_id": 10,
        "message": {
            "message_id": 5,
            "date": 1790280000,
            "chat": {"id": 10, "type": "private", "first_name": "Client"},
            "from": {"id": 10, "is_bot": False, "first_name": "Client"},
            "text": "/start",
        },
    }
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/telegram/webhook",
            json=update,
            headers={"X-Telegram-Bot-Api-Secret-Token": "test_secret"},
        )
    assert response.status == 200
    assert messenger.sent[0][0] == 10
    buttons = messenger.sent[0][2]
    assert [[button["text"] for button in row] for row in buttons] == [
        ["Клиент услуг", "Косметолог"]
    ]
    assert all("style" not in button for button in buttons[0])


@pytest.mark.asyncio
async def test_webhook_rejects_duplicate_secret_headers_before_handling_update():
    flow = FakeFlow()
    app = create_app("test_secret", repo=object(), flow=flow)
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/telegram/webhook",
            json={"update_id": 1},
            headers=[
                ("X-Telegram-Bot-Api-Secret-Token", "test_secret"),
                ("X-Telegram-Bot-Api-Secret-Token", "wrong"),
            ],
        )
    assert response.status == 401
    assert flow.handled == 0


@pytest.mark.asyncio
async def test_webhook_rejects_wrong_media_type_invalid_shape_and_oversized_body():
    flow = FakeFlow()
    app = create_app("test_secret", repo=object(), flow=flow)
    headers = {"X-Telegram-Bot-Api-Secret-Token": "test_secret"}
    async with TestClient(TestServer(app)) as client:
        wrong_type = await client.post("/telegram/webhook", data="{}", headers=headers)
        invalid_shape = await client.post("/telegram/webhook", json=[], headers=headers)
        oversized = await client.post(
            "/telegram/webhook",
            data=b"x" * (1024 * 1024 + 1),
            headers={**headers, "Content-Type": "application/json"},
        )
    assert wrong_type.status == 415
    assert invalid_shape.status == 400
    assert oversized.status == 413
    assert flow.handled == 0


@pytest.mark.asyncio
async def test_webhook_busy_and_unexpected_failure_are_retryable_without_private_details(caplog):
    class FailingFlow:
        def __init__(self):
            self.failures = [UpdateBusyError(), RuntimeError("synthetic private Telegram data")]

        async def handle(self, _update):
            raise self.failures.pop(0)

    flow = FailingFlow()
    app = create_app("test_secret", repo=object(), flow=flow)
    async with TestClient(TestServer(app)) as client:
        responses = []
        for index in (1, 2):
            response = await client.post(
                "/telegram/webhook",
                json={"update_id": index},
                headers={"X-Telegram-Bot-Api-Secret-Token": "test_secret"},
            )
            responses.append((response.status, await response.text()))
    assert responses == [(503, "temporary error"), (500, "temporary error")]
    assert "synthetic private Telegram data" not in caplog.text
    assert "test_secret" not in caplog.text


@pytest.mark.asyncio
async def test_bot_health_returns_generic_failure_when_repository_is_unavailable(caplog):
    class OfflineRepository:
        async def health(self):
            raise RuntimeError("synthetic private DB detail")

    app = create_app("test_secret", repo=OfflineRepository(), flow=FakeFlow())
    async with TestClient(TestServer(app)) as client:
        response = await client.get("/healthz")
        body = await response.text()
    assert response.status == 503
    assert body == "unavailable"
    assert "synthetic private DB detail" not in caplog.text


@pytest.mark.asyncio
async def test_webhook_retries_backend_failure_without_exposing_details(caplog):
    class FailingFlow:
        async def handle(self, _update):
            raise BackendError("synthetic private backend detail")

    app = create_app("test_secret", repo=object(), flow=FailingFlow())
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/telegram/webhook",
            json={"update_id": 1},
            headers={"X-Telegram-Bot-Api-Secret-Token": "test_secret"},
        )
        assert response.status == 503
        assert await response.text() == "temporary error"
    assert "test_secret" not in caplog.text
    assert "synthetic private backend detail" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("recordings_enabled", [False, True])
async def test_api_mode_starts_without_opening_a_bot_database_pool(
    monkeypatch, recordings_enabled
):
    opened = []
    closed = []

    class Session:
        def __init__(self, **_kwargs):
            opened.append("http")

        async def close(self):
            closed.append("http")

    class Repo:
        def __init__(self, _session, url, token):
            assert url == "http://api:8000"
            assert token == "synthetic-internal-token-1234567890"

        async def health(self):
            return None

    class Bot:
        def __init__(self, **_kwargs):
            self.session = SimpleNamespace(close=self.close)

        async def close(self):
            closed.append("telegram")

    async def forbidden_pool(**_kwargs):
        raise AssertionError("bot API mode opened a DB pool")

    monkeypatch.setattr("gl_bot.server.aiohttp.ClientSession", Session)
    monkeypatch.setattr("gl_bot.server.ApiRepository", Repo)
    monkeypatch.setattr("gl_bot.server.asyncpg.create_pool", forbidden_pool)
    monkeypatch.setattr("gl_bot.server.create_bot", lambda _settings: Bot())
    settings = SimpleNamespace(
        api_url="http://api:8000",
        api_token="synthetic-internal-token-1234567890",
        telegram_bot_token="123456:synthetic-bot-token",
        german_telegram_id=None,
        german_contact_url=None,
        display_time_zone="Europe/Moscow",
        recordings_api_enabled=recordings_enabled,
    )
    app = create_app("synthetic_secret")
    app.cleanup_ctx.append(lambda current: resources(current, settings))
    async with TestClient(TestServer(app)) as client:
        from gl_bot.api_recordings import ApiRecordingGateway
        from gl_bot.server import FLOW_KEY

        assert isinstance(app[FLOW_KEY].recordings, ApiRecordingGateway) == recordings_enabled
        assert (await client.get("/healthz")).status == 200
    assert opened == ["http"]
    assert closed == ["telegram", "http"]


@pytest.mark.asyncio
async def test_api_mode_closes_session_if_backend_health_fails_at_startup(monkeypatch):
    closed = []

    class Session:
        def __init__(self, **_kwargs):
            pass

        async def close(self):
            closed.append("http")

    class Repo:
        def __init__(self, *_args):
            pass

        async def health(self):
            raise BackendError("synthetic private backend detail")

    monkeypatch.setattr("gl_bot.server.aiohttp.ClientSession", Session)
    monkeypatch.setattr("gl_bot.server.ApiRepository", Repo)
    monkeypatch.setattr(
        "gl_bot.server.create_bot",
        lambda _settings: (_ for _ in ()).throw(AssertionError("bot started before backend")),
    )
    settings = SimpleNamespace(
        api_url="http://api:8000",
        api_token="synthetic-internal-token-1234567890",
    )
    app = create_app("synthetic_secret")
    app.cleanup_ctx.append(lambda current: resources(current, settings))
    with pytest.raises(BackendError):
        async with TestClient(TestServer(app)):
            pass
    assert closed == ["http"]
