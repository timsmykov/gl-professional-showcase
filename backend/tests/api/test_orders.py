"""The pending-order route is authenticated and does not initiate checkout."""

from pathlib import Path

import httpx
import pytest

from gl_api.app import create_app
from gl_api.config import Settings
from gl_api.orders import OrderRepository

TOKEN = "synthetic-internal-token-1234567890"
INTENT = {"telegram_id": -9001, "webinar_id": 1, "source_update_id": 5001}


class OfflinePool:
    def acquire(self):
        raise OSError("synthetic DB connection detail")


@pytest.mark.asyncio
async def test_order_route_is_private_and_rejects_invalid_or_unauthorized_input(
    tmp_path: Path, caplog
):
    token_file = tmp_path / "internal-token"
    token_file.write_text(TOKEN)
    app = create_app(Settings(bot_token_file=token_file))
    app.state.pool = OfflinePool()
    headers = {"Authorization": f"Bearer {TOKEN}"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        path = "/internal/order-intents"
        assert (await client.post(path, json=INTENT)).status_code == 401
        assert (await client.post(path, content=b"invalid json")).status_code == 401
        assert (
            await client.post(
                path,
                json=INTENT,
                headers=[("Authorization", f"Bearer {TOKEN}"), ("Authorization", "Bearer wrong")],
            )
        ).status_code == 401
        assert (await client.post(path, json=INTENT, headers=headers)).status_code == 503
        private_email = "synthetic-private@example.invalid"
        rejected_email = await client.post(
            path, json={**INTENT, "email": private_email}, headers=headers
        )
        assert rejected_email.status_code == 422
        assert rejected_email.json() == {"detail": "invalid order intent"}
        assert private_email not in rejected_email.text
        for malformed in (
            {**INTENT, "amount_kopeks": 1},
            {**INTENT, "status": "paid"},
            {**INTENT, "telegram_id": True},
            {**INTENT, "telegram_id": 0},
            {**INTENT, "webinar_id": 0},
            {**INTENT, "source_update_id": 0},
            {**INTENT, "source_update_id": 2**63},
            {"telegram_id": -9001, "webinar_id": 1},
        ):
            assert (await client.post(path, json=malformed, headers=headers)).status_code == 422
        assert (
            await client.post(
                "/internal/bot/create_pending_order", json={"args": [1, 2, 3]}, headers=headers
            )
        ).status_code == 404
        assert (await client.post("/api/orders", json=INTENT)).status_code == 404
        assert (await client.post("/api/payment-events/test", json={})).status_code == 404
        assert path not in (await client.get("/openapi.json")).text
    assert TOKEN not in caplog.text
    assert private_email not in caplog.text
    assert "synthetic DB connection detail" not in caplog.text

    public_app = create_app(Settings())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=public_app), base_url="http://test"
    ) as client:
        assert (await client.post(path, json=INTENT, headers=headers)).status_code == 404


@pytest.mark.asyncio
async def test_invalid_database_order_is_safe_for_response_and_logs(
    tmp_path: Path, monkeypatch, caplog
):
    token_file = tmp_path / "internal-token"
    token_file.write_text(TOKEN)
    private_email = "synthetic-private@example.invalid"

    async def broken_record(_repository, *_args):
        return {
            "id": 1,
            "webinar_id": 3,
            "amount_kopeks": 0,
            "currency": "RUB",
            "status": "pending",
            "receipt_email": private_email,
        }

    monkeypatch.setattr(OrderRepository, "create_pending_order", broken_record)
    app = create_app(Settings(bot_token_file=token_file))
    app.state.pool = object()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/internal/order-intents", json=INTENT, headers={"Authorization": f"Bearer {TOKEN}"}
        )
    assert response.status_code == 503
    assert response.json() == {"detail": "order unavailable"}
    assert private_email not in response.text + caplog.text
    assert TOKEN not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ids",
    [
        (True, 1, 1),
        (0, 1, 1),
        (1, 0, 1),
        (1, 1, 0),
        (1, 1, -1),
        (2**63, 1, 1),
        (1, 2**63, 1),
        (1, 1, 2**63),
    ],
)
async def test_invalid_order_identifiers_fail_before_database_access(ids):
    repo = OrderRepository(None)
    with pytest.raises(ValueError, match="invalid order identifiers"):
        await repo.create_pending_order(*ids)


async def test_order_status_boundary_reads_only_owned_persisted_state(
    tmp_path, monkeypatch, caplog
):
    from unittest.mock import AsyncMock

    from gl_api.orders import OrderUnavailable

    token_file = tmp_path / "internal-token"
    token_file.write_text(TOKEN)
    app = create_app(Settings(bot_token_file=token_file))
    app.state.pool = OfflinePool()
    owned = {"id": 7, "webinar_id": 3, "amount_kopeks": 1000, "currency": "RUB", "status": "paid"}
    read = AsyncMock(return_value=owned)
    monkeypatch.setattr(OrderRepository, "status", read)
    auth = {"Authorization": f"Bearer {TOKEN}"}
    body = {"telegram_id": -9001, "order_id": 7}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        path = "/internal/order-status"
        assert (await client.post(path, json=body)).status_code == 401
        assert (
            await client.post(
                path, json=body, headers=auth | {"Origin": "https://glprofessional.ru"}
            )
        ).status_code == 403
        for bad in (
            body | {"status": "paid"},
            body | {"amount_kopeks": 1},
            body | {"order_id": True},
            body | {"telegram_id": "1"},
        ):
            assert (await client.post(path, json=bad, headers=auth)).status_code == 422
        read.assert_not_awaited()
        for state in ("pending", "paid", "cancelled", "refunded"):
            read.return_value = owned | {"status": state}
            result = await client.post(path, json=body, headers=auth)
            assert result.status_code == 200
            assert result.json() == owned | {"status": state}
            assert result.headers["cache-control"] == "no-store"
        read.assert_awaited_with(-9001, 7)
        read.side_effect = OrderUnavailable()
        assert (await client.post(path, json=body, headers=auth)).status_code == 404
        read.side_effect = OSError("synthetic-private-error")
        result = await client.post(path, json=body, headers=auth)
        assert result.status_code == 503
        assert "synthetic-private-error" not in result.text
        assert path not in (await client.get("/openapi.json")).text
    assert TOKEN not in caplog.text and "synthetic-private-error" not in caplog.text
    public = create_app(Settings())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=public), base_url="http://test"
    ) as client:
        assert (await client.post(path, json=body)).status_code == 404
