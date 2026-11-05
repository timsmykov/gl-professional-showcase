"""A manual owner QA helper must fail closed before touching another environment."""

import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import asyncpg
import pytest

from gl_api.config import ConfigurationError, Settings


@pytest.fixture
def helper():
    path = Path(__file__).resolve().parents[2] / "infra/timeweb/owner-booking-fixture.py"
    spec = importlib.util.spec_from_file_location("owner_fixture", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("database_name", "gl_prod"),
        ("database_user", "gen_user"),
        ("application_role", "gl_prod_app"),
        ("database_host", "201.51.6.253"),
        ("database_port", 5433),
        ("database_tls_mode", "disable"),
        ("require_database_tls", False),
    ],
)
def test_wrong_scope_is_rejected(helper, field, value):
    config = SimpleNamespace(
        database_name="gl_dev",
        database_user="gl_dev_migrator",
        application_role="gl_dev_app",
        database_host=helper.HOST,
        database_port=5432,
        database_tls_mode="verify-full",
        require_database_tls=True,
    )
    helper.validate(config)
    setattr(config, field, value)
    with pytest.raises(ConfigurationError):
        helper.validate(config)


async def test_invalid_action_does_not_open_transaction(helper):
    connection = AsyncMock()
    with pytest.raises(ConfigurationError):
        await helper.fixture(connection, "drop")
    connection.transaction.assert_not_called()


async def test_main_refuses_prod_before_credentials_or_connection(helper, monkeypatch):
    config = SimpleNamespace(database_name="gl_prod")
    connect = AsyncMock()
    monkeypatch.setattr(helper, "load_settings", lambda _: config)
    monkeypatch.setattr(helper.asyncpg, "connect", connect)
    with pytest.raises(ConfigurationError):
        await helper.main("prepare")
    connect.assert_not_called()


@pytest.mark.skipif(
    os.getenv("GL_API_TEST_DATABASE") != "1", reason="isolated PostgreSQL required"
)
async def test_fixture_prepare_repeat_deactivate_preserves_other_content(helper):
    config = Settings(
        database_user="gl_api_migrator",
        database_password_file=Path(os.environ["GL_API_TEST_MIGRATOR_PASSWORD_FILE"]),
    )
    connection = await asyncpg.connect(config.database_url())
    transaction = connection.transaction()
    await transaction.start()
    try:
        unrelated = await connection.fetchval(
            "INSERT INTO services(title,active) VALUES('Unrelated fixture',true) RETURNING id"
        )
        first = await helper.fixture(connection, "prepare")
        assert first["fixture_present"] and first["bookings"] == 0
        assert await helper.fixture(connection, "prepare") == first
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM appointment_slots s JOIN services v ON v.id=s.service_id "
                "WHERE v.title=$1",
                helper.TITLE,
            )
            == 2
        )
        result = await helper.fixture(connection, "deactivate")
        assert result["fixture_present"]
        assert not await connection.fetchval(
            "SELECT active FROM services WHERE title=$1", helper.TITLE
        )
        assert not await connection.fetchval(
            "SELECT bool_or(s.active) FROM appointment_slots s JOIN services v "
            "ON v.id=s.service_id WHERE v.title=$1",
            helper.TITLE,
        )
        assert await connection.fetchval("SELECT active FROM services WHERE id=$1", unrelated)
    finally:
        await transaction.rollback()
        await connection.close()


@pytest.mark.skipif(
    os.getenv("GL_API_TEST_DATABASE") != "1", reason="isolated PostgreSQL required"
)
async def test_fixture_conflicting_title_does_not_modify_existing_content(helper):
    config = Settings(
        database_user="gl_api_migrator",
        database_password_file=Path(os.environ["GL_API_TEST_MIGRATOR_PASSWORD_FILE"]),
    )
    connection = await asyncpg.connect(config.database_url())
    transaction = connection.transaction()
    await transaction.start()
    try:
        unrelated = await connection.fetchval(
            "INSERT INTO services(title,description,active) VALUES($1,'Not QA',true) RETURNING id",
            helper.TITLE,
        )
        with pytest.raises(ConfigurationError):
            await helper.fixture(connection, "deactivate")
        assert await connection.fetchval("SELECT active FROM services WHERE id=$1", unrelated)
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM appointment_slots WHERE service_id=$1", unrelated
            )
            == 0
        )
    finally:
        await transaction.rollback()
        await connection.close()
