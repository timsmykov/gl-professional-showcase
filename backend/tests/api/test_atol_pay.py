"""Actual synthetic HTTP checks; no merchant key or real payment is used."""

import asyncio

import aiohttp
import pytest
from aiohttp import web

import gl_api.atol_pay as provider
from gl_api.atol_pay import (
    AtolPayClient,
    AtolPayError,
    PaymentStatus,
    ReceiptPolicy,
    RegistrationUncertain,
)
from gl_api.config import ConfigurationError

ORDER_ID = "00000000-0000-4000-8000-000000000001"
TOKEN = "synthetic-token-never-live"
POLICY = ReceiptPolicy(sno=1, tax=5, payment_method=3, payment_subject=4)


@pytest.fixture
async def transport(tmp_path, monkeypatch):
    calls = []
    reply = {"http": 200, "payload": {"status": "success", "data": {"status": 0}}}

    async def handler(request):
        calls.append(
            (request.method, request.path, await request.json() if request.can_read_body else None)
        )
        assert request.headers["Authorization"] == "Bearer " + TOKEN
        if reply.get("delay"):
            await asyncio.sleep(reply["delay"])
        if "raw" in reply:
            return web.Response(status=reply["http"], text=reply["raw"])
        return web.json_response(reply["payload"], status=reply["http"])

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    origin = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    monkeypatch.setitem(provider.ORIGINS, "sandbox", origin)
    token_file = tmp_path / "token"
    token_file.write_text(TOKEN)
    try:
        async with aiohttp.ClientSession(trust_env=False) as session:
            yield (
                AtolPayClient(
                    session,
                    token_file,
                    environment="sandbox",
                    payment_hosts=frozenset({"pay.example"}),
                ),
                calls,
                reply,
            )
    finally:
        await runner.cleanup()


def registered_reply(reply):
    reply["payload"] = {
        "status": "success",
        "data": {
            "orderId": ORDER_ID,
            "amount": 150_000,
            "paymentUrls": {"main": "https://pay.example/synthetic"},
            "secret": TOKEN,
        },
    }


async def register(client, **changes):
    arguments = {
        "order_id": ORDER_ID,
        "amount_kopeks": 150_000,
        "recording_name": "Synthetic recording",
        "return_url": "https://t.me/glprofessional_bot",
        "receipt": POLICY,
    }
    return await client.register(**(arguments | changes))


async def test_registration_uses_hosted_email_fiscalization_and_fixed_price(transport, caplog):
    client, calls, reply = transport
    registered_reply(reply)
    payment = await register(client)
    assert payment.amount_kopeks == 150_000
    assert payment.order_id == ORDER_ID
    assert payment.payment_url == "https://pay.example/synthetic"
    assert "synthetic" not in repr(payment)
    method, path, body = calls[0]
    assert (method, path) == ("POST", "/v1/ecom/payments")
    assert body["sessionType"] == "oneStep"
    assert body["recurring"] is False
    assert body["receipt"]["providerId"] == 100
    assert body["receipt"]["positions"][0]["quantity"] == 1000
    assert body["receipt"]["positions"][0]["price"] == body["amount"]
    assert "buyer" not in body["receipt"]
    assert "buyerId" not in body and "paymentMethods" not in body
    assert TOKEN not in caplog.text and TOKEN not in repr(payment)


async def test_order_notification_url_is_sent_only_to_provider(transport):
    client, calls, reply = transport
    registered_reply(reply)
    notification_url = "https://callbacks.example/callbacks/atol/receipts?token=" + "a" * 64
    payment = await register(client, notification_url=notification_url)
    assert calls[0][2]["additionalProps"]["notificationUrl"] == notification_url
    assert notification_url not in repr(payment)
    assert payment.payment_url == "https://pay.example/synthetic"


@pytest.mark.parametrize("code", list(PaymentStatus))
async def test_status_projects_all_documented_states(transport, code):
    client, calls, reply = transport
    reply["payload"]["data"] = {"status": int(code), "secret": TOKEN}
    assert await client.status(ORDER_ID) is code
    assert calls == [("GET", f"/v1/ecom/payments/{ORDER_ID}/status", None)]


@pytest.mark.parametrize("code", [True, False, "1", 1.0, None, -1, 13, 256])
async def test_malformed_status_never_becomes_paid(transport, code):
    client, _, reply = transport
    reply["payload"]["data"]["status"] = code
    with pytest.raises(AtolPayError, match="payment status invalid"):
        await client.status(ORDER_ID)


@pytest.mark.parametrize("http", [201, 302, 401, 403, 404, 429, 500, 503])
async def test_http_failures_safe_and_registration_never_retries(transport, http, caplog):
    client, calls, reply = transport
    reply.update(http=http, payload={"status": "error", "errorMessage": TOKEN})
    with pytest.raises(RegistrationUncertain) as error:
        await register(client)
    assert len(calls) == 1
    assert TOKEN not in str(error.value) and TOKEN not in caplog.text


@pytest.mark.parametrize("mutation", ["id", "amount", "bool_amount", "evil_url", "missing_url"])
async def test_registration_mismatch_requires_reconciliation(transport, mutation):
    client, calls, reply = transport
    registered_reply(reply)
    data = reply["payload"]["data"]
    if mutation == "id":
        data["orderId"] = "00000000-0000-4000-8000-000000000002"
    elif mutation == "amount":
        data["amount"] = 149_000
    elif mutation == "bool_amount":
        data["amount"] = True
    elif mutation == "evil_url":
        data["paymentUrls"]["main"] = "https://pay.example.attacker.invalid/"
    else:
        data.pop("paymentUrls")
    with pytest.raises(RegistrationUncertain):
        await register(client)
    assert len(calls) == 1


@pytest.mark.parametrize("raw", ["[]", "null", "{}", "not JSON", '{"status":"success","data":[]}'])
async def test_invalid_response_sanitized(transport, raw):
    client, _, reply = transport
    reply["raw"] = raw
    with pytest.raises(AtolPayError, match="response invalid"):
        await client.status(ORDER_ID)


@pytest.mark.parametrize(
    "raw",
    [
        '{"status":"error","status":"success","data":{"status":1}}',
        '{"status":"success","data":{"status":0,"status":1}}',
        '{"status":"success","data":{"status":0},"data":{"status":1}}',
    ],
)
async def test_ambiguous_json_cannot_confirm_payment(transport, raw, caplog):
    client, calls, reply = transport
    reply["raw"] = raw
    with pytest.raises(AtolPayError, match="response invalid"):
        await client.status(ORDER_ID)
    assert len(calls) == 1
    assert TOKEN not in caplog.text


async def test_response_size_bounded(transport):
    client, _, reply = transport
    reply["raw"] = "x" * (provider.MAX_RESPONSE_BYTES + 1)
    with pytest.raises(AtolPayError, match="response too large"):
        await client.status(ORDER_ID)


@pytest.mark.parametrize(
    "change",
    [
        {"amount_kopeks": True},
        {"amount_kopeks": 0},
        {"amount_kopeks": 1.5},
        {"amount_kopeks": 2_000_000_001},
        {"order_id": "../../outside"},
        {"return_url": "http://site.example"},
        {"return_url": "https://user:pass@site.example"},
        {"recording_name": ""},
        {"recording_name": "bad\nname"},
        {"receipt": None},
    ],
)
async def test_bad_inputs_do_not_reach_provider(transport, change):
    client, calls, _ = transport
    with pytest.raises(ValueError):
        await register(client, **change)
    assert calls == []


@pytest.mark.parametrize("phase", ["connect", "response"])
async def test_transport_timeout_is_uncertain_without_retry(transport, monkeypatch, phase):
    client, calls, reply = transport
    registered_reply(reply)
    reply["delay"] = 0.2
    original = aiohttp.ClientTimeout
    if phase == "connect":
        connect = aiohttp.TCPConnector._create_connection

        async def delayed_connection(self, *args, **kwargs):
            await asyncio.sleep(0.1)
            return await connect(self, *args, **kwargs)

        monkeypatch.setattr(aiohttp.TCPConnector, "_create_connection", delayed_connection)
        timeout = original(total=2, connect=0.01)
    else:
        # Test a response timeout after acceptance, rather than a 10ms total budget
        # that can expire before connecting on a loaded CI runner.
        timeout = original(total=2, connect=1, sock_read=0.05)
    monkeypatch.setattr(provider.aiohttp, "ClientTimeout", lambda **kw: timeout)
    with pytest.raises(RegistrationUncertain):
        await register(client)
    assert len(calls) == (0 if phase == "connect" else 1)


async def test_dictionaries_read_only(transport):
    client, calls, reply = transport
    reply["payload"]["data"] = {"paymentSubjects": [{"id": 4, "name": "synthetic"}]}
    assert await client.receipt_dictionaries() == reply["payload"]["data"]
    assert calls == [("GET", "/v1/ecom/receipts/dictionaries", None)]


@pytest.mark.parametrize(
    "changes",
    [{"sno": 3}, {"tax": 12}, {"payment_method": 7}, {"payment_subject": 27}, {"tax": True}],
)
def test_fiscal_policy_rejects_invalid_dictionary_ids(changes):
    with pytest.raises(ValueError, match="fiscal dictionary"):
        ReceiptPolicy(**(vars(POLICY) | changes))


async def test_client_refuses_environment_proxy_and_header_injection(tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text(TOKEN)
    async with aiohttp.ClientSession(trust_env=True) as session:
        with pytest.raises(ConfigurationError):
            AtolPayClient(
                session,
                token_file,
                environment="sandbox",
                payment_hosts=frozenset({"pay.example"}),
            )
    token_file.write_text("token\r\nInjected: header")
    async with aiohttp.ClientSession(trust_env=False) as session:
        with pytest.raises(ConfigurationError):
            AtolPayClient(
                session,
                token_file,
                environment="sandbox",
                payment_hosts=frozenset({"pay.example"}),
            )


@pytest.mark.parametrize(
    "url",
    [
        "http://pay.example/",
        "https://user:pass@pay.example/",
        "https://pay.example:444/",
        "https://pay.example/\nsecret",
        "https://pay.example\\@evil.invalid/",
    ],
)
async def test_registration_rejects_unsafe_hosted_url(transport, url):
    client, calls, reply = transport
    registered_reply(reply)
    reply["payload"]["data"]["paymentUrls"]["main"] = url
    with pytest.raises(RegistrationUncertain):
        await register(client)
    assert len(calls) == 1


@pytest.mark.parametrize("operation", ["register", "status"])
async def test_redirect_does_not_forward_merchant_credential(tmp_path, monkeypatch, operation):
    """A real HTTP redirect must not trigger a request to the destination."""
    redirected = []

    async def destination(request):
        redirected.append(request.headers.get("Authorization"))
        return web.json_response({"status": "success", "data": {"status": 1}})

    target = web.Application()
    target.router.add_route("*", "/{tail:.*}", destination)
    target_runner = web.AppRunner(target, access_log=None)
    await target_runner.setup()
    target_site = web.TCPSite(target_runner, "127.0.0.1", 0)
    await target_site.start()
    url = f"http://127.0.0.1:{target_site._server.sockets[0].getsockname()[1]}/capture"

    async def redirect(request):
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        raise web.HTTPTemporaryRedirect(location=url)

    source = web.Application()
    source.router.add_route("*", "/{tail:.*}", redirect)
    source_runner = web.AppRunner(source, access_log=None)
    await source_runner.setup()
    source_site = web.TCPSite(source_runner, "127.0.0.1", 0)
    await source_site.start()
    monkeypatch.setitem(
        provider.ORIGINS,
        "sandbox",
        f"http://127.0.0.1:{source_site._server.sockets[0].getsockname()[1]}",
    )
    token_file = tmp_path / "token"
    token_file.write_text(TOKEN)
    try:
        async with aiohttp.ClientSession(trust_env=False) as session:
            client = AtolPayClient(
                session,
                token_file,
                environment="sandbox",
                payment_hosts=frozenset({"pay.example"}),
            )
            with pytest.raises(AtolPayError):
                if operation == "register":
                    await register(client)
                else:
                    await client.status(ORDER_ID)
        assert redirected == []
    finally:
        await source_runner.cleanup()
        await target_runner.cleanup()
