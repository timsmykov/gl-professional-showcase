"""Disposable synthetic API/PG contract checks; never run against live databases."""

import asyncio
import os
import socket
from pathlib import Path

import aiohttp
import asyncpg
import pytest
import uvicorn
from aiohttp import web

import gl_api.atol_pay as provider_module
from gl_api.app import create_app
from gl_api.config import Settings
from gl_api.payment_config import PaymentSettings
from gl_api.payment_worker import PaymentReconciler
from gl_bot.api_recordings import ApiRecordingGateway
from gl_bot.api_repository import ApiRepository

pytestmark = pytest.mark.skipif(
    os.getenv("GL_API_TEST_DATABASE") != "1", reason="isolated synthetic PostgreSQL required"
)


@pytest.mark.asyncio
@pytest.mark.parametrize("with_payment", [False, True])
async def test_gateway_actual_api_pending_reuse_status_and_entitlement_contract(
    tmp_path, monkeypatch, with_payment, caplog
):
    token = "synthetic-recording-service-token-1234567890"
    token_file = tmp_path / "internal"
    token_file.write_text(token)
    provider_calls = []
    bank = {"status": 0}
    merchant = tmp_path / "merchant"
    merchant.write_text("synthetic-merchant-token")

    async def provider_reply(request):
        assert request.headers["Authorization"] == "Bearer synthetic-merchant-token"
        provider_calls.append(request.method)
        if request.method == "GET":
            return web.json_response({"status": "success", "data": {"status": bank["status"]}})
        body = await request.json()
        assert body["amount"] == 14900
        assert "telegram_id" not in body
        return web.json_response(
            {
                "status": "success",
                "data": {
                    "orderId": body["orderId"],
                    "amount": body["amount"],
                    "paymentUrls": {"main": "https://pay.example/synthetic-gateway"},
                },
            }
        )

    fake = web.Application()
    fake.router.add_route("*", "/{tail:.*}", provider_reply)
    runner = web.AppRunner(fake, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    monkeypatch.setitem(
        provider_module.ORIGINS,
        "sandbox",
        f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}",
    )
    payment = (
        PaymentSettings(
            environment="sandbox",
            token_file=merchant,
            payment_hosts=["pay.example"],
            return_url="https://t.me/glprofessional_bot",
            sno=1,
            tax=5,
            payment_method=3,
            payment_subject=0,
        )
        if with_payment
        else None
    )
    app = create_app(
        Settings(
            bot_token_file=token_file,
            payment=payment,
            kinescope_token_file=None,
            video_watch_origin=None,
        )
    )
    migrator = await asyncpg.connect(
        Settings(
            database_user="gl_api_migrator",
            database_password_file=Path(os.environ["GL_API_TEST_MIGRATOR_PASSWORD_FILE"]),
        ).database_url()
    )
    uid = 9_000_000_000_000_731
    update_id = 9_000_000_000_000_731
    webinar_id = order_id = user_id = None
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    server = uvicorn.Server(
        uvicorn.Config(app, log_level="error", access_log=False, lifespan="on")
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
        # Fixture setup uses the isolated migrator, never bot SQL or live data.
        webinar_id = await migrator.fetchval(
            "INSERT INTO webinars(title, description, price_kopeks, active) VALUES ($1,$2,$3,true) RETURNING id",
            "Synthetic gateway recording",
            "Synthetic fixture",
            14900,
        )
        await migrator.execute(
            "UPDATE webinars SET public_code=$1 WHERE id=$2", "synthetic_public_code", webinar_id
        )
        async with aiohttp.ClientSession() as session:
            repo = ApiRepository(session, f"http://127.0.0.1:{listener.getsockname()[1]}", token)
            gateway = ApiRecordingGateway(repo)
            user_id = (await repo.get_user(uid))["id"]
            assert await gateway.owned(uid) == []
            first = await gateway.checkout(uid, webinar_id, update_id)
            order_id = first.id
            assert first.expires_at is None
            assert bool(first.payment_url) == with_payment
            assert await gateway.owned(uid) == []
            await migrator.execute(
                "UPDATE webinars SET price_kopeks=$1 WHERE id=$2", 29900, webinar_id
            )
            duplicate = await gateway.checkout(uid, webinar_id, update_id)
            repeat = await gateway.checkout(uid, webinar_id, update_id + 1)
            assert duplicate.id == repeat.id == first.id
            assert duplicate.amount_kopeks == repeat.amount_kopeks == 14900
            assert await gateway.order(uid + 1, order_id) is None
            assert await gateway.order(uid, order_id + 9999) is None
            assert await gateway.playback(uid, webinar_id) is None
            if with_payment:
                assert provider_calls.count("POST") == 1
                assert (await gateway.order(uid, order_id)).status == "pending"
                assert await gateway.owned(uid) == []
                for bank_status, status in [(1, "paid"), (1, "paid"), (5, "refunded")]:
                    bank["status"] = bank_status
                    await migrator.execute(
                        "UPDATE atol_payment_attempts SET next_poll_at=clock_timestamp() WHERE order_id=$1",
                        order_id,
                    )
                    await PaymentReconciler(app.state.checkout).run_once()
                    assert (await gateway.order(uid, order_id)).status == status
                    owned = await gateway.owned(uid)
                    assert bool(owned) == (status == "paid")
                    if owned:
                        assert owned[0].webinar_id == webinar_id
                    assert await gateway.owned(uid + 1) == []
                assert provider_calls.count("POST") == 1
                assert (
                    await migrator.fetchval(
                        "SELECT count(*) FROM payment_events WHERE order_id=$1", order_id
                    )
                    == 2
                )
            for status in [] if with_payment else ["paid", "refunded"]:
                # Persisted-state fixtures, not an implementation of payment confirmation.
                await migrator.execute(
                    "UPDATE purchase_orders SET status=$1 WHERE id=$2", status, order_id
                )
                result = await gateway.order(uid, order_id)
                assert result.status == status and result.payment_url is None
                assert await gateway.owned(uid) == []
            await migrator.execute(
                "INSERT INTO webinar_entitlements(user_id,webinar_id,source) VALUES ($1,$2,'paid')",
                user_id,
                webinar_id,
            )
            assert (await gateway.owned(uid))[0].webinar_id == webinar_id
            assert await gateway.owned(uid + 1) == []
            assert (await gateway.resolve("synthetic_public_code")).id == webinar_id
            assert await gateway.resolve("unknown_code") is None
            await migrator.execute("UPDATE webinars SET active=false WHERE id=$1", webinar_id)
            assert await gateway.resolve("synthetic_public_code") is None
            assert (await gateway.order(uid, order_id)).status == "refunded"
            assert (await gateway.owned(uid))[0].webinar_id == webinar_id
    finally:
        server.should_exit = True
        await task
        listener.close()
        if order_id is not None:
            await migrator.execute("DELETE FROM webinar_entitlements WHERE user_id=$1", user_id)
            await migrator.execute("DELETE FROM payment_events WHERE order_id=$1", order_id)
            await migrator.execute("DELETE FROM atol_payment_attempts WHERE order_id=$1", order_id)
            await migrator.execute("DELETE FROM order_intent_requests WHERE order_id=$1", order_id)
            await migrator.execute("DELETE FROM purchase_orders WHERE id=$1", order_id)
        if user_id is not None:
            await migrator.execute("DELETE FROM webinar_entitlements WHERE user_id=$1", user_id)
            await migrator.execute("DELETE FROM users WHERE id=$1", user_id)
        if webinar_id is not None:
            await migrator.execute("DELETE FROM webinars WHERE id=$1", webinar_id)
        await migrator.close()
        await runner.cleanup()
        assert "synthetic-merchant-token" not in caplog.text
        assert token not in caplog.text
        assert "https://pay.example/synthetic-gateway" not in caplog.text
