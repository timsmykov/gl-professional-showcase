"""Bot-side presentation contract. No payment/provider/SQL implementation here.

The internal HTTP adapter is opt-in; missing public-code and entitlement-ID
contracts remain explicit blockers. Identity, ownership and idempotency belong
to the backend.
"""

from datetime import datetime
from typing import Literal, Protocol
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator


def secure_link(value: str) -> str:
    """Validate a backend-issued HTTPS link without constructing or changing it."""
    if any(char.isspace() or ord(char) < 32 for char in value) or "\\" in value:
        raise ValueError("invalid link")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValueError("invalid link") from None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
    ):
        raise ValueError("invalid link")
    return value


class Recording(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: StrictInt = Field(gt=0, lt=10**18)
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(max_length=2500)
    price_kopeks: StrictInt | None = Field(default=None, ge=0)


class Checkout(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: StrictInt = Field(gt=0, lt=10**18)
    webinar_id: StrictInt = Field(gt=0, lt=10**18)
    title: str = Field(min_length=1, max_length=200)
    amount_kopeks: StrictInt = Field(gt=0)
    currency: Literal["RUB"] = "RUB"
    status: Literal["pending", "paid", "cancelled", "refunded"]
    payment_url: str | None = None
    expires_at: datetime | None = None

    @field_validator("payment_url")
    @classmethod
    def validate_link(cls, value):
        return secure_link(value) if value is not None else None

    @field_validator("expires_at")
    @classmethod
    def aware_expiry(cls, value):
        if value is not None and value.utcoffset() is None:
            raise ValueError("expiry requires timezone")
        return value


class OwnedRecording(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    webinar_id: StrictInt = Field(gt=0, lt=10**18)
    title: str = Field(min_length=1, max_length=200)


class Playback(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    webinar_id: StrictInt = Field(gt=0, lt=10**18)
    title: str = Field(min_length=1, max_length=200)
    url: str

    @field_validator("url")
    @classmethod
    def validate_link(cls, value):
        return secure_link(value)


class RecordingGateway(Protocol):
    async def resolve(self, public_code: str) -> Recording | None: ...

    async def checkout(
        self, telegram_id: int, webinar_id: int, source_update_id: int
    ) -> Checkout: ...

    async def order(self, telegram_id: int, order_id: int) -> Checkout | None: ...

    async def owned(self, telegram_id: int) -> list[OwnedRecording]: ...

    async def playback(self, telegram_id: int, webinar_id: int) -> Playback | None: ...


def format_price(kopeks: int | None) -> str:
    """Format a supplied catalog/checkout amount; no price is calculated here."""
    if kopeks is None:
        return "Цена уточняется"
    rubles, remainder = divmod(kopeks, 100)
    text = f"{rubles:,}".replace(",", " ")
    return text + (f".{remainder:02d}" if remainder else "") + " ₽"
