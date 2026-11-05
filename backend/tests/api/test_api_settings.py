"""Configuration boundaries and observable startup failures, using synthetic values."""

import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

import httpx
import pytest
from pydantic import ValidationError

from gl_api.app import create_app
from gl_api.config import ConfigurationError, MigrationSettings, Settings, load_settings


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    # Isolate precedence tests even inside the configured PostgreSQL test image.
    for name in os.environ:
        if name.startswith("GL_API_"):
            monkeypatch.delenv(name)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("database_host", ""),
        ("database_host", " db"),
        ("database_host", "postgresql://user:private-value@db"),
        ("database_host", "db:5432"),
        ("database_host", "db?password=private-value"),
        ("database_host", "[::1]"),
        ("database_host", "fe80::1%eth0"),
        ("database_host", "999.1.1.1"),
        ("database_host", "-db"),
        ("database_host", "a..b"),
        ("database_host", "db.."),
        ("database_host", "a" * 64),
        ("database_name", ""),
        ("database_name", "with space"),
        ("database_name", "я" * 32),
        ("database_user", ""),
        ("database_user", "private-value\n"),
        ("database_user", "a" * 64),
        ("database_port", 0),
        ("database_port", 65536),
        ("database_port", "private-value"),
        ("pool_max_size", 0),
        ("pool_max_size", 21),
        ("database_password_file", ""),
        ("bot_token_file", ""),
    ],
)
def test_invalid_settings_hide_inputs(field, value):
    with pytest.raises(ValidationError) as error:
        Settings(**{field: value})
    assert "private-value" not in str(error.value)
    assert "input_value" not in str(error.value)


@pytest.mark.parametrize("host", ["db", "test_db", "postgres.example.ru.", "127.0.0.1", "::1"])
def test_dsn_encodes_credentials_and_identifiers_without_changing_password(tmp_path, host):
    password = " synthetic:/@?#%+ пароль "
    secret = tmp_path / "synthetic-secret"
    secret.write_text(password + "\r\n", encoding="utf-8")
    settings = Settings(
        database_host=host,
        database_name="db/?#",
        database_user="role:@%",
        database_password_file=secret,
    )
    url = urlsplit(settings.database_url())
    assert url.hostname == host
    assert unquote(url.username) == "role:@%"
    assert unquote(url.password) == password
    assert unquote(url.path) == "/db/?#"
    assert not url.query and not url.fragment
    assert password not in repr(settings)
    assert str(secret) not in repr(settings)


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        (b"", "is empty"),
        (b"\r\n", "is empty"),
        (b"synthetic-private\xff", "UTF-8"),
        (b"a" * 4097, "4096 bytes"),
        (b"synthetic-private\x00", "one line"),
        (b"synthetic-private\nsecond", "one line"),
    ],
)
@pytest.mark.parametrize("kind", ["password", "token"])
def test_bad_secret_file_errors_never_echo_content_or_path(tmp_path, content, reason, kind):
    secret = tmp_path / "synthetic-private-path"
    secret.write_bytes(content)
    settings = Settings(database_password_file=secret, bot_token_file=secret)
    with pytest.raises(ConfigurationError, match=reason) as error:
        (settings.database_url if kind == "password" else settings.bot_token)()
    assert "synthetic-private" not in str(error.value)
    assert str(secret) not in repr(error.value)


@pytest.mark.parametrize("kind", ["password", "token"])
def test_missing_directory_and_unreadable_secret_fail_safely(tmp_path, monkeypatch, kind):
    for path in (tmp_path / "missing-private-file", tmp_path):
        settings = Settings(database_password_file=path, bot_token_file=path)
        with pytest.raises(ConfigurationError) as error:
            (settings.database_url if kind == "password" else settings.bot_token)()
        assert str(path) not in str(error.value)

    def denied(*args, **kwargs):
        raise PermissionError("synthetic-private-permission-detail")

    monkeypatch.setattr(os, "open", denied)
    with pytest.raises(ConfigurationError, match="cannot be read") as error:
        Settings(database_password_file=tmp_path / "denied").database_url()
    assert "synthetic-private" not in str(error.value)


@pytest.mark.parametrize(
    "token",
    [
        "x" * 31,
        "x" * 257,
        "я" * 32,
        "x" * 32 + " ",
        " " + "x" * 32,
        "x" * 32 + ":",
        "x" * 32 + "=a",
    ],
)
def test_invalid_internal_token_is_rejected_before_routes_are_created(tmp_path, token):
    secret = tmp_path / "token"
    secret.write_text(token)
    with pytest.raises(ConfigurationError):
        create_app(Settings(bot_token_file=secret))


def test_settings_precedence_and_no_automatic_dotenv(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("GL_API_DATABASE_HOST=dotenv-host\n")
    assert load_settings().database_host == "db"
    monkeypatch.setenv("GL_API_DATABASE_HOST", "env-host")
    assert load_settings().database_host == "env-host"
    assert Settings(database_host="explicit-host").database_host == "explicit-host"
    monkeypatch.setenv("GL_API_POOL_MAX_SIZE", "3")
    assert load_settings().pool_max_size == 3
    monkeypatch.setenv("GL_API_POOL_MAX_SIZE", "synthetic-private-value")
    with pytest.raises(ConfigurationError, match="invalid settings: pool_max_size") as error:
        load_settings()
    assert "synthetic-private" not in str(error.value)


@pytest.mark.parametrize(
    "name", ["GL_API_DATABASE_PASSWORD", "GL_API_DATABASE_URL", "GL_API_BOT_TOKEN"]
)
def test_direct_credentials_are_not_silently_ignored(monkeypatch, name):
    monkeypatch.setenv(name, "synthetic-private-value")
    with pytest.raises(ConfigurationError, match="use a secret file") as error:
        load_settings()
    assert "synthetic-private" not in str(error.value)


def test_migrator_settings_do_not_inherit_runtime_role_or_api_only_settings(monkeypatch):
    monkeypatch.setenv("GL_API_DATABASE_USER", "runtime_role")
    monkeypatch.setenv("GL_API_POOL_MAX_SIZE", "invalid-runtime-pool")
    monkeypatch.setenv("GL_API_BOT_TOKEN_FILE", "/missing/runtime-token")
    settings = load_settings(MigrationSettings)
    assert settings.database_user == "gl_api_migrator"
    assert settings.database_password_file == Path("/run/secrets/gl_api_migrator_password")
    monkeypatch.setenv("GL_API_DATABASE_PASSWORD_FILE", "/legacy/migrator-password")
    assert load_settings(MigrationSettings).database_password_file == Path(
        "/legacy/migrator-password"
    )
    monkeypatch.setenv("GL_API_MIGRATION_DATABASE_USER", "migration_role")
    monkeypatch.setenv("GL_API_MIGRATION_DATABASE_PASSWORD_FILE", "/dedicated/migrator-password")
    assert load_settings(MigrationSettings).database_user == "migration_role"
    assert load_settings(MigrationSettings).database_password_file == Path(
        "/dedicated/migrator-password"
    )
    assert (
        MigrationSettings(GL_API_MIGRATION_DATABASE_USER="explicit_migrator").database_user
        == "explicit_migrator"
    )
    with pytest.raises(ValidationError):
        MigrationSettings(database_user="accidental-runtime-override")


def test_disabled_internal_auth_is_explicit(monkeypatch):
    assert load_settings().bot_token() is None
    monkeypatch.setenv("GL_API_BOT_TOKEN_FILE", "")
    with pytest.raises(ConfigurationError):
        load_settings()


@pytest.mark.parametrize("entrypoint", [["-m", "gl_api"], ["-m", "alembic", "upgrade", "head"]])
def test_real_process_rejects_invalid_env_without_exposing_input(entrypoint):
    result = subprocess.run(
        [sys.executable, *entrypoint],
        env={**os.environ, "GL_API_DATABASE_HOST": "synthetic-private-value@db"},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "invalid settings: database_host" in output
    assert "synthetic-private" not in output
    if entrypoint == ["-m", "gl_api"]:
        assert result.returncode == 2
        assert "Traceback" not in output


def test_fifo_secret_fails_without_blocking_startup(tmp_path):
    fifo = tmp_path / "synthetic-private-fifo"
    os.mkfifo(fifo)
    result = subprocess.run(
        [sys.executable, "-m", "gl_api"],
        env={**os.environ, "GL_API_DATABASE_PASSWORD_FILE": str(fifo)},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 2
    assert "must be a regular file" in result.stderr
    assert str(fifo) not in result.stderr


@pytest.mark.asyncio
async def test_token_rotation_requires_new_application_and_keeps_old_token_out_of_logs(
    tmp_path, caplog
):
    secret = tmp_path / "token"
    old_token, new_token = "old-synthetic-token-" + "a" * 32, "new-synthetic-token-" + "b" * 32
    secret.write_text(old_token + "\n")
    old_app = create_app(Settings(bot_token_file=secret))
    replacement = tmp_path / "new-token"
    replacement.write_text(new_token)
    replacement.replace(secret)
    new_app = create_app(Settings(bot_token_file=secret))

    class OfflinePool:
        async def fetch(self, *args):
            raise OSError("synthetic-private-driver-detail")

    for app, accepted, rejected in (
        (old_app, old_token, new_token),
        (new_app, new_token, old_token),
    ):
        app.state.pool = OfflinePool()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            malformed = await client.post(
                "/internal/bot/list_webinars",
                json={"args": []},
                headers=[(b"Authorization", b"Bearer \xff" + b"a" * 32)],
            )
            assert malformed.status_code == 401
            for token, status in ((accepted, 503), (rejected, 401)):
                response = await client.post(
                    "/internal/bot/list_webinars",
                    json={"args": []},
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert response.status_code == status
                assert token not in response.text
    assert old_token not in caplog.text and new_token not in caplog.text
    assert "synthetic-private" not in caplog.text
