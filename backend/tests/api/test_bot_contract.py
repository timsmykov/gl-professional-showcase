from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg
import httpx
import pytest

from gl_api.app import create_app
from gl_api.bot_contract import ARG_TYPES, valid_args
from gl_api.bot_repository import Repository
from gl_api.config import Settings


class FakePool:
    async def fetchval(self, _sql):
        return 1


class MissingTablePool:
    def __init__(self, table: str):
        self.table = table

    @asynccontextmanager
    async def acquire(self, **_kwargs):
        yield self

    async def fetchval(self, sql):
        if f"FROM {self.table} " in sql:
            raise asyncpg.UndefinedTableError("synthetic missing bot table")
        return 1


def test_allowlist_rejects_unknown_and_malformed_operations():
    assert valid_args("get_user", [10])
    assert not valid_args("get_user", [True])
    assert not valid_args("get_user", ["10"])
    assert not valid_args("select_section", [1, "admin"])
    assert not valid_args("decide_booking", [1, "paid"])
    assert not valid_args("__class__", [])
    assert not valid_args("mark_paid", [1])
    assert not valid_args("grant_entitlement", [1, 1])
    assert not valid_args("complete_update", [1, "not-a-lease"])
    assert not valid_args("release_update", [1, "2026-09-28T00:00:00"])
    for operation, kinds in ARG_TYPES.items():
        assert callable(getattr(Repository, operation, None)), operation
        args = [
            (
                "confirmed"
                if operation == "decide_booking"
                else "2026-09-28T00:00:00+00:00"
                if operation in {"complete_update", "release_update"}
                else "pro"
            )
            if kind is str
            else 1
            for kind in kinds
        ]
        assert valid_args(operation, args), operation
        assert not valid_args(operation, args + [1]), operation
        if args:
            assert not valid_args(operation, args[:-1]), operation
        for index, kind in enumerate(kinds):
            if kind is int:
                too_large = args.copy()
                too_large[index] = 2**63
                assert not valid_args(operation, too_large), (operation, index)
                too_small = args.copy()
                too_small[index] = -(2**63) - 1
                assert not valid_args(operation, too_small), (operation, index)


@pytest.mark.asyncio
async def test_internal_api_requires_token_and_hides_route_without_configuration(
    tmp_path: Path, caplog
):
    password_file = tmp_path / "db_password"
    password_file.write_text("synthetic-password")
    token_file = tmp_path / "token"
    token_file.write_text("synthetic-internal-token-1234567890")
    app = create_app(Settings(database_password_file=password_file, bot_token_file=token_file))
    app.state.pool = FakePool()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        path = "/internal/bot/health"
        assert (await client.post(path, json={"args": []})).status_code == 401
        assert (await client.post(path, content=b"not-json")).status_code == 401
        assert (
            await client.post(path, json={"args": []}, headers={"Authorization": "Bearer wrong"})
        ).status_code == 401
        assert (
            await client.post(
                path,
                json={"args": []},
                headers=[
                    ("Authorization", "Bearer synthetic-internal-token-1234567890"),
                    ("Authorization", "Bearer wrong"),
                ],
            )
        ).status_code == 401
        headers = {"Authorization": "Bearer synthetic-internal-token-1234567890"}
        assert (await client.post(path, json={"args": []}, headers=headers)).json() == {
            "result": None
        }
        assert (
            await client.post("/internal/bot/nope", json={"args": []}, headers=headers)
        ).status_code == 404
        assert (
            await client.post("/internal/bot/get_user", json={"args": [True]}, headers=headers)
        ).status_code == 422
        assert (
            await client.post(
                "/internal/bot/get_user",
                json={"args": [1], "unexpected": "ignored"},
                headers=headers,
            )
        ).status_code == 422
        assert (
            await client.post("/internal/bot/get_user", json={"args": [2**63]}, headers=headers)
        ).status_code == 422
        assert path not in (await client.get("/openapi.json")).text
    assert "synthetic-internal-token-1234567890" not in caplog.text

    public_app = create_app(Settings(database_password_file=password_file))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=public_app), base_url="http://test"
    ) as client:
        assert (await client.post("/internal/bot/health", json={"args": []})).status_code == 404


def test_short_internal_token_fails_closed(tmp_path: Path):
    token_file = tmp_path / "token"
    token_file.write_text("short")
    with pytest.raises(ValueError, match="internal bot token is too short"):
        create_app(Settings(bot_token_file=token_file))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "missing_table",
    [
        "users",
        "user_sections",
        "services",
        "appointment_slots",
        "booking_requests",
        "products",
        "webinars",
        "webinar_entitlements",
        "processed_updates",
        "purchase_orders",
        "order_intent_requests",
    ],
)
async def test_readiness_checks_bot_tables_when_internal_api_enabled(
    tmp_path: Path, caplog, missing_table: str
):
    token_file = tmp_path / "token"
    token_file.write_text("synthetic-internal-token-1234567890")
    app = create_app(Settings(bot_token_file=token_file))
    app.state.pool = MissingTablePool(missing_table)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get("/healthz")).status_code == 200
        response = await client.get("/readyz")
        assert response.status_code == 503
        assert response.json() == {"detail": "database unavailable"}
    assert "synthetic missing bot table" not in caplog.text


@pytest.mark.parametrize("code", ["", "a" * 65, "../x", "x y", "x\n", "кириллица", "x' OR true--"])
def test_recording_code_rejects_malformed_values(code):
    assert not valid_args("resolve_recording", [code])


@pytest.mark.parametrize("operation", ["list_owned_recordings", "get_recording_context"])
@pytest.mark.parametrize("identifier", [0, -1, True, "1", 2**63])
def test_recording_reads_reject_invalid_identifiers(operation, identifier):
    assert not valid_args(operation, [identifier])
