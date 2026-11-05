"""RecordingGateway over the private API. No provider, SQL or payment authority.

Uses explicit backend public codes and UID-scoped entitlements.
"""

import json
import re

import aiohttp
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, ValidationError

from gl_api.order_contract import OrderIntent, PendingOrder

from .api_repository import ApiRepository, BackendError
from .recordings import Checkout, OwnedRecording, Playback, Recording, secure_link

_ABSENT = object()
MAX_RESPONSE_BYTES = 65536
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=8, connect=3, sock_read=5)


class RecordingContractUnavailable(BackendError):
    """An operation requires a backend contract that is not available yet."""


class _Link(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    order_id: StrictInt = Field(gt=0, lt=10**18)
    payment_url: StrictStr = Field(min_length=1, max_length=8192)


class _Watch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    watch_url: StrictStr = Field(min_length=1, max_length=8192)
    expires_in: StrictInt = Field(gt=0)


class ApiRecordingGateway:
    def __init__(self, repo: ApiRepository):
        self.repo = repo

    async def _post(self, path: str, body: dict, *, absent: tuple[int, ...] = ()):
        try:
            async with self.repo.session.post(
                self.repo.base_url + path,
                json=body,
                headers={"Authorization": f"Bearer {self.repo.token}"},
                timeout=REQUEST_TIMEOUT,
                allow_redirects=False,
            ) as response:
                if response.status in absent:
                    return _ABSENT
                if response.status != 200:
                    raise BackendError("recording backend unavailable")
                if response.content_type != "application/json":
                    raise BackendError("invalid recording response")
                raw = bytearray()
                async for chunk in response.content.iter_chunked(8192):
                    raw.extend(chunk)
                    if len(raw) > MAX_RESPONSE_BYTES:
                        raise BackendError("invalid recording response")
                return json.loads(raw)
        except (aiohttp.ClientError, TimeoutError):
            raise BackendError("recording backend unavailable") from None
        except (ValueError, UnicodeError, RecursionError):
            raise BackendError("invalid recording response") from None

    async def _operation(self, operation: str, *args):
        payload = await self._post(f"/internal/bot/{operation}", {"args": list(args)})
        if not isinstance(payload, dict) or set(payload) != {"result"}:
            raise BackendError("invalid recording response")
        return payload["result"]

    @staticmethod
    def _identifiers(**values):
        if any(type(v) is not int or not 0 < v < 10**18 for v in values.values()):
            raise ValueError("invalid recording identifiers")

    async def _recording(self, webinar_id: int, *, historical: bool = False) -> Recording | None:
        payload = await self._operation(
            "get_recording_context" if historical else "get_webinar", webinar_id
        )
        if payload is None:
            return None
        try:
            if isinstance(payload, dict) and payload.get("description") is None:
                payload = {**payload, "description": ""}
            recording = Recording.model_validate(payload)
        except ValidationError:
            raise BackendError("invalid recording response") from None
        if recording.id != webinar_id:
            raise BackendError("invalid recording context")
        return recording

    async def resolve(self, public_code: str) -> Recording | None:
        if not isinstance(public_code, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{1,64}", public_code
        ):
            return None
        payload = await self._operation("resolve_recording", public_code)
        if payload is None:
            return None
        try:
            if isinstance(payload, dict) and payload.get("description") is None:
                payload = {**payload, "description": ""}
            return Recording.model_validate(payload)
        except ValidationError:
            raise BackendError("invalid recording response") from None

    async def checkout(self, telegram_id: int, webinar_id: int, source_update_id: int) -> Checkout:
        self._identifiers(
            telegram_id=telegram_id, webinar_id=webinar_id, source_update_id=source_update_id
        )
        recording = await self._recording(webinar_id)
        if recording is None:
            raise BackendError("recording unavailable")
        intent = OrderIntent(
            telegram_id=telegram_id, webinar_id=webinar_id, source_update_id=source_update_id
        )
        payload = await self._post("/internal/order-intents", intent.model_dump())
        order = self._checkout(payload, recording.title)
        if order.webinar_id != webinar_id:
            raise BackendError("invalid recording context")
        if order.status != "pending":
            return order
        link = await self._post(
            "/internal/checkouts",
            {"telegram_id": telegram_id, "order_id": order.id},
            absent=(404, 409),
        )
        # Reservation/uncertainty is pending, not permission to try again.
        if link is _ABSENT:
            return order
        try:
            result = _Link.model_validate(link)
            secure_link(result.payment_url)
            if result.order_id != order.id:
                raise ValueError("mismatched order")
            return Checkout.model_validate(
                {**order.model_dump(), "payment_url": result.payment_url}
            )
        except (ValidationError, ValueError):
            raise BackendError("invalid recording response") from None

    @staticmethod
    def _checkout(payload, title: str) -> Checkout:
        try:
            order = PendingOrder.model_validate(payload)
            return Checkout(**order.model_dump(), title=title, expires_at=None)
        except ValidationError:
            raise BackendError("invalid recording response") from None

    async def order(self, telegram_id: int, order_id: int) -> Checkout | None:
        self._identifiers(telegram_id=telegram_id, order_id=order_id)
        payload = await self._post(
            "/internal/order-status",
            {"telegram_id": telegram_id, "order_id": order_id},
            absent=(404,),
        )
        if payload is _ABSENT:
            return None
        try:
            result = PendingOrder.model_validate(payload)
        except ValidationError:
            raise BackendError("invalid recording response") from None
        if result.id != order_id:
            raise BackendError("invalid recording context")
        recording = await self._recording(result.webinar_id, historical=True)
        if recording is None:
            raise RecordingContractUnavailable("order catalog context unavailable")
        # A status read never registers/retries a hosted checkout.
        return self._checkout(payload, recording.title)

    async def owned(self, telegram_id: int) -> list[OwnedRecording]:
        self._identifiers(telegram_id=telegram_id)
        rows = await self._operation("list_owned_recordings", telegram_id)
        if not isinstance(rows, list) or len(rows) > 20:
            raise BackendError("invalid recording response")
        try:
            result = [OwnedRecording.model_validate(row) for row in rows]
            if len({row.webinar_id for row in result}) != len(result):
                raise ValueError("duplicate recording")
            return result
        except (ValidationError, ValueError):
            raise BackendError("invalid recording response") from None

    async def playback(self, telegram_id: int, webinar_id: int) -> Playback | None:
        self._identifiers(telegram_id=telegram_id, webinar_id=webinar_id)
        recording = await self._recording(webinar_id, historical=True)
        if recording is None:
            return None
        payload = await self._post(
            "/internal/video-sessions",
            {"telegram_id": telegram_id, "webinar_id": webinar_id},
            absent=(403, 404),
        )
        if payload is _ABSENT:
            return None
        try:
            result = _Watch.model_validate(payload)
            return Playback(webinar_id=webinar_id, title=recording.title, url=result.watch_url)
        except ValidationError:
            raise BackendError("invalid recording response") from None
