"""The internal normalized ledger is not a callback or payment authority."""

from dataclasses import replace
from uuid import uuid4

import asyncpg
import httpx
import pytest

from gl_api.app import create_app
from gl_api.config import Settings
from gl_api.fiscal import FiscalState, ReceiptObservation, ReceiptRepository


def example():
    return ReceiptObservation(uuid4(), uuid4(), "sandbox", "sell", 1000, FiscalState.SUCCESS)


def test_driver_uuid_values_are_supported():
    observation = example()
    assert (
        replace(
            observation,
            merchant_order_id=asyncpg.pgproto.pgproto.UUID(str(observation.merchant_order_id)),
            receipt_id=asyncpg.pgproto.pgproto.UUID(str(observation.receipt_id)),
        )
        == observation
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"amount_kopeks": True},
        {"amount_kopeks": 0},
        {"amount_kopeks": 1000.0},
        {"amount_kopeks": 2000000001},
        {"state": "success"},
        {"state": True},
        {"environment": "test"},
        {"receipt_type": "paid"},
        {"receipt_id": "synthetic-private-id"},
        {"merchant_order_id": None},
    ],
)
def test_unsafe_normalized_observation_rejected_without_echo(changes):
    with pytest.raises(ValueError, match="^invalid normalized receipt observation$"):
        replace(example(), **changes)


def test_fiscal_reference_ids_and_amount_are_not_in_repr():
    observation = example()
    rendered = repr(observation)
    assert str(observation.receipt_id) not in rendered
    assert str(observation.merchant_order_id) not in rendered
    assert "1000" not in rendered


async def test_raw_callback_is_not_an_internal_observation():
    with pytest.raises(TypeError, match="normalized receipt observation required"):
        await ReceiptRepository(None).record({"status": "success", "type": "fiscal"})


async def test_no_http_or_bot_route_can_submit_fiscal_observations(tmp_path):
    token = tmp_path / "internal-token"
    value = "synthetic-internal-token-1234567890"
    token.write_text(value)
    app = create_app(Settings(bot_token_file=token))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        for path in (
            "/internal/receipts",
            "/internal/fiscal-events",
            "/internal/bot/record_receipt",
            "/api/atol/callback",
            "/callbacks/atol",
        ):
            response = await client.post(
                path,
                json={"args": []} if "/bot/" in path else {"status": "success"},
                headers={"Authorization": f"Bearer {value}"},
            )
            assert response.status_code == 404
