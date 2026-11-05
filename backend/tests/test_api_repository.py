"""Bot-side HTTP adapter: failures must stay retryable and free of response details."""

import asyncio

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from gl_bot.api_repository import ApiRepository, BackendError

TOKEN = "synthetic-internal-token-1234567890"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        web.json_response({"unexpected": "value"}),
        web.json_response(["not an envelope"]),
        web.json_response({"result": {"starts_at": "not a datetime"}}),
        web.Response(text="sensitive synthetic response", content_type="text/plain"),
    ],
)
async def test_adapter_rejects_malformed_backend_response_without_leaking_it(payload):
    async def malformed(_request):
        return payload

    app = web.Application()
    app.router.add_post("/internal/bot/health", malformed)
    async with TestServer(app) as server, aiohttp.ClientSession() as session:
        repo = ApiRepository(session, str(server.make_url("/")), TOKEN)
        with pytest.raises(BackendError, match="invalid backend response") as error:
            await repo.health()
        assert "synthetic" not in str(error.value)


@pytest.mark.asyncio
async def test_adapter_never_sends_invalid_operation_or_arguments():
    calls = []

    async def record(request):
        calls.append(await request.json())
        return web.json_response({"result": None})

    app = web.Application()
    app.router.add_post("/internal/bot/{operation}", record)
    async with TestServer(app) as server, aiohttp.ClientSession() as session:
        repo = ApiRepository(session, str(server.make_url("/")), TOKEN)
        assert not hasattr(repo, "grant_entitlement")
        with pytest.raises(ValueError, match="invalid bot repository call"):
            await repo.get_user(2**63)
        with pytest.raises(ValueError, match="invalid bot repository call"):
            await repo.select_section(1, "admin")
        assert calls == []
        assert await repo.health() is None
        assert calls == [{"args": []}]


@pytest.mark.asyncio
async def test_adapter_maps_timeout_and_http_failure_to_safe_errors():
    async def slow(_request):
        await asyncio.sleep(0.1)
        return web.json_response({"result": None})

    async def rejected(_request):
        return web.Response(status=401, text="synthetic private detail")

    app = web.Application()
    app.router.add_post("/internal/bot/health", slow)
    app.router.add_post("/internal/bot/list_services", rejected)
    async with (
        TestServer(app) as server,
        aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=0.01)) as short_session,
        aiohttp.ClientSession() as session,
    ):
        timed = ApiRepository(short_session, str(server.make_url("/")), TOKEN)
        with pytest.raises(BackendError, match="backend unavailable"):
            await timed.health()
        repo = ApiRepository(session, str(server.make_url("/")), TOKEN)
        with pytest.raises(BackendError, match="backend status 401") as error:
            await repo.list_services()
        assert "synthetic private detail" not in str(error.value)
        assert TOKEN not in str(error.value)


@pytest.mark.asyncio
async def test_order_adapter_sends_only_identity_and_returns_valid_pending_order():
    calls = []

    async def intent(request):
        calls.append((await request.json(), request.headers.get("Authorization")))
        return web.json_response(
            {
                "id": 7,
                "webinar_id": 3,
                "amount_kopeks": 15900,
                "currency": "RUB",
                "status": "pending",
            }
        )

    app = web.Application()
    app.router.add_post("/internal/order-intents", intent)
    async with TestServer(app) as server, aiohttp.ClientSession() as session:
        repo = ApiRepository(session, str(server.make_url("/")), TOKEN)
        for values in ((True, 3, 50), (0, 3, 50), (1, -1, 50), (1, 3, 2**63)):
            with pytest.raises(ValueError, match="invalid order intent"):
                await repo.create_order_intent(*values)
        assert calls == []
        assert await repo.create_order_intent(-9001, 3, 50) == {
            "id": 7,
            "webinar_id": 3,
            "amount_kopeks": 15900,
            "currency": "RUB",
            "status": "pending",
        }
        assert calls == [
            ({"telegram_id": -9001, "webinar_id": 3, "source_update_id": 50}, f"Bearer {TOKEN}")
        ]


@pytest.mark.asyncio
async def test_order_adapter_fails_closed_on_bad_response_and_omits_private_details():
    response = {
        "status": 200,
        "payload": {
            "id": 1,
            "webinar_id": 3,
            "amount_kopeks": 99,
            "currency": "RUB",
            "status": "paid",
        },
    }

    async def malformed(_request):
        return web.json_response(response["payload"], status=response["status"])

    app = web.Application()
    app.router.add_post("/internal/order-intents", malformed)
    async with TestServer(app) as server, aiohttp.ClientSession() as session:
        repo = ApiRepository(session, str(server.make_url("/")), TOKEN)
        for payload in (
            {
                "id": 1,
                "webinar_id": 3,
                "amount_kopeks": 99,
                "currency": "RUB",
                "status": "unknown",
            },
            {
                "id": True,
                "webinar_id": 3,
                "amount_kopeks": 99,
                "currency": "RUB",
                "status": "pending",
            },
            {"id": 1, "webinar_id": 3, "amount_kopeks": 0, "currency": "RUB", "status": "pending"},
            {"detail": "synthetic private email"},
        ):
            response["payload"] = payload
            with pytest.raises(BackendError, match="invalid backend response") as error:
                await repo.create_order_intent(-9001, 3, 50)
            assert "synthetic private email" not in str(error.value)
        response["status"] = 503
        with pytest.raises(BackendError, match="backend status 503") as error:
            await repo.create_order_intent(-9001, 3, 50)
        assert TOKEN not in str(error.value)
