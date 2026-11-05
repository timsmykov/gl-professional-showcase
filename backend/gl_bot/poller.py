"""Restricted Telegram polling for the temporary, synthetic live preview."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from pathlib import Path

import aiohttp
import asyncpg
from aiogram import Bot
from aiogram.types import Update

from .api_recordings import ApiRecordingGateway
from .api_repository import ApiRepository
from .flow import BotFlow
from .products import ProductCard
from .repository import Repository
from .settings import Settings
from .telegram import AiogramMessenger, Keyboard
from .transport import create_bot

logger = logging.getLogger(__name__)
HEARTBEAT = Path("/tmp/gl-preview-heartbeat")


DEFAULT_MESSAGE_LABEL = "🧪 ДЕМО · G&L"


def message_label_from_env() -> str:
    label = os.environ.get("BOT_MESSAGE_LABEL", DEFAULT_MESSAGE_LABEL)
    if len(label) > 80 or any(ord(char) < 32 or ord(char) == 127 for char in label):
        raise ValueError("BOT_MESSAGE_LABEL is invalid")
    return label


class PreviewMessenger:
    def __init__(self, bot: Bot, label: str = DEFAULT_MESSAGE_LABEL) -> None:
        self.delegate = AiogramMessenger(bot, rich=not label)
        self.label = label

    async def send(self, chat_id: int, text: str, buttons: Keyboard | None = None) -> int:
        body = f"{self.label}\n\n{text}" if self.label else text
        return await self.delegate.send(chat_id, body, buttons)

    async def answer(self, callback_id: str) -> None:
        await self.delegate.answer(callback_id)

    async def send_product(self, chat_id: int, card: ProductCard, buttons: Keyboard) -> int:
        return await self.delegate.send_product(chat_id, card, buttons)


def allowed_private_update(update: Update, allowed_user_id: int) -> bool:
    message = update.message
    callback = update.callback_query
    sender = message.from_user if message else callback.from_user if callback else None
    chat = (
        message.chat if message else getattr(callback.message, "chat", None) if callback else None
    )
    return bool(
        sender
        and chat
        and sender.id == allowed_user_id
        and chat.id == allowed_user_id
        and chat.type == "private"
    )


def read_allowed_user_id(path: str) -> int:
    value = Path(path).read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"[1-9][0-9]{0,17}", value):
        raise ValueError("PREVIEW_ALLOWED_USER_ID_FILE is invalid")
    return int(value)


async def run() -> None:
    settings = Settings.from_env()
    message_label = message_label_from_env()
    allowed_path = os.environ["PREVIEW_ALLOWED_USER_ID_FILE"]
    allowed_user_id = read_allowed_user_id(allowed_path)
    expected_bot_id = int(os.environ["PREVIEW_EXPECTED_BOT_ID"])
    pool = None
    session = None
    bot = create_bot(settings)
    try:
        if settings.api_url:
            session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8))
            repo = ApiRepository(session, settings.api_url, settings.api_token or "")
        else:
            pool = await asyncpg.create_pool(
                dsn=settings.database_url, min_size=1, max_size=4, timeout=5
            )
            repo = Repository(pool)
        await repo.health()
        me = await bot.get_me()
        if me.id != expected_bot_id:
            raise RuntimeError("Telegram token belongs to a different bot")
        webhook = await bot.get_webhook_info()
        if webhook.url:
            raise RuntimeError("An existing webhook must be reviewed before preview polling")

        flow = BotFlow(
            repo,
            PreviewMessenger(bot, label=message_label),
            german_telegram_id=allowed_user_id,
            contact_url=settings.german_contact_url,
            time_zone=settings.display_time_zone,
            product_cards=True,
            recordings=ApiRecordingGateway(repo) if settings.recordings_api_enabled else None,
        )
        offset: int | None = None
        logger.warning("restricted Telegram polling started")
        while True:
            try:
                updates = await bot.get_updates(
                    offset=offset,
                    timeout=20,
                    allowed_updates=["message", "callback_query"],
                )
                HEARTBEAT.write_text(str(time.time()), encoding="ascii")
                for update in updates:
                    if allowed_private_update(update, allowed_user_id):
                        await flow.handle(update)
                    offset = update.update_id + 1
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - polling must recover after transient failures.
                logger.error("preview_poll_failed type=%s", type(error).__name__)
                await asyncio.sleep(3)
    finally:
        await bot.session.close()
        if session:
            await session.close()
        if pool:
            await pool.close()


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s")
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    except Exception as error:  # noqa: BLE001 - startup must not print private details.
        logger.error("bot_startup_failed type=%s", type(error).__name__)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
