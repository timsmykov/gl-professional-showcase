"""Process configuration. Credentials are read only from bounded secret files."""

import ipaddress
import os
import re
import ssl
import stat
from pathlib import Path
from typing import Literal
from urllib.parse import quote, urlsplit

from pydantic import AliasChoices, Field, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from gl_api.payment_config import PaymentSettings


class ConfigurationError(ValueError):
    """Safe to print: messages contain field names and reasons, never inputs."""


def read_secret(path: Path, label: str) -> str:
    try:
        # NONBLOCK prevents a mistakenly configured FIFO from hanging startup.
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise ConfigurationError(f"{label} must be a regular file")
            content = source.read(4097)
    except OSError:
        raise ConfigurationError(f"{label} cannot be read") from None
    if len(content) > 4096:
        raise ConfigurationError(f"{label} exceeds 4096 bytes")
    try:
        value = content.decode("utf-8").rstrip("\r\n")
    except UnicodeError:
        raise ConfigurationError(f"{label} must contain UTF-8 text") from None
    if not value:
        raise ConfigurationError(f"{label} is empty")
    if any(character in value for character in "\x00\r\n"):
        raise ConfigurationError(f"{label} must contain one line without NUL")
    return value


class DatabaseSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="GL_API_", extra="ignore", hide_input_in_errors=True
    )

    database_tls_mode: Literal["disable", "verify-full"] = "disable"
    database_ca_file: Path | None = Field(default=None, repr=False)
    require_database_tls: bool = False

    database_host: str = "db"
    database_port: int = Field(default=5432, ge=1, le=65535)
    database_name: str = "gl_api"
    database_user: str = "gl_api_app"
    database_password_file: Path = Field(
        default=Path("/run/secrets/gl_api_app_password"), repr=False
    )

    @field_validator("database_host")
    @classmethod
    def valid_host(cls, value: str) -> str:
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            if len(value) > 253 or ".." in value or not re.fullmatch(r"[A-Za-z0-9_.-]+", value):
                raise ValueError("expected an IP address or hostname") from None
            if "." in value and re.fullmatch(r"[0-9.]+", value):
                raise ValueError("invalid IPv4 address")
            labels = value.rstrip(".").split(".")
            if any(
                not label or len(label) > 63 or label.startswith("-") or label.endswith("-")
                for label in labels
            ):
                raise ValueError("invalid hostname")
        else:
            if getattr(address, "scope_id", None):
                raise ValueError("scoped IPv6 addresses are unsupported")
        return value

    @field_validator("database_name", "database_user", "application_role", check_fields=False)
    @classmethod
    def valid_identifier(cls, value: str) -> str:
        if (
            not value
            or len(value.encode("utf-8")) > 63
            or any(not character.isprintable() or character.isspace() for character in value)
        ):
            raise ValueError("expected a nonempty PostgreSQL identifier up to 63 bytes")
        return value

    @field_validator("database_password_file", "bot_token_file", check_fields=False, mode="before")
    @classmethod
    def valid_secret_path(cls, value):
        if value is not None and (not str(value).strip() or "\x00" in str(value)):
            raise ValueError("expected a nonempty secret file path")
        return value

    @model_validator(mode="after")
    def validate_tls(self):
        if self.require_database_tls and self.database_tls_mode != "verify-full":
            raise ValueError("verified database TLS is required")
        if self.database_tls_mode == "verify-full" and self.database_ca_file is None:
            raise ValueError("verified database TLS requires a CA file")
        return self

    def database_ssl(self) -> ssl.SSLContext | bool:
        if self.database_tls_mode == "disable":
            return False
        try:
            context = ssl.create_default_context(cafile=str(self.database_ca_file))
        except (OSError, ssl.SSLError):
            raise ConfigurationError("database CA file cannot be loaded") from None
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
        return context

    def migration_connect_args(self) -> dict[str, str]:
        self.database_ssl()  # Validate the CA before the driver can print an error.
        if self.database_tls_mode == "disable":
            return {"sslmode": "disable"}
        return {"sslmode": "verify-full", "sslrootcert": str(self.database_ca_file)}

    def database_url(self) -> str:
        password = read_secret(self.database_password_file, "database password file")
        host = f"[{self.database_host}]" if ":" in self.database_host else self.database_host
        return (
            f"postgresql://{quote(self.database_user, safe='')}:"
            f"{quote(password, safe='')}@{host}:"
            f"{self.database_port}/{quote(self.database_name, safe='')}"
        )


class Settings(DatabaseSettings):
    payment: PaymentSettings | None = Field(default=None, repr=False)
    pool_max_size: int = Field(default=5, ge=1, le=20)
    bot_token_file: Path | None = Field(default=None, repr=False)
    kinescope_token_file: Path | None = Field(default=None, repr=False)
    video_watch_origin: str | None = None

    @field_validator("kinescope_token_file", mode="before")
    @classmethod
    def optional_kinescope_file(cls, value):
        return None if value == "" else value

    @field_validator("video_watch_origin")
    @classmethod
    def valid_watch_origin(cls, value: str | None) -> str | None:
        if value is None:
            return None
        url = urlsplit(value)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.path
            or url.query
            or url.fragment
            or url.port is not None
            or value.lower() != f"https://{url.netloc.lower()}"
        ):
            raise ValueError("expected an HTTPS origin without path or credentials")
        cls.valid_host(url.hostname)
        return value.lower()

    def bot_token(self) -> str | None:
        if self.bot_token_file is None:
            return None
        token = read_secret(self.bot_token_file, "internal bot token file")
        if len(token) < 32:
            raise ConfigurationError("internal bot token is too short")
        if len(token) > 256 or not re.fullmatch(r"[A-Za-z0-9._~+/-]+=*", token):
            raise ConfigurationError("internal bot token has invalid format")
        return token


class MigrationSettings(DatabaseSettings):
    application_role: str = Field(
        default="gl_api_app", validation_alias="GL_API_MIGRATION_APP_ROLE"
    )

    # Never inherit the runtime role. The password fallback supports older
    # Compose releases; the dedicated migration variable always wins.
    database_user: str = Field(
        default="gl_api_migrator", validation_alias="GL_API_MIGRATION_DATABASE_USER"
    )
    database_password_file: Path = Field(
        default=Path("/run/secrets/gl_api_migrator_password"),
        repr=False,
        validation_alias=AliasChoices(
            "GL_API_MIGRATION_DATABASE_PASSWORD_FILE", "GL_API_DATABASE_PASSWORD_FILE"
        ),
    )
    model_config = SettingsConfigDict(populate_by_name=False, extra="forbid")


def load_settings[ConfigType: DatabaseSettings](
    settings_class: type[ConfigType] = Settings,
) -> ConfigType:
    unsupported = ["GL_API_DATABASE_PASSWORD", "GL_API_DATABASE_URL"]
    if issubclass(settings_class, Settings):
        unsupported.append("GL_API_BOT_TOKEN")
        unsupported.append("GL_API_KINESCOPE_TOKEN")
        unsupported.append("GL_API_ATOL_TOKEN")
    for name in unsupported:
        if name in os.environ:
            raise ConfigurationError(f"{name} is unsupported; use a secret file")
    try:
        return settings_class()
    except ValidationError as error:
        # errors()/json() may still contain input even with hide_input_in_errors.
        # Extract only field/alias names, never error details or input values.
        fields = ", ".join(sorted({str(item["loc"][0]) for item in error.errors()}))
        raise ConfigurationError(f"invalid settings: {fields}") from None
