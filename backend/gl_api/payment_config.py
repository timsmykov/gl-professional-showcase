"""Explicit, disabled-by-default hosted-payment configuration."""

import re
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator


class PaymentSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    environment: Literal["sandbox", "production"]
    token_file: Path = Field(repr=False)
    payment_hosts: frozenset[str] = Field(min_length=1, max_length=8)
    return_url: str = Field(max_length=255, repr=False)
    sno: StrictInt
    tax: StrictInt = Field(ge=0, le=11)
    payment_method: StrictInt = Field(ge=0, le=6)
    payment_subject: StrictInt = Field(ge=0, le=26)
    poll_interval_seconds: int = Field(default=10, ge=5, le=60)
    notification_endpoint: str | None = Field(default=None, repr=False)
    notification_key_file: Path | None = Field(default=None, repr=False)

    @model_validator(mode="after")
    def notification_pair(self):
        if (self.notification_endpoint is None) != (self.notification_key_file is None):
            raise ValueError("fiscal notifications require endpoint and key file together")
        return self

    @field_validator("sno")
    @classmethod
    def valid_sno(cls, value):
        if value not in {0, 1, 2, 4, 5}:
            raise ValueError("invalid fiscal dictionary ID")
        return value

    @field_validator("payment_hosts")
    @classmethod
    def valid_hosts(cls, hosts):
        for host in hosts:
            if len(host) > 253 or not re.fullmatch(r"[a-z0-9]+(?:[a-z0-9.-]*[a-z0-9])?", host):
                raise ValueError("expected lowercase payment hostnames")
            if any(
                not label or len(label) > 63 or label.startswith("-") or label.endswith("-")
                for label in host.split(".")
            ):
                raise ValueError("invalid payment hostname")
        return hosts

    @field_validator("return_url")
    @classmethod
    def valid_return_url(cls, value):
        try:
            parsed = urlsplit(value)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.port not in (None, 443)
                or "\\" in value
                or any(ord(c) <= 32 or ord(c) == 127 for c in value)
            ):
                raise ValueError()
        except ValueError:
            raise ValueError("expected a valid HTTPS return URL") from None
        return value
