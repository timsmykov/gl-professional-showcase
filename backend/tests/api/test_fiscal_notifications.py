from dataclasses import replace
from unittest.mock import AsyncMock
from uuid import uuid4

import asyncpg
import httpx
import pytest

from gl_api.config import ConfigurationError
from gl_api.fiscal import FiscalState, ReceiptUnavailable
from gl_api.fiscal_callback import FiscalSettings, create_app
from gl_api.fiscal_notifications import (
    InvalidNotification,
    NotificationCredentials,
    decode_notification,
    normalize_receipt,
)


@pytest.fixture
def credentials():
    return NotificationCredentials(
        "https://callbacks.example/callbacks/atol/receipts", "sandbox", b"x" * 32
    )


@pytest.fixture
def body():
    return {
        "type": "fiscal",
        "orderId": str(uuid4()),
        "receiptId": str(uuid4()),
        "receiptType": "sell",
        "amount": 1000,
        "sessionType": "oneStep",
        "status": "success",
    }


def test_token_is_order_environment_key_and_purpose_scoped(credentials, body):
    order = body["orderId"]
    token = credentials.token(order)
    assert len(token) == 64 and credentials.verify(order, token)
    assert not credentials.verify(str(uuid4()), token)
    assert not replace(credentials, environment="production").verify(order, token)
    assert not replace(credentials, key=b"y" * 32).verify(order, token)
    assert not credentials.verify(order, "Bearer " + token)
    assert "xxxxx" not in repr(credentials)
    assert credentials.notification_url(order).endswith("?token=" + token)


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://callbacks.example/callbacks/atol/receipts",
        "https://user:pass@callbacks.example/callbacks/atol/receipts",
        "https://callbacks.example/callbacks/atol/receipts?secret=unsafe",
        "https://callbacks.example/callbacks/atol/receipts#fragment",
        "https://callbacks.example/internal/checkouts",
        "https://callbacks.example:8000/callbacks/atol/receipts",
    ],
)
def test_bad_callback_configuration_is_safe(credentials, endpoint):
    with pytest.raises(ConfigurationError, match="^invalid fiscal notification configuration$"):
        replace(credentials, endpoint=endpoint)


@pytest.mark.parametrize(
    "raw",
    [
        b"{}",
        b"[]",
        b"null",
        b'{"orderId":NaN}',
        b'{"orderId":"a","orderId":"b"}',
        b"\xff",
        b"x" * 16385,
    ],
)
def test_invalid_json_never_echoes_input(raw):
    with pytest.raises(InvalidNotification, match="^invalid notification$"):
        decode_notification(raw)


@pytest.mark.parametrize(
    "changes",
    [
        {"amount": True},
        {"amount": 1.0},
        {"amount": "1000"},
        {"amount": 0},
        {"receiptId": str(uuid4()).upper()},
        {"receiptType": "paid"},
        {"type": []},
        {"status": {}},
        {"status": "PAID"},
        {"sessionType": "twoStep"},
    ],
)
def test_invalid_fiscal_payload_rejected(body, changes):
    with pytest.raises(InvalidNotification):
        normalize_receipt({**body, **changes}, "sandbox")


@pytest.mark.parametrize("status", ["fail", "failed"])
def test_failure_aliases_normalized_without_provider_error_storage(body, status):
    observation = normalize_receipt(
        {**body, "status": status, "errorMessage": "private"}, "sandbox"
    )
    assert observation.state is FiscalState.FAILED
    assert "private" not in repr(observation)


def test_success_with_error_is_review_not_confirmation(body):
    assert (
        normalize_receipt({**body, "errorCode": "PRIVATE"}, "sandbox").state is FiscalState.REVIEW
    )


def test_json_integral_decimal_kopeks_are_exact_and_fractional_kopeks_rejected(body):
    import json

    raw = json.dumps({**body, "amount": 1000.0}).encode()
    assert normalize_receipt(decode_notification(raw), "sandbox").amount_kopeks == 1000
    for value in ["1000.01", "1e999999", "1e-999999"]:
        raw = json.dumps(body).replace('"amount": 1000', '"amount": ' + value).encode()
        with pytest.raises(InvalidNotification):
            normalize_receipt(decode_notification(raw), "sandbox")


@pytest.fixture
def callback(tmp_path, credentials):
    secret = tmp_path / "notification_key"
    secret.write_text(credentials.key.hex())
    app = create_app(
        FiscalSettings(
            environment="sandbox",
            notification_endpoint=credentials.endpoint,
            notification_key_file=secret,
            require_database_tls=False,
        )
    )
    app.state.receipts = AsyncMock()
    app.state.receipts.record.return_value = FiscalState.SUCCESS
    return app


async def send(callback, credentials, body, **options):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=callback), base_url="https://test"
    ) as client:
        return await client.post(
            "/callbacks/atol/receipts?token=" + credentials.token(body["orderId"]),
            json=body,
            **options,
        )


async def test_authenticated_fiscal_callback_records_only_normalized_fields(
    callback, credentials, body
):
    response = await send(callback, credentials, {**body, "email": "private@example.test"})
    assert response.status_code == 200
    callback.state.receipts.record.assert_awaited_once_with(
        normalize_receipt(body, "sandbox"), authenticated_notification=True
    )
    assert "private" not in response.text


@pytest.mark.parametrize("event", ["payment", "deposit", "cancel", "refund"])
async def test_bank_callbacks_are_not_payment_authority(callback, credentials, body, event):
    assert (await send(callback, credentials, {**body, "type": event})).status_code == 200
    callback.state.receipts.record.assert_not_awaited()


@pytest.mark.parametrize(
    "headers", [{"Origin": "https://glprofessional.ru"}, {"Cookie": "session=synthetic"}]
)
async def test_browser_requests_denied(callback, credentials, body, headers):
    assert (await send(callback, credentials, body, headers=headers)).status_code == 403
    callback.state.receipts.record.assert_not_awaited()


async def test_invalid_duplicate_and_cross_order_tokens_never_write(callback, credentials, body):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=callback), base_url="https://test"
    ) as client:
        for query in [
            "",
            "?token=wrong",
            "?token=" + credentials.token(str(uuid4())),
            "?token=a&token=b",
            "?token=a&environment=production",
        ]:
            assert (
                await client.post("/callbacks/atol/receipts" + query, json=body)
            ).status_code == 401
    callback.state.receipts.record.assert_not_awaited()


@pytest.mark.parametrize(
    "error,code",
    [(ReceiptUnavailable("safe"), 409), (asyncpg.PostgresError("private password"), 503)],
)
async def test_database_error_does_not_acknowledge_success_or_leak(
    callback, credentials, body, error, code, caplog
):
    callback.state.receipts.record.side_effect = error
    response = await send(callback, credentials, body)
    assert response.status_code == code
    assert "private password" not in response.text + caplog.text


async def test_callback_application_has_no_internal_api_or_docs(callback):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=callback), base_url="https://test"
    ) as client:
        for path in [
            "/internal/checkouts",
            "/internal/order-intents",
            "/internal/bot/record_receipt",
            "/docs",
            "/openapi.json",
        ]:
            assert (await client.post(path, json={})).status_code == 404


async def test_oversize_body_rejected_without_write(callback, credentials, body):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=callback), base_url="https://test"
    ) as client:
        response = await client.post(
            "/callbacks/atol/receipts?token=" + credentials.token(body["orderId"]),
            content=b"x" * 16385,
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 413
    callback.state.receipts.record.assert_not_awaited()
