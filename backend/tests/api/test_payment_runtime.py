"""Disabled defaults, private checkout boundary, durable worker and safe shutdown."""

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import ValidationError

from gl_api.app import create_app
from gl_api.checkout import CheckoutLink, CheckoutPending
from gl_api.config import ConfigurationError, Settings, load_settings
from gl_api.payment_config import PaymentSettings
from gl_api.payment_worker import PaymentReconciler
from gl_api.payments import CheckoutUnavailable

TOKEN = "synthetic-internal-token-1234567890"
PRIVATE = "synthetic-private-payment-detail"
BODY = {"telegram_id": -9001, "order_id": 1}


@pytest.fixture
def options(tmp_path):
    token = tmp_path / "merchant-token"
    token.write_text("synthetic-atol-credential")
    return {
        "environment": "sandbox",
        "token_file": token,
        "payment_hosts": ["pay.example"],
        "return_url": "https://t.me/glprofessional_bot",
        "sno": 1,
        "tax": 5,
        "payment_method": 3,
        "payment_subject": 3,
    }


@pytest.fixture
def app(options, tmp_path):
    token = tmp_path / "internal-token"
    token.write_text(TOKEN)
    result = create_app(Settings(payment=PaymentSettings(**options), bot_token_file=token))
    result.state.checkout = AsyncMock()
    result.state.checkout.checkout.return_value = CheckoutLink(1, "https://pay.example/synthetic")
    return result


async def post(app, payload=BODY, headers=None):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.post("/internal/checkouts", json=payload, headers=headers)


async def test_checkout_default_off_and_auth_required(options):
    assert (await post(create_app(Settings()))).status_code == 404
    with pytest.raises(ConfigurationError, match="requires internal service authentication"):
        create_app(Settings(payment=PaymentSettings(**options)))


async def test_private_route_auth_browser_and_payload_denial(app, caplog):
    assert (await post(app)).status_code == 401
    auth = {"Authorization": f"Bearer {TOKEN}"}
    assert (
        await post(app, headers={**auth, "Origin": "https://glprofessional.ru"})
    ).status_code == 403
    for payload in (
        {**BODY, "amount_kopeks": 1},
        {**BODY, "status": "paid"},
        {**BODY, "email": PRIVATE},
        {**BODY, "telegram_id": True},
        {**BODY, "order_id": 0},
        {**BODY, "order_id": "1"},
    ):
        response = await post(app, payload, auth)
        assert response.status_code == 422
        assert PRIVATE not in response.text
    app.state.checkout.checkout.assert_not_awaited()
    response = await post(app, headers=auth)
    assert response.status_code == 200
    assert response.json() == {"order_id": 1, "payment_url": "https://pay.example/synthetic"}
    assert response.headers["cache-control"] == "no-store"
    app.state.checkout.checkout.assert_awaited_once_with(-9001, 1)
    assert TOKEN not in caplog.text and PRIVATE not in caplog.text


@pytest.mark.parametrize(
    "error,code",
    [
        (CheckoutPending(PRIVATE), 409),
        (CheckoutUnavailable(PRIVATE), 404),
        (OSError(PRIVATE), 503),
    ],
)
async def test_checkout_errors_are_safe(app, caplog, error, code):
    app.state.checkout.checkout.side_effect = error
    response = await post(app, headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == code
    assert PRIVATE not in response.text and PRIVATE not in caplog.text


@pytest.mark.parametrize(
    "override",
    [
        {"sno": 3},
        {"payment_subject": True},
        {"tax": -1},
        {"payment_hosts": []},
        {"payment_hosts": ["*.example"]},
        {"payment_hosts": ["pay..example"]},
        {"return_url": "http://pay.example"},
        {"return_url": "https://user:pw@pay.example"},
        {"return_url": "https://pay.example:444"},
    ],
)
def test_explicit_configuration_rejects_unsafe_values(options, override):
    with pytest.raises(ValidationError):
        PaymentSettings(**(options | override))


async def test_worker_does_not_die_or_log_details_for_one_failed_observation(caplog):
    service = AsyncMock()
    service.environment = "sandbox"
    service.repository.due.return_value = [1, 2]
    service.reconcile.side_effect = [OSError(PRIVATE), True]
    worker = PaymentReconciler(service)
    assert await worker.run_once() == 1
    service.repository.due.assert_awaited_once_with("sandbox", limit=20)
    assert service.reconcile.await_count == 2
    assert PRIVATE not in caplog.text


async def test_worker_recovers_db_outage_and_preserves_cancellation(caplog):
    service = AsyncMock()
    service.environment = "sandbox"
    service.repository.due.side_effect = [OSError(PRIVATE), []]
    worker = PaymentReconciler(service, interval=0.01)
    task = asyncio.create_task(worker.run())
    async with asyncio.timeout(1):
        while service.repository.due.await_count < 2:
            await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert PRIVATE not in caplog.text


async def test_payment_lifespan_cancels_worker_closes_session_and_pool(
    options, tmp_path, monkeypatch
):
    internal = tmp_path / "internal"
    internal.write_text(TOKEN)
    password = tmp_path / "password"
    password.write_text("synthetic-password")
    pool = AsyncMock()
    monkeypatch.setattr("gl_api.app.asyncpg.create_pool", AsyncMock(return_value=pool))
    started, stopped = asyncio.Event(), asyncio.Event()

    async def run(_self):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    monkeypatch.setattr("gl_api.app.PaymentReconciler.run", run)
    app = create_app(
        Settings(
            payment=PaymentSettings(**options),
            bot_token_file=internal,
            database_password_file=password,
        )
    )
    async with app.router.lifespan_context(app):
        await started.wait()
        session = app.state.checkout.client._session
        assert not session.closed and not session.trust_env
    assert stopped.is_set() and session.closed and app.state.stopping
    pool.close.assert_awaited_once()


def test_raw_merchant_credential_env_is_forbidden(monkeypatch):
    monkeypatch.setenv("GL_API_ATOL_TOKEN", PRIVATE)
    with pytest.raises(ConfigurationError, match="unsupported") as error:
        load_settings()
    assert PRIVATE not in str(error.value)
