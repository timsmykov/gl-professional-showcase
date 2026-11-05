"""Order-scoped callback credentials, not an invented provider signature scheme."""

import hashlib
import hmac
import json
import re
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

from gl_api.config import ConfigurationError, read_secret
from gl_api.fiscal import FiscalState, ReceiptObservation

MAX_NOTIFICATION_BYTES = 16384


class InvalidNotification(ValueError):
    """Safe boundary: never echo the request, identifiers or credentials."""


def merchant_uuid(value: object) -> UUID:
    try:
        if type(value) is not str:
            raise ValueError()
        identifier = UUID(value)
        if identifier.version != 4 or str(identifier) != value:
            raise ValueError()
        return identifier
    except ValueError:
        raise InvalidNotification("invalid notification") from None


@dataclass(frozen=True)
class NotificationCredentials:
    endpoint: str
    environment: str
    key: bytes = field(repr=False)

    def __post_init__(self):
        try:
            url = urlsplit(self.endpoint)
            if (
                url.scheme != "https"
                or not url.hostname
                or url.username is not None
                or url.password is not None
                or url.port not in (None, 443)
                or url.path != "/callbacks/atol/receipts"
                or url.query
                or url.fragment
                or "\\" in self.endpoint
                or any(ord(c) <= 32 or ord(c) == 127 for c in self.endpoint)
                or len(self.endpoint) + len("?token=") + 64 > 255
                or self.environment not in {"sandbox", "production"}
                or type(self.key) is not bytes
                or len(self.key) != 32
            ):
                raise ValueError()
        except (ValueError, TypeError):
            raise ConfigurationError("invalid fiscal notification configuration") from None

    @classmethod
    def from_file(cls, endpoint: str, environment: str, path: Path):
        value = read_secret(path, "fiscal notification key file")
        if not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ConfigurationError("fiscal notification key must be 32 bytes of hex")
        return cls(endpoint, environment, bytes.fromhex(value))

    def token(self, merchant_order_id: str) -> str:
        identifier = merchant_uuid(merchant_order_id)
        message = f"gl-atol-fiscal-v1:{self.environment}:{identifier}".encode("ascii")
        return hmac.new(self.key, message, hashlib.sha256).hexdigest()

    def notification_url(self, merchant_order_id: str) -> str:
        return f"{self.endpoint}?token={self.token(merchant_order_id)}"

    def verify(self, merchant_order_id: str, supplied: str) -> bool:
        return bool(
            type(supplied) is str
            and re.fullmatch(r"[0-9a-f]{64}", supplied)
            and hmac.compare_digest(self.token(merchant_order_id), supplied)
        )


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


def decode_notification(raw: bytes) -> dict:
    try:
        if not 0 < len(raw) <= MAX_NOTIFICATION_BYTES:
            raise ValueError()
        body = json.loads(
            raw,
            object_pairs_hook=_unique_object,
            parse_float=Decimal,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
        )
        if type(body) is not dict:
            raise ValueError()
        merchant_uuid(body.get("orderId"))
        return body
    except (ValueError, UnicodeError, RecursionError):
        raise InvalidNotification("invalid notification") from None


def normalize_receipt(body: dict, environment: str) -> ReceiptObservation | None:
    """Non-fiscal events are acknowledged but never used as payment authority."""
    kind = body.get("type")
    if type(kind) is str and kind in {"payment", "deposit", "cancel", "refund"}:
        return None
    if kind != "fiscal" or body.get("sessionType") != "oneStep":
        raise InvalidNotification("invalid notification")
    status = body.get("status")
    if type(status) is not str or status not in {"success", "fail", "failed"}:
        raise InvalidNotification("invalid notification")
    errors = any(body.get(key) not in (None, "") for key in ("errorCode", "errorMessage"))
    # The official example says success even with error fields. Do not approve it.
    state = (
        FiscalState.REVIEW
        if status == "success" and errors
        else FiscalState.SUCCESS
        if status == "success"
        else FiscalState.FAILED
    )
    try:
        amount = body.get("amount")
        if isinstance(amount, Decimal):
            if (
                not amount.is_finite()
                or not 1 <= amount <= 2_000_000_000
                or amount != amount.to_integral_value()
            ):
                raise ValueError()
            amount = int(amount)
        return ReceiptObservation(
            merchant_uuid(body.get("orderId")),
            merchant_uuid(body.get("receiptId")),
            environment,
            body.get("receiptType"),
            amount,
            state,
        )
    except ValueError:
        raise InvalidNotification("invalid notification") from None
