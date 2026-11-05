from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, urlsplit
from zoneinfo import ZoneInfo


def _secret_value(values: Mapping[str, str], key: str) -> str | None:
    direct = values.get(key) or None
    file_path = values.get(f"{key}_FILE") or None
    if direct and file_path:
        raise ValueError(f"Set only one of {key} and {key}_FILE")
    if file_path:
        value = Path(file_path).read_text(encoding="utf-8").strip()
        if not value:
            raise ValueError(f"{key}_FILE is empty")
        return value
    return direct


def database_url_from_env(
    values: Mapping[str, str], *, password_file_key: str = "DB_PASSWORD_FILE"
) -> str:
    direct = _secret_value(values, "DATABASE_URL")
    password_file = values.get(password_file_key) or None
    if direct and password_file:
        raise ValueError("Set either DATABASE_URL or DB_PASSWORD_FILE")
    if direct:
        return direct
    if not password_file:
        raise ValueError("DATABASE_URL or DB_PASSWORD_FILE is required")

    user = values.get("DB_USER") or "gl_bot_app"
    name = values.get("DB_NAME") or "gl_bot_dev"
    host = values.get("DB_HOST") or "db"
    port = values.get("DB_PORT") or "5432"
    if not re.fullmatch(r"[A-Za-z0-9_]+", user) or not re.fullmatch(r"[A-Za-z0-9_]+", name):
        raise ValueError("DB_USER or DB_NAME has invalid format")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", host):
        raise ValueError("DB_HOST has invalid format")
    if not port.isdigit() or not 1 <= int(port) <= 65535:
        raise ValueError("DB_PORT is invalid")
    password = Path(password_file).read_text(encoding="utf-8").strip()
    if not password:
        raise ValueError(f"{password_file_key} is empty")
    return f"postgresql://{quote(user)}:{quote(password, safe='')}@{host}:{port}/{quote(name)}"


@dataclass(frozen=True)
class Settings:
    database_url: str | None = field(repr=False)
    api_url: str | None
    api_token: str | None = field(repr=False)
    telegram_bot_token: str = field(repr=False)
    telegram_webhook_secret: str = field(repr=False)
    german_telegram_id: int | None
    german_contact_url: str | None
    display_time_zone: str
    port: int
    telegram_proxy_url: str | None = None
    recordings_api_enabled: bool = False

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        values = os.environ if env is None else env
        proxy_url = values.get("TELEGRAM_PROXY_URL") or None
        if proxy_url:
            proxy = urlsplit(proxy_url)
            try:
                proxy_port = proxy.port
            except ValueError:
                raise ValueError("TELEGRAM_PROXY_URL must be a loopback HTTP proxy") from None
            if (
                proxy.scheme != "http"
                or proxy.hostname not in {"127.0.0.1", "localhost"}
                or not proxy_port
                or proxy.username
                or proxy.password
                or proxy.path
                or proxy.query
                or proxy.fragment
            ):
                raise ValueError("TELEGRAM_PROXY_URL must be a loopback HTTP proxy")
        api_url = values.get("GL_BOT_API_URL") or None
        api_token = _secret_value(values, "GL_BOT_API_TOKEN") if api_url else None
        if api_url:
            parsed = urlsplit(api_url)
            if (
                parsed.scheme != "http"
                or parsed.hostname not in {"api", "localhost", "127.0.0.1"}
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("GL_BOT_API_URL must be an internal HTTP URL")
            if not api_token or len(api_token) < 32:
                raise ValueError("GL_BOT_API_TOKEN_FILE must contain an internal token")
            if (
                values.get("DATABASE_URL")
                or values.get("DATABASE_URL_FILE")
                or values.get("DB_PASSWORD_FILE")
            ):
                raise ValueError("Bot API mode must not have database credentials")
            database_url = None
        else:
            database_url = database_url_from_env(values)
        recordings_flag = values.get("GL_BOT_RECORDINGS_API_ENABLED", "0")
        if recordings_flag not in {"0", "1"}:
            raise ValueError("GL_BOT_RECORDINGS_API_ENABLED must be 0 or 1")
        if recordings_flag == "1" and not api_url:
            raise ValueError("Recording API requires internal API mode")
        bot_token = _secret_value(values, "TELEGRAM_BOT_TOKEN")
        secret = _secret_value(values, "TELEGRAM_WEBHOOK_SECRET")
        if not bot_token:
            raise ValueError("TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN_FILE is required")
        if not secret:
            raise ValueError("TELEGRAM_WEBHOOK_SECRET or TELEGRAM_WEBHOOK_SECRET_FILE is required")

        if not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", secret):
            raise ValueError("TELEGRAM_WEBHOOK_SECRET has invalid format")

        german_id = values.get("GERMAN_TELEGRAM_ID") or None
        if german_id and not re.fullmatch(r"\d{1,18}", german_id):
            raise ValueError("GERMAN_TELEGRAM_ID has invalid format")

        contact_url = values.get("GERMAN_CONTACT_URL") or None
        if contact_url and not re.fullmatch(r"https://t\.me/[A-Za-z0-9_]{5,32}/?", contact_url):
            raise ValueError("GERMAN_CONTACT_URL must be a confirmed t.me username URL")

        time_zone = values.get("DISPLAY_TIME_ZONE") or "Europe/Moscow"
        ZoneInfo(time_zone)
        port = int(values.get("PORT") or "3000")
        if not 1 <= port <= 65535:
            raise ValueError("PORT is invalid")

        return cls(
            database_url=database_url,
            api_url=api_url,
            api_token=api_token,
            telegram_bot_token=bot_token,
            telegram_webhook_secret=secret,
            german_telegram_id=int(german_id) if german_id else None,
            german_contact_url=contact_url,
            display_time_zone=time_zone,
            port=port,
            telegram_proxy_url=proxy_url,
            recordings_api_enabled=recordings_flag == "1",
        )
