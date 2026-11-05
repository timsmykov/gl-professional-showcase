from pathlib import Path

import pytest

from gl_bot.settings import Settings, database_url_from_env


def test_settings_accept_file_secrets_without_exposing_values(tmp_path: Path):
    password_file = tmp_path / "db_password"
    password_file.write_text("test:@/password", encoding="utf-8")
    token_file = tmp_path / "token"
    token_file.write_text("123456:synthetic_bot_token", encoding="utf-8")
    webhook_file = tmp_path / "webhook"
    webhook_file.write_text("synthetic_secret", encoding="utf-8")
    env = {
        "DB_HOST": "db",
        "DB_PASSWORD_FILE": str(password_file),
        "TELEGRAM_BOT_TOKEN_FILE": str(token_file),
        "TELEGRAM_WEBHOOK_SECRET_FILE": str(webhook_file),
    }
    settings = Settings.from_env(env)
    assert (
        settings.database_url == "postgresql://gl_bot_app:test%3A%40%2Fpassword@db:5432/gl_bot_dev"
    )
    assert settings.telegram_bot_token == "123456:synthetic_bot_token"
    assert settings.telegram_webhook_secret == "synthetic_secret"
    assert "test:@/password" not in repr(settings)
    assert "synthetic_bot_token" not in repr(settings)
    assert "synthetic_secret" not in repr(settings)


def test_settings_reject_ambiguous_database_credentials(tmp_path: Path):
    password_file = tmp_path / "db_password"
    password_file.write_text("synthetic", encoding="utf-8")
    with pytest.raises(ValueError, match="either DATABASE_URL or DB_PASSWORD_FILE"):
        database_url_from_env(
            {"DATABASE_URL": "postgresql://example", "DB_PASSWORD_FILE": str(password_file)}
        )


def test_api_mode_has_no_database_credentials(tmp_path: Path):
    token = tmp_path / "internal"
    token.write_text("synthetic-internal-token-1234567890")
    env = {
        "GL_BOT_API_URL": "http://api:8000",
        "GL_BOT_API_TOKEN_FILE": str(token),
        "TELEGRAM_BOT_TOKEN": "123456:synthetic_bot_token",
        "TELEGRAM_WEBHOOK_SECRET": "synthetic_secret",
    }
    settings = Settings.from_env(env)
    assert settings.database_url is None
    assert settings.api_url == "http://api:8000"
    with pytest.raises(ValueError, match="must not have database credentials"):
        Settings.from_env({**env, "DB_PASSWORD_FILE": str(token)})
    with pytest.raises(ValueError, match="must not have database credentials"):
        Settings.from_env({**env, "DATABASE_URL_FILE": str(token)})
    with pytest.raises(ValueError, match="internal HTTP URL"):
        Settings.from_env({**env, "GL_BOT_API_URL": "http://outside.example"})


def test_recording_api_is_explicit_opt_in_and_requires_internal_api():
    env = {
        "GL_BOT_API_URL": "http://api:8000",
        "GL_BOT_API_TOKEN": "synthetic-internal-token-1234567890",
        "TELEGRAM_BOT_TOKEN": "123456:synthetic_bot_token",
        "TELEGRAM_WEBHOOK_SECRET": "synthetic_secret",
    }
    assert not Settings.from_env(env).recordings_api_enabled
    assert Settings.from_env({**env, "GL_BOT_RECORDINGS_API_ENABLED": "1"}).recordings_api_enabled
    with pytest.raises(ValueError, match="must be 0 or 1"):
        Settings.from_env({**env, "GL_BOT_RECORDINGS_API_ENABLED": "yes"})
    legacy = {k: v for k, v in env.items() if not k.startswith("GL_BOT_API_")}
    legacy.update(DATABASE_URL="postgresql://synthetic", GL_BOT_RECORDINGS_API_ENABLED="1")
    with pytest.raises(ValueError, match="requires internal API mode"):
        Settings.from_env(legacy)
