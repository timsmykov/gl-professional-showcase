"""Orchestration failure boundaries independent of provider and SQL transport."""

import asyncio
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from gl_api.atol_pay import (
    AtolPayError,
    PaymentStatus,
    ReceiptPolicy,
    RegisteredPayment,
    RegistrationUncertain,
)
from gl_api.checkout import CheckoutPending, CheckoutService
from gl_api.payments import CheckoutUnavailable


@pytest.fixture
def checkout():
    attempt = {
        "merchant_order_id": uuid4(),
        "amount_kopeks": 1000,
        "recording_name": "Synthetic",
        "review_required": False,
        "registration_state": "reserved",
        "payment_url": None,
        "environment": "sandbox",
        "poll_lease": uuid4(),
    }
    repo, client = AsyncMock(), AsyncMock()
    repo.reserve.return_value = (attempt, True)
    repo.claim_poll.return_value = attempt
    client.register.return_value = RegisteredPayment(
        str(attempt["merchant_order_id"]), 1000, "https://pay.example/synthetic"
    )
    client.status.return_value = PaymentStatus.PAID
    repo.observe.return_value = True
    return (
        CheckoutService(
            repo, client, "sandbox", ReceiptPolicy(1, 5, 3, 3), "https://t.me/glprofessional_bot"
        ),
        repo,
        client,
        attempt,
    )


async def test_commit_registration_before_returning_link(checkout):
    service, repo, client, _attempt = checkout
    result = await service.checkout(123, 1)
    assert result.payment_url == "https://pay.example/synthetic"
    repo.registered.assert_awaited_once_with(1, client.register.return_value)
    client.register.assert_awaited_once()
    assert "synthetic" not in repr(result)
    assert repo.observe.await_count == 0


async def test_notification_credential_scoped_to_attempt_and_not_reissued_on_retry(checkout):
    from gl_api.fiscal_notifications import NotificationCredentials

    service, repo, client, attempt = checkout
    credentials = NotificationCredentials(
        "https://callbacks.example/callbacks/atol/receipts", "sandbox", b"x" * 32
    )
    service.notifications = credentials
    result = await service.checkout(123, 1)
    url = client.register.await_args.kwargs["notification_url"]
    assert url == credentials.notification_url(str(attempt["merchant_order_id"]))
    assert credentials.token(str(attempt["merchant_order_id"])) not in repr(result)
    attempt.update(registration_state="registered", payment_url=result.payment_url)
    repo.reserve.return_value = (attempt, False)
    await service.checkout(123, 1)
    assert client.register.await_count == 1


@pytest.mark.parametrize("state", ["reserved", "uncertain"])
async def test_existing_uncertain_attempt_never_retries_post(checkout, state):
    service, repo, client, attempt = checkout
    attempt["registration_state"] = state
    repo.reserve.return_value = (attempt, False)
    with pytest.raises(CheckoutPending):
        await service.checkout(123, 1)
    client.register.assert_not_awaited()


async def test_existing_link_returned_without_registration(checkout):
    service, repo, client, attempt = checkout
    attempt.update(registration_state="registered", payment_url="https://pay.example/synthetic")
    repo.reserve.return_value = (attempt, False)
    assert (await service.checkout(123, 1)).payment_url == attempt["payment_url"]
    client.register.assert_not_awaited()


async def test_timeout_commits_uncertain_state_not_a_second_payment(checkout):
    service, repo, client, _ = checkout
    client.register.side_effect = RegistrationUncertain("safe")
    with pytest.raises(CheckoutPending):
        await service.checkout(123, 1)
    repo.uncertain.assert_awaited_once_with(1)
    repo.registered.assert_not_awaited()
    assert client.register.await_count == 1


async def test_cancelled_request_never_returns_or_confirms_payment(checkout):
    service, repo, client, _ = checkout
    client.register.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await service.checkout(123, 1)
    repo.registered.assert_not_awaited()
    repo.observe.assert_not_awaited()


async def test_failed_sql_commit_does_not_return_link(checkout):
    service, repo, client, _ = checkout
    repo.registered.side_effect = OSError("database unavailable")
    with pytest.raises(OSError):
        await service.checkout(123, 1)
    assert client.register.await_count == 1
    repo.observe.assert_not_awaited()


async def test_failed_provider_status_releases_lease_without_grant(checkout):
    service, repo, client, attempt = checkout
    client.status.side_effect = AtolPayError("safe")
    assert not await service.reconcile(1)
    repo.poll_failed.assert_awaited_once_with(1, attempt["poll_lease"])
    repo.observe.assert_not_awaited()


async def test_namespace_mismatch_does_not_contact_provider(checkout):
    service, repo, client, attempt = checkout
    attempt["environment"] = "production"
    with pytest.raises(CheckoutUnavailable):
        await service.reconcile(1)
    client.status.assert_not_awaited()
    repo.observe.assert_not_awaited()


async def test_status_only_reaches_fenced_observation(checkout):
    service, repo, _client, attempt = checkout
    assert await service.reconcile(1)
    repo.observe.assert_awaited_once_with(1, attempt["poll_lease"], PaymentStatus.PAID)


async def test_review_required_cannot_return_checkout(checkout):
    service, _repo, client, attempt = checkout
    attempt["review_required"] = True
    with pytest.raises(CheckoutUnavailable):
        await service.checkout(123, 1)
    client.register.assert_not_awaited()
