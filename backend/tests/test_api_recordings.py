import asyncio
from contextlib import asynccontextmanager

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from test_flow import Fixture

from gl_bot.api_recordings import ApiRecordingGateway
from gl_bot.api_repository import ApiRepository, BackendError

TOKEN = "synthetic-internal-service-token-123456"
URL = "https://checkout.example.invalid/backend-only-link"
ORDER = {"id": 71, "webinar_id": 3, "amount_kopeks": 14900, "currency": "RUB", "status": "pending"}
CATALOG = {
    "id": 3,
    "title": "Synthetic recording",
    "description": "Description",
    "price_kopeks": 20000,
}


@asynccontextmanager
async def gateway_server(overrides=None):
    calls = []
    replies = {
        "/internal/bot/get_webinar": {"result": CATALOG},
        "/internal/bot/get_recording_context": {"result": CATALOG},
        "/internal/bot/resolve_recording": {"result": CATALOG},
        "/internal/bot/list_owned_recordings": {"result": []},
        "/internal/bot/get_user": {"result": {"id": 200}},
        "/internal/bot/list_entitlements": {"result": []},
        "/internal/order-intents": ORDER,
        "/internal/order-status": ORDER,
        "/internal/checkouts": {"order_id": 71, "payment_url": URL},
        "/internal/video-sessions": {
            "watch_url": "https://video.example.invalid/watch/#backend-ticket",
            "expires_in": 60,
        },
        **(overrides or {}),
    }

    async def handle(request):
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        calls.append((request.path, await request.json()))
        result = replies.get(request.path)
        if callable(result):
            return await result(request)
        return (
            web.Response(status=result.status, body=result.body, headers=result.headers)
            if isinstance(result, web.Response)
            else web.json_response(result)
        )

    app = web.Application()
    app.router.add_post("/{tail:.*}", handle)
    async with TestServer(app) as server, aiohttp.ClientSession() as session:
        repo = ApiRepository(session, str(server.make_url("/")), TOKEN)
        yield ApiRecordingGateway(repo), calls, replies


@pytest.mark.asyncio
async def test_checkout_reuses_backend_pending_and_frozen_price_without_ttl():
    async with gateway_server() as (gateway, calls, _):
        first = await gateway.checkout(10, 3, 1)
        second = await gateway.checkout(10, 3, 2)
        assert first == second
        assert first.id == 71 and first.webinar_id == 3
        assert first.title == CATALOG["title"]
        assert first.amount_kopeks == 14900 != CATALOG["price_kopeks"]
        assert first.payment_url == URL and first.expires_at is None
        assert [body for path, body in calls if path == "/internal/order-intents"] == [
            {"telegram_id": 10, "webinar_id": 3, "source_update_id": 1},
            {"telegram_id": 10, "webinar_id": 3, "source_update_id": 2},
        ]
        assert [body for path, body in calls if path == "/internal/checkouts"] == [
            {"telegram_id": 10, "order_id": 71},
        ] * 2


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [404, 409])
async def test_unavailable_or_reconciling_checkout_does_not_repeat_post(status):
    async with gateway_server({"/internal/checkouts": web.Response(status=status, text=URL)}) as (
        gateway,
        calls,
        _,
    ):
        order = await gateway.checkout(10, 3, 1)
        assert order.status == "pending" and order.payment_url is None
        assert [p for p, _ in calls].count("/internal/checkouts") == 1
        await gateway.order(10, 71)
        assert [p for p, _ in calls].count("/internal/checkouts") == 1
        assert [p for p, _ in calls].count("/internal/order-intents") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["pending", "paid", "refunded", "cancelled"])
async def test_persisted_status_read_never_creates_checkout_or_grants(status):
    async with gateway_server({"/internal/order-status": {**ORDER, "status": status}}) as (
        gateway,
        calls,
        _,
    ):
        result = await gateway.order(10, 71)
        assert result.status == status and result.payment_url is None
        assert calls == [
            ("/internal/order-status", {"telegram_id": 10, "order_id": 71}),
            ("/internal/bot/get_recording_context", {"args": [3]}),
        ]


@pytest.mark.asyncio
async def test_foreign_unknown_order_and_video_denial_are_indistinguishable():
    async with gateway_server(
        {
            "/internal/order-status": web.Response(status=404),
            "/internal/video-sessions": web.Response(status=403),
        }
    ) as (gateway, calls, _):
        assert await gateway.order(10, 71) is None
        assert await gateway.order(11, 999) is None
        assert await gateway.playback(11, 3) is None
        assert not any(
            path in {"/internal/checkouts", "/internal/order-intents"} for path, _ in calls
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"order_id": 72, "payment_url": URL},
        {"order_id": True, "payment_url": URL},
        {"order_id": 71, "payment_url": "http://unsafe.example"},
        {"order_id": 71, "payment_url": "https://user:secret@example.invalid"},
        {"order_id": 71, "payment_url": URL, "extra": "secret"},
        [],
        None,
    ],
)
async def test_invalid_checkout_schema_is_safe(payload, caplog):
    async with gateway_server({"/internal/checkouts": payload}) as (gateway, calls, _):
        with pytest.raises(BackendError) as error:
            await gateway.checkout(10, 3, 1)
        assert URL not in str(error.value) and TOKEN not in str(error.value)
        assert URL not in caplog.text and TOKEN not in caplog.text
        assert [path for path, _ in calls].count("/internal/checkouts") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {**ORDER, "webinar_id": 4},
        {**ORDER, "id": True},
        {**ORDER, "amount_kopeks": 0},
        {**ORDER, "status": "paid_by_callback"},
    ],
)
async def test_bad_intent_context_prevents_checkout_post(payload):
    async with gateway_server({"/internal/order-intents": payload}) as (gateway, calls, _):
        with pytest.raises(BackendError):
            await gateway.checkout(10, 3, 1)
        assert not any(p == "/internal/checkouts" for p, _ in calls)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reply",
    [
        web.Response(status=503, text=URL),
        web.Response(status=401, text=TOKEN),
        web.Response(status=302, headers={"Location": "/internal/checkouts"}),
        web.Response(text="{broken", content_type="application/json"),
        web.Response(text='{"result":null}', content_type="text/plain"),
        web.Response(body=b"x" * 65537, content_type="application/json"),
    ],
)
async def test_http_failures_redirects_and_bounded_response_are_not_retried(reply):
    async with gateway_server({"/internal/bot/get_webinar": reply}) as (gateway, calls, _):
        with pytest.raises(BackendError) as error:
            await gateway.checkout(10, 3, 1)
        assert TOKEN not in str(error.value) and URL not in str(error.value)
        assert len(calls) == 1


@pytest.mark.asyncio
async def test_timeout_and_cancellation_propagate_without_retry(monkeypatch):
    started = asyncio.Event()

    async def slow(_request):
        started.set()
        await asyncio.sleep(0.1)
        return web.json_response({"result": CATALOG})

    monkeypatch.setattr("gl_bot.api_recordings.REQUEST_TIMEOUT", aiohttp.ClientTimeout(total=0.01))
    async with gateway_server({"/internal/bot/get_webinar": slow}) as (gateway, calls, _):
        with pytest.raises(BackendError, match="unavailable"):
            await gateway.checkout(10, 3, 1)
        monkeypatch.setattr(
            "gl_bot.api_recordings.REQUEST_TIMEOUT", aiohttp.ClientTimeout(total=1)
        )
        started.clear()
        task = asyncio.create_task(gateway.checkout(10, 3, 2))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(calls) == 2


@pytest.mark.asyncio
async def test_explicit_code_and_id_bearing_owned_recordings():
    async with gateway_server() as (gateway, calls, replies):
        assert (await gateway.resolve("public_code")).id == 3
        assert calls == [("/internal/bot/resolve_recording", {"args": ["public_code"]})]
        assert await gateway.resolve("../bad") is None
        assert len(calls) == 1
        assert await gateway.owned(10) == []
        replies["/internal/bot/list_owned_recordings"] = {
            "result": [{"webinar_id": 3, "title": CATALOG["title"]}]
        }
        owned = await gateway.owned(10)
        assert owned[0].webinar_id == 3
        replies["/internal/bot/list_owned_recordings"] = {"result": [{"title": "missing id"}]}
        with pytest.raises(BackendError):
            await gateway.owned(10)


@pytest.mark.asyncio
async def test_backend_only_playback_url_and_identifier_validation():
    async with gateway_server() as (gateway, calls, _):
        playback = await gateway.playback(10, 3)
        assert playback.url == "https://video.example.invalid/watch/#backend-ticket"
        assert playback.webinar_id == 3 and playback.title == CATALOG["title"]
        before = len(calls)
        with pytest.raises(ValueError):
            await gateway.checkout(True, 3, 1)
        assert len(calls) == before


@pytest.mark.asyncio
async def test_bot_synthetic_updates_show_only_backend_link_and_reconciliation_status():
    async with gateway_server() as (gateway, calls, replies):
        fixture = Fixture()
        fixture.flow.recordings = gateway
        await fixture.message()
        await fixture.click(10, "section:pro")
        update_id = fixture.next_id
        await fixture.click(10, "webinar:buy:3")
        buttons = fixture.messenger.messages[-1]["buttons"]
        assert buttons[0][0]["url"] == URL
        assert calls[-2][1]["telegram_id"] == 10
        assert calls[-2][1]["source_update_id"] == update_id
        assert "действует до" not in fixture.messenger.messages[-1]["text"]
        count = len(calls)
        fixture.next_id = update_id
        await fixture.click(10, "webinar:buy:3")
        assert len(calls) == count
        replies["/internal/checkouts"] = web.Response(status=409)
        await fixture.click(10, "webinar:buy:3")
        assert URL not in str(fixture.messenger.messages[-1]["buttons"])
        assert "Проверить статус" in str(fixture.messenger.messages[-1]["buttons"])
        checkout_count = sum(p == "/internal/checkouts" for p, _ in calls)
        replies["/internal/order-status"] = {**ORDER, "status": "paid"}
        await fixture.click(10, "order:status:71")
        assert "Оплата подтверждена" in fixture.messenger.messages[-1]["text"]
        await fixture.click(10, "pro:mine")
        assert "нет доступных" in fixture.messenger.messages[-1]["text"]
        replies["/internal/order-status"] = {**ORDER, "status": "refunded"}
        await fixture.click(10, "order:status:71")
        assert "возврат" in fixture.messenger.messages[-1]["text"]
        assert sum(p == "/internal/checkouts" for p, _ in calls) == checkout_count
        await fixture.click(10, "section:client")
        await fixture.click(10, "webinar:buy:3")
        assert sum(p == "/internal/checkouts" for p, _ in calls) == checkout_count
