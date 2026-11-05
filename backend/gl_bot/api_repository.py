"""HTTP repository adapter for the bot; no database credentials in this process."""

from datetime import datetime

import aiohttp
from pydantic import ValidationError

from gl_api.bot_contract import ARG_TYPES, valid_args
from gl_api.order_contract import OrderIntent, PendingOrder


class BackendError(RuntimeError):
    """The internal API could not complete an operation."""


class ApiRepository:
    def __init__(self, session: aiohttp.ClientSession, base_url: str, token: str):
        self.session = session
        self.base_url = base_url.rstrip("/")
        self.token = token

    async def _call(self, operation: str, *args):
        if not valid_args(operation, list(args)):
            raise ValueError("invalid bot repository call")
        try:
            async with self.session.post(
                f"{self.base_url}/internal/bot/{operation}",
                json={"args": list(args)},
                headers={"Authorization": f"Bearer {self.token}"},
            ) as response:
                if response.status != 200:
                    raise BackendError(f"backend status {response.status}")
                try:
                    payload = await response.json()
                except (aiohttp.ContentTypeError, ValueError):
                    raise BackendError("invalid backend response") from None
        except (aiohttp.ClientError, TimeoutError) as error:
            raise BackendError("backend unavailable") from error
        if not isinstance(payload, dict) or "result" not in payload:
            raise BackendError("invalid backend response")
        try:
            return self._restore_dates(payload["result"])
        except (TypeError, ValueError):
            raise BackendError("invalid backend response") from None

    @classmethod
    def _restore_dates(cls, value):
        if isinstance(value, list):
            return [cls._restore_dates(item) for item in value]
        if isinstance(value, dict):
            return {
                key: datetime.fromisoformat(item)
                if key in {"starts_at", "ends_at"} and item
                else cls._restore_dates(item)
                for key, item in value.items()
            }
        return value

    def __getattr__(self, operation: str):
        if operation not in ARG_TYPES:
            raise AttributeError(operation)

        async def invoke(*args):
            return await self._call(operation, *args)

        return invoke

    async def create_order_intent(
        self, telegram_id: int, webinar_id: int, source_update_id: int
    ) -> dict[str, int | str]:
        try:
            intent = OrderIntent.model_validate(
                {
                    "telegram_id": telegram_id,
                    "webinar_id": webinar_id,
                    "source_update_id": source_update_id,
                }
            )
        except ValidationError:
            raise ValueError("invalid order intent") from None
        if telegram_id == 0:
            raise ValueError("invalid order intent")
        try:
            async with self.session.post(
                f"{self.base_url}/internal/order-intents",
                json=intent.model_dump(),
                headers={"Authorization": f"Bearer {self.token}"},
            ) as response:
                if response.status != 200:
                    raise BackendError(f"backend status {response.status}")
                try:
                    payload = await response.json()
                except (aiohttp.ContentTypeError, ValueError):
                    raise BackendError("invalid backend response") from None
        except (aiohttp.ClientError, TimeoutError) as error:
            raise BackendError("backend unavailable") from error
        try:
            return PendingOrder.model_validate(payload).model_dump()
        except ValidationError:
            raise BackendError("invalid backend response") from None
