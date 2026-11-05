"""Bounded ATOL Pay ECOM transport; no direct card or fiscal API integration.

Registration is never retried here. A lost response is an ambiguous operation,
not permission to create a second payment. Status observations alone do not grant
access; the order service must verify the persisted registration association.
"""

import json
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

import aiohttp

from gl_api.config import ConfigurationError, read_secret

ORIGINS = {
    "sandbox": "https://croc-sandbox-api-mobile.atolpay.ru",
    "production": "https://new-api-mobile.atolpay.ru",
}
MAX_RESPONSE_BYTES = 1024 * 1024


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    """Reject ambiguous provider JSON instead of trusting the last duplicate field."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


class AtolPayError(RuntimeError):
    """Fixed safe operational error; never includes provider text or credentials."""


class RegistrationUncertain(AtolPayError):
    """Reconcile the same merchant order ID; do not blindly repeat registration."""


class PaymentStatus(IntEnum):
    PROCESSING = 0
    PAID = 1
    BANK_UNAVAILABLE = 2
    FAILED = 3
    CANCELLED = 4
    REFUNDED = 5
    FRAUD = 6
    PARTIALLY_CANCELLED = 7
    PARTIALLY_REFUNDED = 8
    EXPIRED = 9
    CONFIRM_3DS = 10
    AWAITING_CAPTURE = 11
    RETRYABLE_FAILURE = 12
    CARD_CHECKED = 255


def _identifier(value: str) -> str:
    try:
        if type(value) is not str or str(UUID(value)) != value:
            raise ValueError()
    except (ValueError, AttributeError):
        raise ValueError("invalid merchant order ID") from None
    return value


def _https_url(value: str, allowed_hosts: frozenset[str] | None = None) -> str:
    try:
        parsed = urlsplit(value)
        if (
            type(value) is not str
            or len(value) > 2048
            or any(ord(char) <= 32 or ord(char) == 127 for char in value)
            or "\\" in value
            or parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in (None, 443)
            or (allowed_hosts is not None and parsed.hostname not in allowed_hosts)
        ):
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise ValueError("invalid payment URL") from None
    return value


@dataclass(frozen=True)
class ReceiptPolicy:
    """Explicit operator-confirmed dictionary IDs; no tax/category defaults."""

    sno: int
    tax: int
    payment_method: int
    payment_subject: int

    def __post_init__(self):
        allowed = {
            "sno": {0, 1, 2, 4, 5},
            "tax": set(range(12)),
            "payment_method": set(range(7)),
            "payment_subject": set(range(27)),
        }
        if any(type(v) is not int or v not in allowed[k] for k, v in vars(self).items()):
            raise ValueError("invalid fiscal dictionary ID")


@dataclass(frozen=True)
class RegisteredPayment:
    order_id: str
    amount_kopeks: int
    payment_url: str = field(repr=False)


def load_api_token(path: Path) -> str:
    token = read_secret(path, "ATOL Pay token file")
    if any(ord(c) <= 32 or ord(c) >= 127 for c in token):
        raise ConfigurationError("ATOL Pay token format invalid")
    return token


class AtolPayClient:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        token_file: Path,
        *,
        environment: Literal["sandbox", "production"],
        payment_hosts: frozenset[str],
    ):
        if environment not in ORIGINS or not payment_hosts or session.trust_env:
            raise ConfigurationError("ATOL environment and payment hosts must be configured")
        self._origin = ORIGINS[environment]
        self._session = session
        self._payment_hosts = payment_hosts
        self._token = load_api_token(token_file)

    async def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        try:
            async with self._session.request(
                method,
                self._origin + "/v1/ecom" + path,
                json=payload,
                headers={"Authorization": f"Bearer {self._token}"},
                allow_redirects=False,
                ssl=True,
                timeout=aiohttp.ClientTimeout(total=10, connect=3),
            ) as response:
                if response.status != 200:
                    raise AtolPayError("ATOL request failed")
                body = bytearray()
                async for chunk in response.content.iter_chunked(16384):
                    body.extend(chunk)
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise AtolPayError("ATOL response too large")
                result = json.loads(body, object_pairs_hook=_unique_object)
                if (
                    not isinstance(result, dict)
                    or result.get("status") != "success"
                    or not isinstance(result.get("data"), dict)
                ):
                    raise AtolPayError("ATOL response invalid")
                return result["data"]
        except (aiohttp.ClientError, TimeoutError, OSError):
            raise AtolPayError("ATOL unavailable") from None
        except (ValueError, RecursionError):
            raise AtolPayError("ATOL response invalid") from None

    async def register(
        self,
        order_id: str,
        amount_kopeks: int,
        recording_name: str,
        return_url: str,
        receipt: ReceiptPolicy,
        *,
        notification_url: str | None = None,
    ) -> RegisteredPayment:
        _identifier(order_id)
        if type(amount_kopeks) is not int or not 1 <= amount_kopeks <= 2_000_000_000:
            raise ValueError("invalid payment amount")
        if (
            type(recording_name) is not str
            or not recording_name.strip()
            or len(recording_name) > 128
            or any(ord(c) < 32 for c in recording_name)
        ):
            raise ValueError("invalid recording name")
        _https_url(return_url)
        if len(return_url) > 255 or not isinstance(receipt, ReceiptPolicy):
            raise ValueError("invalid payment registration configuration")
        if notification_url is not None:
            _https_url(notification_url)
            if len(notification_url) > 255:
                raise ValueError("invalid payment registration configuration")
        payload = {
            "orderId": order_id,
            "amount": amount_kopeks,
            "sessionType": "oneStep",
            "recurring": False,
            "additionalProps": {"returnUrl": return_url},
            "receipt": {
                "providerId": 100,
                "sno": receipt.sno,
                "positions": [
                    {
                        "name": recording_name,
                        "price": amount_kopeks,
                        "quantity": 1000,
                        "measure": 0,
                        "paymentMethod": receipt.payment_method,
                        "paymentSubject": receipt.payment_subject,
                        "tax": receipt.tax,
                    }
                ],
            },
        }
        # Omit buyer.email: provider collects it on its hosted payment page.
        if notification_url is not None:
            payload["additionalProps"]["notificationUrl"] = notification_url
        # Omit paymentMethods: use the account's existing bank configuration.
        try:
            data = await self._request("POST", "/payments", payload)
            if (
                data.get("orderId") != order_id
                or type(data.get("amount")) is not int
                or data["amount"] != amount_kopeks
            ):
                raise AtolPayError("ATOL registration mismatch")
            urls = data.get("paymentUrls")
            if not isinstance(urls, dict):
                raise AtolPayError("ATOL payment URL missing")
            url = _https_url(urls.get("main"), self._payment_hosts)
            return RegisteredPayment(order_id, amount_kopeks, url)
        except (AtolPayError, ValueError):
            raise RegistrationUncertain("ATOL registration requires reconciliation") from None

    async def status(self, order_id: str) -> PaymentStatus:
        data = await self._request("GET", f"/payments/{_identifier(order_id)}/status")
        try:
            if type(data.get("status")) is not int:
                raise ValueError()
            return PaymentStatus(data["status"])
        except ValueError:
            raise AtolPayError("ATOL payment status invalid") from None

    async def receipt_dictionaries(self) -> dict:
        return await self._request("GET", "/receipts/dictionaries")
