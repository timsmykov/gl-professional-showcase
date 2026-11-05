from __future__ import annotations

import re
from html import escape
from typing import Literal, Protocol

from aiogram import Bot
from aiogram.types import (
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputRichMessage,
    InputRichMessageMedia,
    LinkPreviewOptions,
    MessageEntity,
)

from .buttons import button_rows
from .products import ProductCard

Button = dict[str, str]
Keyboard = list[list[Button]]
ButtonStyle = Literal["primary", "success", "danger"]


def utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def message_entities(text: str) -> list[MessageEntity]:
    """Native emphasis only: catalogue text is never parsed as markup."""
    entities = []
    offset = 0
    heading = True
    for line in text.splitlines(keepends=True):
        content = line.rstrip("\r\n")
        if content:
            if heading and content.startswith(("🛠 DEV ·", "🧪 ДЕМО ·")):
                entities.append(
                    MessageEntity(type="italic", offset=offset, length=utf16_length(content))
                )
            else:
                is_heading = heading and len(content) <= 96
                is_price = bool(re.fullmatch(r"[\d \u00a0]+(?:[,.]\d{2})? ₽", content))
                is_status = content in {
                    "Оплата подтверждена.",
                    "Оплата пока не подтверждена.",
                    "Ссылка для оплаты готова.",
                }
                if is_heading or is_price or is_status:
                    entities.append(
                        MessageEntity(type="bold", offset=offset, length=utf16_length(content))
                    )
                heading = False
        offset += utf16_length(line)
    return entities


def chunk_entities(entities: list[MessageEntity], start: int, length: int) -> list[MessageEntity]:
    result = []
    for entity in entities:
        left = max(start, entity.offset)
        right = min(start + length, entity.offset + entity.length)
        if left < right:
            result.append(
                entity.model_copy(update={"offset": left - start, "length": right - left})
            )
    return result


def message_chunks(text: str, limit: int = 3800) -> list[str]:
    """Keep long approved descriptions intact within Telegram's text budget."""
    chunks = []
    start, size = 0, 0
    for index, character in enumerate(text):
        width = 2 if ord(character) > 0xFFFF else 1
        if size + width > limit:
            chunks.append(text[start:index])
            start, size = index, 0
        size += width
    if text[start:] or not chunks:
        chunks.append(text[start:])
    return chunks


def callback_button(label: str, data: str, *, style: ButtonStyle | None = None) -> Button:
    return {"text": label, "callback_data": data, **({"style": style} if style else {})}


def url_button(label: str, address: str, *, style: ButtonStyle | None = None) -> Button:
    return {"text": label, "url": address, **({"style": style} if style else {})}


class Messenger(Protocol):
    async def send(
        self, chat_id: int, text: str, buttons: Keyboard | None = None
    ) -> int | None: ...

    async def answer(self, callback_id: str) -> None: ...


class AiogramMessenger:
    def __init__(self, bot: Bot, *, rich: bool = False):
        self.bot = bot
        self.rich = rich

    @staticmethod
    def markup(buttons: Keyboard | None) -> InlineKeyboardMarkup | None:
        rows = button_rows(buttons)
        if not rows:
            return None
        return InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(**button) for button in row] for row in rows]
        )

    async def send_product(self, chat_id: int, card: ProductCard, buttons: Keyboard) -> int:
        message = await self.bot.send_rich_message(
            chat_id=chat_id,
            rich_message=InputRichMessage(
                html=card.html,
                skip_entity_detection=True,
                media=[
                    InputRichMessageMedia(
                        id="product", media=InputMediaPhoto(media=FSInputFile(card.photo))
                    )
                ],
            ),
            reply_markup=self.markup(buttons),
        )
        return message.message_id

    async def send(self, chat_id: int, text: str, buttons: Keyboard | None = None) -> int:
        markup = self.markup(buttons)
        # Rich is for short scripted screens. Long/operational responses retain
        # the established lossless entity/chunk transport and recovery behaviour.
        if self.rich and utf16_length(text) <= 3800:
            parts = text.split("\n\n")
            body = "".join(
                f"<{tag}>{escape(part).replace(chr(10), '<br/>')}</{tag}>"
                for index, part in enumerate(parts)
                if part
                for tag in ["h2" if index == 0 and len(part) <= 96 else "p"]
            )
            message = await self.bot.send_rich_message(
                chat_id=chat_id,
                rich_message=InputRichMessage(html=body, skip_entity_detection=True),
                reply_markup=markup,
            )
            return message.message_id
        chunks = message_chunks(text)
        entities = message_entities(text)
        offset = 0
        for index, chunk in enumerate(chunks):
            message = await self.bot.send_message(
                chat_id=chat_id,
                text=chunk,
                parse_mode=None,
                entities=chunk_entities(entities, offset, utf16_length(chunk)),
                reply_markup=markup if index == len(chunks) - 1 else None,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
            offset += utf16_length(chunk)
        return message.message_id

    async def answer(self, callback_id: str) -> None:
        await self.bot.answer_callback_query(callback_query_id=callback_id)
