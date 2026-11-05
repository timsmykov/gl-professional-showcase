"""Concurrency and payment atomicity on disposable PostgreSQL, never managed data."""

import asyncio
import os
from pathlib import Path
from uuid import UUID, uuid4

import aiohttp
import asyncpg
import httpx
import pytest
from aiohttp import web

import gl_api.atol_pay as provider_module
from gl_api.app import create_app
from gl_api.atol_pay import AtolPayClient, PaymentStatus, ReceiptPolicy, RegisteredPayment
from gl_api.checkout import CheckoutService
from gl_api.config import Settings
from gl_api.orders import OrderRepository
from gl_api.payment_config import PaymentSettings
from gl_api.payment_worker import PaymentReconciler
from gl_api.payments import CheckoutUnavailable, PaymentRepository

pytestmark = pytest.mark.skipif(
    os.getenv("GL_API_TEST_DATABASE") != "1", reason="disposable PostgreSQL required"
)
POLICY = ReceiptPolicy(1, 5, 3, 3)


@pytest.fixture
async def purchase():
    pool = await asyncpg.create_pool(Settings().database_url(), min_size=1, max_size=4)
    admin = await asyncpg.connect(
        Settings(
            database_user="gl_api_migrator",
            database_password_file=Path(os.environ["GL_API_TEST_MIGRATOR_PASSWORD_FILE"]),
        ).database_url()
    )
    tid = -int.from_bytes(uuid4().bytes[:7], "big")
    uid = await admin.fetchval("INSERT INTO users(telegram_id) VALUES($1) RETURNING id", tid)
    wid = await admin.fetchval(
        "INSERT INTO webinars(title,price_kopeks,active) VALUES('Synthetic payment',1000,true) RETURNING id"
    )
    order = await OrderRepository(pool).create_pending_order(tid, wid, -tid)
    try:
        yield PaymentRepository(pool), admin, tid, uid, wid, order["id"]
    finally:
        await admin.execute(
            "DELETE FROM receipt_observations WHERE receipt_id IN (SELECT receipt_id FROM payment_receipts WHERE order_id=$1)",
            order["id"],
        )
        await admin.execute("DELETE FROM payment_receipts WHERE order_id=$1", order["id"])
        await admin.execute("DELETE FROM webinar_entitlements WHERE user_id=$1", uid)
        await admin.execute("DELETE FROM payment_events WHERE order_id=$1", order["id"])
        await admin.execute("DELETE FROM atol_payment_attempts WHERE order_id=$1", order["id"])
        await admin.execute("DELETE FROM order_intent_requests WHERE order_id=$1", order["id"])
        await admin.execute("DELETE FROM purchase_orders WHERE id=$1", order["id"])
        await admin.execute("DELETE FROM webinars WHERE id=$1", wid)
        await admin.execute("DELETE FROM users WHERE id=$1", uid)
        await admin.close()
        await pool.close()


async def setup_registered(purchase):
    repo, _, tid, _, _, oid = purchase
    row, created = await repo.reserve(tid, oid, "sandbox", POLICY)
    assert created
    await repo.registered(
        oid,
        RegisteredPayment(str(row["merchant_order_id"]), 1000, "https://pay.example/synthetic"),
    )
    return row


async def next_poll(purchase):
    repo, admin, _, _, _, oid = purchase
    await admin.execute(
        "UPDATE atol_payment_attempts SET next_poll_at=clock_timestamp() WHERE order_id=$1", oid
    )
    row = await repo.claim_poll(oid)
    assert row is not None
    return row["poll_lease"]


@pytest.fixture
async def receipt_pool(purchase):
    """Prove the exact proposed grants on a disposable DB, never managed roles."""
    role = "synthetic_receipt_" + uuid4().hex
    root_settings = Settings(
        database_user="postgres",
        database_password_file=Path(os.environ["GL_API_TEST_ADMIN_PASSWORD_FILE"]),
    )
    root = await asyncpg.connect(root_settings.database_url())
    await root.execute(
        f'CREATE ROLE "{role}" NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS'
    )
    # Provider UI may provision a broad bundle. The reviewed grants must remove
    # it before enabling a callback login, including future-object privileges.
    await root.execute(f'GRANT ALL ON ALL TABLES IN SCHEMA public TO "{role}"')
    await root.execute(f'GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO "{role}"')
    await root.execute(f'GRANT CREATE ON SCHEMA public TO "{role}"')
    await root.execute(
        f'ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO "{role}"'
    )
    sql = (
        Path(__file__).resolve().parents[2] / "infra/timeweb/fiscal-role-grants.sql"
    ).read_text()
    await root.execute(sql.replace(':"receipt_role"', '"' + role + '"'))

    async def scoped(connection):
        await connection.execute(f'SET ROLE "{role}"')

    pool = await asyncpg.create_pool(
        root_settings.database_url(), min_size=0, max_size=3, setup=scoped
    )
    try:
        yield pool
    finally:
        await pool.close()
        await root.execute(f'DROP OWNED BY "{role}"')
        await root.execute(f'DROP ROLE "{role}"')
        await root.close()


async def test_authenticated_fiscal_http_handles_early_replays_without_payment_grant(
    purchase, tmp_path, receipt_pool
):
    from gl_api.fiscal import ReceiptRepository
    from gl_api.fiscal_callback import FiscalSettings
    from gl_api.fiscal_callback import create_app as callback_app
    from gl_api.fiscal_notifications import NotificationCredentials

    repo, admin, tid, uid, _, oid = purchase
    row, _ = await repo.reserve(tid, oid, "sandbox", POLICY)
    secret = tmp_path / "callback-key"
    secret.write_text("ab" * 32)
    credentials = NotificationCredentials.from_file(
        "https://callbacks.example/callbacks/atol/receipts", "sandbox", secret
    )
    app = callback_app(
        FiscalSettings(
            environment="sandbox",
            notification_endpoint=credentials.endpoint,
            notification_key_file=secret,
            require_database_tls=False,
        )
    )
    app.state.receipts = ReceiptRepository(receipt_pool)
    body = {
        "orderId": str(row["merchant_order_id"]),
        "receiptId": str(uuid4()),
        "type": "fiscal",
        "receiptType": "sell",
        "amount": 1000,
        "sessionType": "oneStep",
        "status": "success",
        "email": "synthetic-private@example.test",
    }
    path = "/callbacks/atol/receipts?token=" + credentials.token(body["orderId"])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://test"
    ) as client:
        # Provider may notify before POST response or before the reconciler marks PAID.
        responses = await asyncio.gather(*[client.post(path, json=body) for _ in range(8)])
        assert all(response.status_code == 200 for response in responses)
        assert (await client.post(path, json={**body, "amount": 999})).status_code == 409
        assert (await client.post(path, json={**body, "status": "failed"})).status_code == 200
        assert (
            await client.post(path, json={**body, "type": "payment", "paymentStatus": 1})
        ).status_code == 200
    assert (
        await admin.fetchval("SELECT count(*) FROM payment_receipts WHERE order_id=$1", oid) == 1
    )
    assert (
        await admin.fetchval(
            "SELECT count(*) FROM receipt_observations WHERE receipt_id=$1",
            UUID(body["receiptId"]),
        )
        == 2
    )
    assert await admin.fetchval("SELECT status FROM purchase_orders WHERE id=$1", oid) == "pending"
    assert (
        await admin.fetchval(
            "SELECT registration_state FROM atol_payment_attempts WHERE order_id=$1", oid
        )
        == "reserved"
    )
    assert await admin.fetchval("SELECT count(*) FROM payment_events WHERE order_id=$1", oid) == 0
    assert (
        await admin.fetchval("SELECT count(*) FROM webinar_entitlements WHERE user_id=$1", uid)
        == 0
    )
    async with receipt_pool.acquire() as limited:
        for statement in (
            "SELECT telegram_id FROM users LIMIT 1",
            "SELECT user_id FROM purchase_orders LIMIT 1",
            "SELECT payment_url FROM atol_payment_attempts LIMIT 1",
            "UPDATE purchase_orders SET status='paid' WHERE false",
            "INSERT INTO payment_events(provider,provider_event_id,order_id,event_type) VALUES('synthetic','invalid',1,'paid')",
            "DELETE FROM payment_receipts WHERE false",
            "UPDATE receipt_observations SET state='success' WHERE false",
        ):
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await limited.execute(statement)


async def test_concurrent_checkout_has_exactly_one_registration_owner(purchase):
    repo, admin, tid, _, _, oid = purchase
    outcomes = await asyncio.gather(*[repo.reserve(tid, oid, "sandbox", POLICY) for _ in range(8)])
    assert sum(created for _, created in outcomes) == 1
    assert len({row["merchant_order_id"] for row, _ in outcomes}) == 1
    await repo.uncertain(oid)
    row, created = await repo.reserve(tid, oid, "sandbox", POLICY)
    assert not created and row["registration_state"] == "uncertain"
    with pytest.raises(CheckoutUnavailable):
        await repo.reserve(tid, oid, "production", POLICY)
    with pytest.raises(CheckoutUnavailable):
        await repo.reserve(tid - 1, oid, "sandbox", POLICY)
    assert await admin.fetchval("SELECT count(*) FROM payment_events WHERE order_id=$1", oid) == 0


@pytest.mark.parametrize(
    "status",
    [status for status in PaymentStatus if status is not PaymentStatus.PAID],
)
async def test_non_paid_status_cannot_grant(purchase, status):
    repo, admin, _, uid, _, oid = purchase
    await setup_registered(purchase)
    assert await repo.observe(oid, await next_poll(purchase), status)
    expected = (
        "refunded"
        if status is PaymentStatus.REFUNDED
        else "cancelled"
        if status
        in {
            PaymentStatus.CANCELLED,
            PaymentStatus.FAILED,
            PaymentStatus.EXPIRED,
            PaymentStatus.FRAUD,
        }
        else "pending"
    )
    assert await admin.fetchval("SELECT status FROM purchase_orders WHERE id=$1", oid) == expected
    assert (
        await admin.fetchval("SELECT count(*) FROM webinar_entitlements WHERE user_id=$1", uid)
        == 0
    )


async def test_paid_replay_refund_and_stale_poll_preserve_invariants(purchase):
    repo, admin, _, uid, _, oid = purchase
    await setup_registered(purchase)
    lease = await next_poll(purchase)
    assert await repo.claim_poll(oid) is None
    assert not await repo.observe(oid, uuid4(), PaymentStatus.PAID)
    assert await repo.observe(oid, lease, PaymentStatus.PAID)
    assert not await repo.observe(oid, lease, PaymentStatus.PAID)
    assert await repo.observe(oid, await next_poll(purchase), PaymentStatus.PAID)
    assert await admin.fetchval("SELECT status FROM purchase_orders WHERE id=$1", oid) == "paid"
    assert (
        await admin.fetchval(
            "SELECT payment_order_id FROM webinar_entitlements WHERE user_id=$1", uid
        )
        == oid
    )
    assert await admin.fetchval("SELECT count(*) FROM payment_events WHERE order_id=$1", oid) == 1
    assert await repo.observe(oid, await next_poll(purchase), PaymentStatus.REFUNDED)
    assert (
        await admin.fetchval("SELECT status FROM purchase_orders WHERE id=$1", oid) == "refunded"
    )
    assert (
        await admin.fetchval("SELECT count(*) FROM webinar_entitlements WHERE user_id=$1", uid)
        == 0
    )
    assert await repo.observe(oid, await next_poll(purchase), PaymentStatus.PAID)
    assert (
        await admin.fetchval("SELECT status FROM purchase_orders WHERE id=$1", oid) == "refunded"
    )
    assert (
        await admin.fetchval("SELECT count(*) FROM webinar_entitlements WHERE user_id=$1", uid)
        == 0
    )


async def test_uncertain_registration_requires_review_not_grant(purchase):
    repo, admin, tid, uid, _, oid = purchase
    await repo.reserve(tid, oid, "sandbox", POLICY)
    await repo.uncertain(oid)
    assert await repo.observe(oid, await next_poll(purchase), PaymentStatus.PAID)
    assert await admin.fetchval("SELECT status FROM purchase_orders WHERE id=$1", oid) == "pending"
    assert await admin.fetchval(
        "SELECT review_required FROM atol_payment_attempts WHERE order_id=$1", oid
    )
    assert (
        await admin.fetchval("SELECT count(*) FROM webinar_entitlements WHERE user_id=$1", uid)
        == 0
    )


async def test_refund_never_deletes_imported_access(purchase):
    repo, admin, _, uid, wid, oid = purchase
    await setup_registered(purchase)
    await admin.execute(
        "INSERT INTO webinar_entitlements(user_id,webinar_id,source) VALUES($1,$2,'imported')",
        uid,
        wid,
    )
    assert await repo.observe(oid, await next_poll(purchase), PaymentStatus.PAID)
    assert await repo.observe(oid, await next_poll(purchase), PaymentStatus.REFUNDED)
    assert (
        await admin.fetchval("SELECT source FROM webinar_entitlements WHERE user_id=$1", uid)
        == "imported"
    )


async def test_expired_poll_lease_cannot_overwrite_successor(purchase):
    repo, admin, _, _, _, oid = purchase
    await setup_registered(purchase)
    old = await next_poll(purchase)
    await admin.execute(
        "UPDATE atol_payment_attempts SET poll_expires_at=clock_timestamp()-interval '1 second' WHERE order_id=$1",
        oid,
    )
    new = await repo.claim_poll(oid)
    assert new is not None and new["poll_lease"] != old
    assert not await repo.observe(oid, old, PaymentStatus.PAID)
    await repo.poll_failed(oid, old)
    assert (
        await admin.fetchval("SELECT poll_lease FROM atol_payment_attempts WHERE order_id=$1", oid)
        == new["poll_lease"]
    )


async def test_registration_mismatch_leaves_attempt_unverified(purchase):
    repo, admin, tid, _, _, oid = purchase
    row, _ = await repo.reserve(tid, oid, "sandbox", POLICY)
    with pytest.raises(CheckoutUnavailable):
        await repo.registered(
            oid, RegisteredPayment(str(row["merchant_order_id"]), 1001, "https://pay.example/")
        )
    assert (
        await admin.fetchval(
            "SELECT registration_state FROM atol_payment_attempts WHERE order_id=$1", oid
        )
        == "reserved"
    )


async def test_payment_transaction_rolls_back_all_effects(purchase):
    repo, admin, _, uid, _, oid = purchase
    await setup_registered(purchase)
    lease = await next_poll(purchase)
    # Force failure after the order/grant writes, without changing shared roles.
    constraint = "synthetic_reject_" + uuid4().hex
    await admin.execute(
        f"ALTER TABLE payment_events ADD CONSTRAINT {constraint} CHECK (order_id <> {oid})"
    )
    try:
        with pytest.raises(asyncpg.CheckViolationError):
            await repo.observe(oid, lease, PaymentStatus.PAID)
        assert (
            await admin.fetchval("SELECT status FROM purchase_orders WHERE id=$1", oid)
            == "pending"
        )
        assert await admin.fetchval("SELECT paid_at FROM purchase_orders WHERE id=$1", oid) is None
        assert (
            await admin.fetchval("SELECT count(*) FROM webinar_entitlements WHERE user_id=$1", uid)
            == 0
        )
        assert (
            await admin.fetchval(
                "SELECT poll_lease FROM atol_payment_attempts WHERE order_id=$1", oid
            )
            == lease
        )
    finally:
        await admin.execute(f"ALTER TABLE payment_events DROP CONSTRAINT {constraint}")
    assert await repo.observe(oid, lease, PaymentStatus.PAID)


async def test_payment_role_cannot_modify_amount_or_catalog(purchase):
    repo, _, _, _, _, oid = purchase
    async with repo.pool.acquire() as app:
        for sql in (
            "UPDATE purchase_orders SET amount_kopeks=1 WHERE id=$1",
            "UPDATE purchase_orders SET user_id=1 WHERE id=$1",
            "UPDATE webinars SET price_kopeks=1 WHERE id=$1",
            "UPDATE webinar_entitlements SET source='imported' WHERE user_id=$1",
        ):
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await app.execute(sql, oid)


@pytest.mark.parametrize(
    "status", [PaymentStatus.PARTIALLY_CANCELLED, PaymentStatus.PARTIALLY_REFUNDED]
)
async def test_partial_result_requires_review_and_cannot_later_grant(purchase, status):
    repo, admin, _, uid, _, oid = purchase
    await setup_registered(purchase)
    assert await repo.observe(oid, await next_poll(purchase), status)
    assert await repo.observe(oid, await next_poll(purchase), PaymentStatus.PAID)
    assert await admin.fetchval("SELECT status FROM purchase_orders WHERE id=$1", oid) == "pending"
    assert (
        await admin.fetchval("SELECT count(*) FROM webinar_entitlements WHERE user_id=$1", uid)
        == 0
    )


async def test_internal_checkout_and_restarted_reconciler_grant_existing_order(
    purchase, tmp_path, monkeypatch
):
    repo, admin, tid, uid, _, oid = purchase
    merchant = tmp_path / "merchant"
    merchant.write_text("synthetic-merchant-credential")
    internal = tmp_path / "internal"
    token = "synthetic-internal-token-1234567890"
    internal.write_text(token)
    options = PaymentSettings(
        environment="sandbox",
        token_file=merchant,
        payment_hosts=["pay.example"],
        return_url="https://t.me/glprofessional_bot",
        sno=1,
        tax=5,
        payment_method=3,
        payment_subject=3,
    )
    calls = []

    async def provider_handler(request):
        assert request.headers["Authorization"] == "Bearer synthetic-merchant-credential"
        calls.append(request.method)
        if request.method == "POST":
            payload = await request.json()
            assert payload["amount"] == 1000 and payload["sessionType"] == "oneStep"
            assert "buyer" not in payload["receipt"]
            return web.json_response(
                {
                    "status": "success",
                    "data": {
                        "orderId": payload["orderId"],
                        "amount": payload["amount"],
                        "paymentUrls": {"main": "https://pay.example/synthetic"},
                    },
                }
            )
        return web.json_response({"status": "success", "data": {"status": 1}})

    fake = web.Application()
    fake.router.add_route("*", "/{tail:.*}", provider_handler)
    runner = web.AppRunner(fake, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    monkeypatch.setitem(
        provider_module.ORIGINS,
        "sandbox",
        f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}",
    )
    session = aiohttp.ClientSession(trust_env=False)
    provider = AtolPayClient(
        session, merchant, environment="sandbox", payment_hosts=options.payment_hosts
    )
    try:
        service = CheckoutService(repo, provider, "sandbox", POLICY, options.return_url)
        app = create_app(Settings(payment=options, bot_token_file=internal))
        app.state.checkout = service
        app.state.pool = repo.pool
        headers = {"Authorization": f"Bearer {token}"}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            body = {"telegram_id": tid, "order_id": oid}
            assert (await client.post("/internal/checkouts", json=body)).status_code == 401
            assert (
                await client.post(
                    "/internal/checkouts", json=body | {"status": "paid"}, headers=headers
                )
            ).status_code == 422
            for _ in range(2):
                response = await client.post("/internal/checkouts", json=body, headers=headers)
                assert response.status_code == 200
        assert calls.count("POST") == 1
        assert (
            await admin.fetchval("SELECT count(*) FROM webinar_entitlements WHERE user_id=$1", uid)
            == 0
        )
        await admin.execute(
            "UPDATE atol_payment_attempts SET next_poll_at=clock_timestamp() WHERE order_id=$1",
            oid,
        )
        assert await repo.due("production") == []
        # New service instance discovers persisted work without an in-memory queue.
        resumed = CheckoutService(
            PaymentRepository(repo.pool), provider, "sandbox", POLICY, options.return_url
        )
        assert await PaymentReconciler(resumed).run_once() == 1
        assert (
            await admin.fetchval("SELECT status FROM purchase_orders WHERE id=$1", oid) == "paid"
        )
        assert (
            await admin.fetchval(
                "SELECT payment_order_id FROM webinar_entitlements WHERE user_id=$1", uid
            )
            == oid
        )
        assert calls.count("GET") == 1
        assert await PaymentReconciler(resumed).run_once() == 0
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/internal/bot/list_entitlements", json={"args": [uid]}, headers=headers
            )
            assert response.status_code == 200
            assert response.json()["result"] == [{"title": "Synthetic payment", "source": "paid"}]
    finally:
        await session.close()
        await runner.cleanup()


@pytest.mark.parametrize("invalid", ["amount", "title"])
async def test_unregistrable_order_does_not_reserve_attempt(purchase, invalid):
    repo, admin, tid, _, wid, oid = purchase
    if invalid == "amount":
        await admin.execute("UPDATE purchase_orders SET amount_kopeks=2000000001 WHERE id=$1", oid)
    else:
        await admin.execute("UPDATE webinars SET title=$2 WHERE id=$1", wid, "Synthetic\tname")
    with pytest.raises(CheckoutUnavailable):
        await repo.reserve(tid, oid, "sandbox", POLICY)
    assert (
        await admin.fetchval("SELECT count(*) FROM atol_payment_attempts WHERE order_id=$1", oid)
        == 0
    )


async def test_expired_observation_cannot_grant_even_without_a_successor(purchase):
    repo, admin, _, uid, _, oid = purchase
    await setup_registered(purchase)
    lease = await next_poll(purchase)
    await admin.execute(
        "UPDATE atol_payment_attempts SET poll_expires_at=clock_timestamp()-interval '1 second' WHERE order_id=$1",
        oid,
    )
    assert not await repo.observe(oid, lease, PaymentStatus.PAID)
    assert await admin.fetchval("SELECT status FROM purchase_orders WHERE id=$1", oid) == "pending"
    assert (
        await admin.fetchval("SELECT count(*) FROM webinar_entitlements WHERE user_id=$1", uid)
        == 0
    )


async def test_fiscal_failure_preserves_paid_order_and_entitlement(purchase):
    from gl_api.fiscal import FiscalState, ReceiptObservation, ReceiptRepository

    repo, admin, _, uid, _, oid = purchase
    row = await setup_registered(purchase)
    await repo.observe(oid, await next_poll(purchase), PaymentStatus.PAID)
    observation = ReceiptObservation(
        row["merchant_order_id"], uuid4(), "sandbox", "sell", 1000, FiscalState.FAILED
    )
    ledger = ReceiptRepository(repo.pool)
    assert await ledger.record(observation) is FiscalState.FAILED
    assert await admin.fetchval("SELECT status FROM purchase_orders WHERE id=$1", oid) == "paid"
    assert (
        await admin.fetchval(
            "SELECT payment_order_id FROM webinar_entitlements WHERE user_id=$1", uid
        )
        == oid
    )
    assert await admin.fetchval("SELECT count(*) FROM payment_events WHERE order_id=$1", oid) == 1
    async with repo.pool.acquire() as app:
        for sql in (
            "DELETE FROM payment_receipts WHERE receipt_id=$1",
            "DELETE FROM receipt_observations WHERE receipt_id=$1",
            "UPDATE payment_receipts SET amount_kopeks=1 WHERE receipt_id=$1",
            "UPDATE receipt_observations SET state='success' WHERE receipt_id=$1",
        ):
            with pytest.raises(asyncpg.InsufficientPrivilegeError):
                await app.execute(sql, observation.receipt_id)


async def test_duplicate_and_conflicting_receipts_are_append_only(purchase):
    from dataclasses import replace

    from gl_api.fiscal import FiscalState, ReceiptObservation, ReceiptRepository

    repo, admin, _, uid, _, oid = purchase
    row = await setup_registered(purchase)
    observation = ReceiptObservation(
        row["merchant_order_id"], uuid4(), "sandbox", "sell", 1000, FiscalState.SUCCESS
    )
    ledger = ReceiptRepository(repo.pool)
    outcomes = await asyncio.gather(*[ledger.record(observation) for _ in range(6)])
    assert outcomes == [FiscalState.SUCCESS] * 6
    assert (
        await admin.fetchval(
            "SELECT count(*) FROM receipt_observations WHERE receipt_id=$1", observation.receipt_id
        )
        == 1
    )
    assert (
        await ledger.record(replace(observation, state=FiscalState.FAILED)) is FiscalState.REVIEW
    )
    assert await ledger.record(observation) is FiscalState.REVIEW
    assert (
        await admin.fetchval(
            "SELECT count(*) FROM receipt_observations WHERE receipt_id=$1", observation.receipt_id
        )
        == 2
    )
    # Fiscal success is not authoritative confirmation of a bank payment.
    assert await admin.fetchval("SELECT status FROM purchase_orders WHERE id=$1", oid) == "pending"
    assert (
        await admin.fetchval("SELECT count(*) FROM webinar_entitlements WHERE user_id=$1", uid)
        == 0
    )


async def test_fiscal_identity_amount_environment_and_registration_must_match(purchase):
    from dataclasses import replace

    from gl_api.fiscal import (
        FiscalState,
        ReceiptObservation,
        ReceiptRepository,
        ReceiptUnavailable,
    )

    repo, admin, tid, _, _, oid = purchase
    row, _ = await repo.reserve(tid, oid, "sandbox", POLICY)
    observation = ReceiptObservation(
        row["merchant_order_id"], uuid4(), "sandbox", "sell", 1000, FiscalState.SUCCESS
    )
    ledger = ReceiptRepository(repo.pool)
    with pytest.raises(ReceiptUnavailable):
        await ledger.record(observation)
    await repo.registered(
        oid,
        RegisteredPayment(str(row["merchant_order_id"]), 1000, "https://pay.example/synthetic"),
    )
    for bad in (
        replace(observation, merchant_order_id=uuid4()),
        replace(observation, environment="production"),
        replace(observation, amount_kopeks=999),
        replace(observation, receipt_type="sell_refund", amount_kopeks=1001),
    ):
        with pytest.raises(ReceiptUnavailable):
            await ledger.record(bad)
    assert await ledger.record(observation) is FiscalState.SUCCESS
    with pytest.raises(ReceiptUnavailable):
        await ledger.record(replace(observation, receipt_type="sell_refund"))
    assert (
        await admin.fetchval(
            "SELECT receipt_type FROM payment_receipts WHERE receipt_id=$1", observation.receipt_id
        )
        == "sell"
    )
    assert (
        await admin.fetchval("SELECT count(*) FROM payment_receipts WHERE order_id=$1", oid) == 1
    )


async def test_refund_receipt_does_not_revoke_access_or_change_payment(purchase):
    from gl_api.fiscal import FiscalState, ReceiptObservation, ReceiptRepository

    repo, admin, _, uid, _, oid = purchase
    row = await setup_registered(purchase)
    await repo.observe(oid, await next_poll(purchase), PaymentStatus.PAID)
    ledger = ReceiptRepository(repo.pool)
    observation = ReceiptObservation(
        row["merchant_order_id"], uuid4(), "sandbox", "sell_refund", 500, FiscalState.SUCCESS
    )
    assert await ledger.record(observation) is FiscalState.SUCCESS
    assert await admin.fetchval("SELECT status FROM purchase_orders WHERE id=$1", oid) == "paid"
    assert (
        await admin.fetchval("SELECT count(*) FROM webinar_entitlements WHERE user_id=$1", uid)
        == 1
    )
    assert await repo.observe(oid, await next_poll(purchase), PaymentStatus.REFUNDED)
    assert (
        await admin.fetchval("SELECT count(*) FROM webinar_entitlements WHERE user_id=$1", uid)
        == 0
    )
    assert (
        await admin.fetchval("SELECT count(*) FROM payment_receipts WHERE order_id=$1", oid) == 1
    )


@pytest.mark.parametrize("state", ["pending", "paid", "cancelled", "refunded"])
async def test_order_status_is_owned_read_only_and_uses_order_price(purchase, state):
    from gl_api.orders import OrderUnavailable

    repo, admin, tid, _, wid, oid = purchase
    await admin.execute("UPDATE purchase_orders SET status=$2 WHERE id=$1", oid, state)
    await admin.execute("UPDATE webinars SET active=false,price_kopeks=2000 WHERE id=$1", wid)
    orders = OrderRepository(repo.pool)
    expected = {
        "id": oid,
        "webinar_id": wid,
        "amount_kopeks": 1000,
        "currency": "RUB",
        "status": state,
    }
    assert await orders.status(tid, oid) == expected
    for buyer, order in ((tid - 1, oid), (tid, 2**63 - 1)):
        with pytest.raises(OrderUnavailable):
            await orders.status(buyer, order)
    for buyer, order in ((0, oid), (tid, True), (True, oid), (tid, 0)):
        with pytest.raises(ValueError):
            await orders.status(buyer, order)
    assert await admin.fetchval("SELECT status FROM purchase_orders WHERE id=$1", oid) == state
    assert await admin.fetchval("SELECT count(*) FROM payment_events WHERE order_id=$1", oid) == 0
    assert (
        await admin.fetchval("SELECT count(*) FROM atol_payment_attempts WHERE order_id=$1", oid)
        == 0
    )


@pytest.fixture
async def payment_http(purchase, tmp_path, monkeypatch):
    """Real provider HTTP + real scoped SQL; no production credentials or network."""
    repo, _, _, _, _, _ = purchase
    merchant = tmp_path / "merchant"
    merchant.write_text("synthetic-audit-merchant-token")
    internal = tmp_path / "internal"
    token = "synthetic-audit-internal-token-1234567890"
    internal.write_text(token)
    private = "synthetic-buyer@example.invalid"
    calls = []
    control = {"status": 0, "registration": "valid"}

    async def handler(request):
        assert request.headers["Authorization"] == "Bearer synthetic-audit-merchant-token"
        calls.append(request.method)
        if request.method == "GET":
            return web.json_response(
                {"status": "success", "data": {"status": control["status"], "email": private}}
            )
        body = await request.json()
        assert body["amount"] == 1000
        assert "buyer" not in body["receipt"] and "telegram_id" not in body
        data = {
            "orderId": body["orderId"],
            "amount": body["amount"],
            "paymentUrls": {"main": "https://pay.example/synthetic-audit-link"},
            "email": private,
            "token": "synthetic-audit-merchant-token",
        }
        if control["registration"] == "amount":
            data["amount"] = 999
        elif control["registration"] == "id":
            data["orderId"] = str(uuid4())
        elif control["registration"] == "url":
            data["paymentUrls"]["main"] = "https://pay.example.attacker.invalid/"
        elif control["registration"] == "malformed":
            return web.Response(text=private)
        return web.json_response({"status": "success", "data": data})

    fake = web.Application()
    fake.router.add_route("*", "/{tail:.*}", handler)
    runner = web.AppRunner(fake, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    monkeypatch.setitem(
        provider_module.ORIGINS,
        "sandbox",
        f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}",
    )
    options = PaymentSettings(
        environment="sandbox",
        token_file=merchant,
        payment_hosts=["pay.example"],
        return_url="https://t.me/glprofessional_bot",
        sno=1,
        tax=5,
        payment_method=3,
        payment_subject=3,
    )
    try:
        async with aiohttp.ClientSession(trust_env=False) as session:
            provider = AtolPayClient(
                session, merchant, environment="sandbox", payment_hosts=options.payment_hosts
            )
            service = CheckoutService(repo, provider, "sandbox", POLICY, options.return_url)
            app = create_app(Settings(payment=options, bot_token_file=internal))
            app.state.pool, app.state.checkout = repo.pool, service
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                yield (
                    client,
                    service,
                    calls,
                    control,
                    {"Authorization": f"Bearer {token}"},
                    private,
                )
    finally:
        await runner.cleanup()


async def test_http_concurrency_payment_refund_and_data_minimization(
    purchase, payment_http, caplog
):
    repo, admin, tid, uid, wid, oid = purchase
    client, service, calls, control, auth, private = payment_http
    body = {"telegram_id": tid, "order_id": oid}
    # The catalogue price can change; registration still uses the frozen 1000.
    await admin.execute("UPDATE webinars SET price_kopeks=9000 WHERE id=$1", wid)
    responses = await asyncio.gather(
        *[client.post("/internal/checkouts", json=body, headers=auth) for _ in range(8)]
    )
    assert all(r.status_code in {200, 409} for r in responses)
    assert any(r.status_code == 200 for r in responses)
    assert calls == ["POST"]
    # Scheduling is persisted with a one-minute initial delay. Advance only the
    # synthetic row; the test does not change production polling policy.
    await admin.execute(
        "UPDATE atol_payment_attempts SET next_poll_at=clock_timestamp() WHERE order_id=$1", oid
    )
    assert await PaymentReconciler(service).run_once() == 1  # PROCESSING, not PAID
    assert await admin.fetchval("SELECT status FROM purchase_orders WHERE id=$1", oid) == "pending"
    assert (
        await admin.fetchval("SELECT count(*) FROM webinar_entitlements WHERE user_id=$1", uid)
        == 0
    )
    control["status"] = 1
    await admin.execute(
        "UPDATE atol_payment_attempts SET next_poll_at=clock_timestamp() WHERE order_id=$1",
        oid,
    )
    restarted = CheckoutService(
        PaymentRepository(repo.pool), service.client, "sandbox", POLICY, service.return_url
    )
    assert await PaymentReconciler(restarted).run_once() == 1
    status = await client.post("/internal/order-status", json=body, headers=auth)
    assert status.json() == {
        "id": oid,
        "webinar_id": wid,
        "amount_kopeks": 1000,
        "currency": "RUB",
        "status": "paid",
    }
    assert status.headers["cache-control"] == "no-store"
    assert (await client.post("/internal/checkouts", json=body, headers=auth)).status_code == 404
    assert (
        await admin.fetchval(
            "SELECT payment_order_id FROM webinar_entitlements WHERE user_id=$1", uid
        )
        == oid
    )
    # Inspect the actual stored rows, including default/raw fields, for provider extras.
    stored = await admin.fetchval(
        """SELECT jsonb_build_object('order',to_jsonb(o),'attempt',to_jsonb(a),
        'events',(SELECT jsonb_agg(to_jsonb(e)) FROM payment_events e WHERE e.order_id=o.id),
        'access',(SELECT jsonb_agg(to_jsonb(e)) FROM webinar_entitlements e WHERE e.user_id=o.user_id))::text
        FROM purchase_orders o JOIN atol_payment_attempts a ON a.order_id=o.id WHERE o.id=$1""",
        oid,
    )
    for sensitive in (private, "synthetic-audit-merchant-token", auth["Authorization"]):
        assert sensitive not in stored and sensitive not in caplog.text
        assert all(sensitive not in r.text for r in responses)
    assert "synthetic-audit-link" not in caplog.text and str(tid) not in caplog.text
    # Persisted checkout URL is necessary to reuse a link; it is private DB data.
    assert "https://pay.example/synthetic-audit-link" in stored
    for bank_status, expected in ((5, "refunded"), (1, "refunded")):
        control["status"] = bank_status
        await admin.execute(
            "UPDATE atol_payment_attempts SET next_poll_at=clock_timestamp() WHERE order_id=$1",
            oid,
        )
        assert await PaymentReconciler(restarted).run_once() == 1
        assert (
            await admin.fetchval("SELECT status FROM purchase_orders WHERE id=$1", oid) == expected
        )
        assert (
            await admin.fetchval("SELECT count(*) FROM webinar_entitlements WHERE user_id=$1", uid)
            == 0
        )
    assert await admin.fetchval("SELECT count(*) FROM payment_events WHERE order_id=$1", oid) == 3
    assert calls.count("POST") == 1


@pytest.mark.parametrize("mutation", ["amount", "id", "url", "malformed"])
async def test_http_unverified_registration_cannot_repost_or_grant(
    purchase, payment_http, mutation, caplog
):
    _, admin, tid, uid, _, oid = purchase
    client, service, calls, control, auth, private = payment_http
    control["registration"] = mutation
    body = {"telegram_id": tid, "order_id": oid}
    for _ in range(2):
        response = await client.post("/internal/checkouts", json=body, headers=auth)
        assert response.status_code == 409
        assert private not in response.text
    control["status"] = 1
    await admin.execute(
        "UPDATE atol_payment_attempts SET next_poll_at=clock_timestamp() WHERE order_id=$1", oid
    )
    assert await PaymentReconciler(service).run_once() == 1
    row = await admin.fetchrow(
        "SELECT registration_state,payment_url,review_required FROM atol_payment_attempts WHERE order_id=$1",
        oid,
    )
    assert dict(row) == {
        "registration_state": "uncertain",
        "payment_url": None,
        "review_required": True,
    }
    assert await admin.fetchval("SELECT status FROM purchase_orders WHERE id=$1", oid) == "pending"
    assert (
        await admin.fetchval("SELECT count(*) FROM webinar_entitlements WHERE user_id=$1", uid)
        == 0
    )
    assert calls == ["POST", "GET"]
    assert private not in caplog.text and str(tid) not in caplog.text


async def test_http_foreign_orders_and_hostile_input_have_no_payment_side_effects(
    purchase, payment_http, caplog
):
    _, admin, tid, uid, _, oid = purchase
    client, _, calls, _, auth, private = payment_http
    body = {"telegram_id": tid, "order_id": oid}
    for path in ("/internal/checkouts", "/internal/order-status"):
        foreign = await client.post(path, json=body | {"telegram_id": tid - 1}, headers=auth)
        missing = await client.post(path, json=body | {"order_id": 2**63 - 1}, headers=auth)
        assert foreign.status_code == missing.status_code == 404
        assert foreign.json() == missing.json()
        for headers, expected in (
            ({}, 401),
            ({**auth, "Origin": "null"}, 403),
            ({"Authorization": "Bearer synthetic-attacker"}, 401),
        ):
            assert (await client.post(path, json=body, headers=headers)).status_code == expected
        for extra in (
            {"status": "paid"},
            {"amount_kopeks": 1},
            {"email": private},
            {"order_id": "1 OR 1=1"},
            {"telegram_id": True},
            {"payment_url": private},
        ):
            response = await client.post(path, json=body | extra, headers=auth)
            assert response.status_code == 422 and private not in response.text
    assert calls == []
    for table in ("atol_payment_attempts", "payment_events"):
        assert await admin.fetchval(f"SELECT count(*) FROM {table} WHERE order_id=$1", oid) == 0
    assert (
        await admin.fetchval("SELECT count(*) FROM webinar_entitlements WHERE user_id=$1", uid)
        == 0
    )
    assert await admin.fetchval("SELECT status FROM purchase_orders WHERE id=$1", oid) == "pending"
    assert private not in caplog.text and str(tid) not in caplog.text


@pytest.mark.parametrize("status", [PaymentStatus.PAID, PaymentStatus.REFUNDED])
async def test_last_payment_write_failure_rolls_back_order_event_and_access(purchase, status):
    repo, admin, _, uid, _, oid = purchase
    await setup_registered(purchase)
    if status is PaymentStatus.REFUNDED:
        await repo.observe(oid, await next_poll(purchase), PaymentStatus.PAID)
    before_order = await admin.fetchrow(
        "SELECT status,paid_at FROM purchase_orders WHERE id=$1", oid
    )
    before_access = await admin.fetch("SELECT * FROM webinar_entitlements WHERE user_id=$1", uid)
    before_events = await admin.fetch("SELECT * FROM payment_events WHERE order_id=$1", oid)
    lease = await next_poll(purchase)
    constraint = "synthetic_final_" + uuid4().hex
    await admin.execute(
        f"ALTER TABLE atol_payment_attempts ADD CONSTRAINT {constraint} "
        f"CHECK (order_id <> {oid} OR last_status IS DISTINCT FROM {int(status)})"
    )
    try:
        with pytest.raises(asyncpg.CheckViolationError):
            await repo.observe(oid, lease, status)
        assert (
            await admin.fetchrow("SELECT status,paid_at FROM purchase_orders WHERE id=$1", oid)
            == before_order
        )
        assert (
            await admin.fetch("SELECT * FROM webinar_entitlements WHERE user_id=$1", uid)
            == before_access
        )
        assert (
            await admin.fetch("SELECT * FROM payment_events WHERE order_id=$1", oid)
            == before_events
        )
        assert (
            await admin.fetchval(
                "SELECT poll_lease FROM atol_payment_attempts WHERE order_id=$1", oid
            )
            == lease
        )
    finally:
        await admin.execute(f"ALTER TABLE atol_payment_attempts DROP CONSTRAINT {constraint}")
    assert await repo.observe(oid, lease, status)


async def test_http_sql_failure_after_provider_success_keeps_reservation_and_hides_url(
    purchase, payment_http, caplog
):
    _, admin, tid, uid, _, oid = purchase
    client, service, calls, control, auth, private = payment_http
    constraint = "synthetic_registration_" + uuid4().hex
    await admin.execute(
        f"ALTER TABLE atol_payment_attempts ADD CONSTRAINT {constraint} "
        f"CHECK (order_id <> {oid} OR registration_state <> 'registered')"
    )
    body = {"telegram_id": tid, "order_id": oid}
    try:
        response = await client.post("/internal/checkouts", json=body, headers=auth)
        assert response.status_code == 503
        assert "synthetic-audit-link" not in response.text
        row = await admin.fetchrow(
            "SELECT registration_state,payment_url FROM atol_payment_attempts WHERE order_id=$1",
            oid,
        )
        assert dict(row) == {"registration_state": "reserved", "payment_url": None}
    finally:
        await admin.execute(f"ALTER TABLE atol_payment_attempts DROP CONSTRAINT {constraint}")
    # The provider accepted the first POST. Removing the SQL failure is not permission
    # to register again, return an uncommitted URL, or trust an unverified PAID result.
    assert (await client.post("/internal/checkouts", json=body, headers=auth)).status_code == 409
    control["status"] = 1
    await admin.execute(
        "UPDATE atol_payment_attempts SET next_poll_at=clock_timestamp() WHERE order_id=$1", oid
    )
    assert await PaymentReconciler(service).run_once() == 1
    assert calls == ["POST", "GET"]
    assert await admin.fetchval("SELECT status FROM purchase_orders WHERE id=$1", oid) == "pending"
    assert (
        await admin.fetchval("SELECT count(*) FROM webinar_entitlements WHERE user_id=$1", uid)
        == 0
    )
    assert await admin.fetchval(
        "SELECT review_required FROM atol_payment_attempts WHERE order_id=$1", oid
    )
    for sensitive in (private, str(tid), "synthetic-audit-link", "synthetic-audit-merchant-token"):
        assert sensitive not in caplog.text


@pytest.mark.parametrize(
    "status",
    [
        PaymentStatus.FAILED,
        PaymentStatus.EXPIRED,
        PaymentStatus.FRAUD,
        PaymentStatus.PARTIALLY_CANCELLED,
        PaymentStatus.PARTIALLY_REFUNDED,
    ],
)
async def test_ambiguous_post_payment_states_preserve_access_and_require_review(purchase, status):
    repo, admin, _, uid, _, oid = purchase
    await setup_registered(purchase)
    assert await repo.observe(oid, await next_poll(purchase), PaymentStatus.PAID)
    entitlement = await admin.fetchrow("SELECT * FROM webinar_entitlements WHERE user_id=$1", uid)
    paid_at = await admin.fetchval("SELECT paid_at FROM purchase_orders WHERE id=$1", oid)
    assert await repo.observe(oid, await next_poll(purchase), status)
    assert await repo.observe(oid, await next_poll(purchase), PaymentStatus.PAID)
    assert await admin.fetchval("SELECT status FROM purchase_orders WHERE id=$1", oid) == "paid"
    assert await admin.fetchval("SELECT paid_at FROM purchase_orders WHERE id=$1", oid) == paid_at
    assert (
        await admin.fetchrow("SELECT * FROM webinar_entitlements WHERE user_id=$1", uid)
        == entitlement
    )
    assert await admin.fetchval(
        "SELECT review_required FROM atol_payment_attempts WHERE order_id=$1", oid
    )
    assert await admin.fetchval("SELECT count(*) FROM payment_events WHERE order_id=$1", oid) == 2
