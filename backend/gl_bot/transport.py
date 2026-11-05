"""Explicit Telegram transport; a configured proxy never falls back to direct."""

from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession

from .settings import Settings


def create_bot(settings: Settings) -> Bot:
    if settings.telegram_proxy_url:
        return Bot(
            token=settings.telegram_bot_token,
            session=AiohttpSession(proxy=settings.telegram_proxy_url),
        )
    return Bot(token=settings.telegram_bot_token)
