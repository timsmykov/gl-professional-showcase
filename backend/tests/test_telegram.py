from types import SimpleNamespace

import pytest

from gl_bot.telegram import AiogramMessenger, callback_button, message_chunks, url_button


@pytest.mark.asyncio
async def test_aiogram_adapter_builds_callback_and_contact_buttons():
    class Bot:
        async def send_message(self, **kwargs):
            self.payload = kwargs
            return SimpleNamespace(message_id=123)

        async def answer_callback_query(self, **kwargs):
            self.answer = kwargs

    bot = Bot()
    messenger = AiogramMessenger(bot)
    message_id = await messenger.send(
        10,
        "Выбор",
        [
            [
                callback_button("Косметолог", "section:pro"),
                url_button("Написать Герману", "https://t.me/german_example"),
            ]
        ],
    )
    assert message_id == 123
    buttons = bot.payload["reply_markup"].inline_keyboard[0]
    assert buttons[0].callback_data == "section:pro"
    assert buttons[1].url == "https://t.me/german_example"
    await messenger.answer("callback-1")
    assert bot.answer["callback_query_id"] == "callback-1"


@pytest.mark.asyncio
async def test_long_description_is_preserved_and_navigation_is_on_last_message():
    class Bot:
        def __init__(self):
            self.payloads = []

        async def send_message(self, **kwargs):
            self.payloads.append(kwargs)
            return SimpleNamespace(message_id=len(self.payloads))

    text = "Длинное описание.\n" * 400
    bot = Bot()
    result = await AiogramMessenger(bot).send(10, text, [[callback_button("Назад", "pro:menu")]])
    assert "".join(item["text"] for item in bot.payloads) == text
    assert all(len(item["text"]) <= 3800 for item in bot.payloads)
    assert all(item["reply_markup"] is None for item in bot.payloads[:-1])
    assert bot.payloads[-1]["reply_markup"].inline_keyboard[0][0].callback_data == "pro:menu"
    assert result == len(bot.payloads)


def test_astral_unicode_does_not_exceed_message_budget():
    text = "🌿" * 3000
    chunks = message_chunks(text)
    assert "".join(chunks) == text
    assert all(len(chunk.encode("utf-16-le")) // 2 <= 3800 for chunk in chunks)


def test_native_emphasis_uses_utf16_offsets_and_preserves_literal_markup():
    from gl_bot.telegram import message_entities

    text = "🛠 DEV · G&L\n\n🌿 G&L <b>Название</b>\n\nОписание & [ссылка](evil)\n\n1 500 ₽\nОплата подтверждена."
    entities = message_entities(text)
    encoded = text.encode("utf-16-le")
    spans = [
        (item.type, encoded[item.offset * 2 : (item.offset + item.length) * 2].decode("utf-16-le"))
        for item in entities
    ]
    assert spans == [
        ("italic", "🛠 DEV · G&L"),
        ("bold", "🌿 G&L <b>Название</b>"),
        ("bold", "1 500 ₽"),
        ("bold", "Оплата подтверждена."),
    ]
    assert all(item.url is None for item in entities)


@pytest.mark.asyncio
async def test_native_entities_survive_chunk_boundary_without_parse_mode():
    from gl_bot.telegram import message_entities

    class Bot:
        def __init__(self):
            self.payloads = []

        async def send_message(self, **kwargs):
            self.payloads.append(kwargs)
            return SimpleNamespace(message_id=len(self.payloads))

    text = "🌿 Заголовок\n\n" + "я" * 3778 + "\n1 500 ₽\nОписание <b>без HTML</b>"
    bot = Bot()
    await AiogramMessenger(bot).send(
        10, text, [[callback_button("Купить", "buy", style="primary")]]
    )
    assert len(bot.payloads) == 2
    original_entities = message_entities(text)
    start = 0
    reconstructed = []
    for payload in bot.payloads:
        assert payload["parse_mode"] is None
        size = len(payload["text"].encode("utf-16-le")) // 2
        for entity in payload["entities"]:
            assert 0 <= entity.offset < entity.offset + entity.length <= size
            reconstructed.append((entity.type, start + entity.offset, entity.length))
        start += size
    for entity in original_entities:
        fragments = [
            (offset, size)
            for kind, offset, size in reconstructed
            if kind == entity.type and entity.offset <= offset < entity.offset + entity.length
        ]
        assert sum(size for _, size in fragments) == entity.length
    assert bot.payloads[-1]["reply_markup"].inline_keyboard[0][0].style == "primary"
    assert "".join(payload["text"] for payload in bot.payloads) == text


def test_single_line_and_long_heading_are_not_interpreted_as_markup():
    from gl_bot.telegram import message_entities

    assert message_entities("<b>" + "x" * 100 + "</b>\n\nОписание") == []
    assert callback_button("Назад", "menu") == {"text": "Назад", "callback_data": "menu"}
    assert url_button("Оплатить", "https://example.invalid", style="primary")["style"] == "primary"
