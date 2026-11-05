import hashlib
import struct
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import FSInputFile
from test_flow import Fixture

from gl_bot.products import PRODUCTS
from gl_bot.telegram import AiogramMessenger, callback_button


def test_published_assets_are_exact_copies_and_callbacks_fit_telegram():
    assert len(PRODUCTS) == 4
    for card in PRODUCTS.values():
        assert hashlib.sha256(card.photo.read_bytes()).hexdigest() == card.photo_sha256
        assert len(f"product:card:{card.slug}".encode()) <= 64
        assert card.description and card.composition
        assert "₽" not in card.text


def test_catalog_text_cannot_inject_rich_markup():
    card = replace(
        PRODUCTS["cute"],
        title='<a href="https://evil">X</a>',
        composition="<img src='https://evil'/> & <script>",
    )
    assert '<a href="https://evil">' not in card.html
    assert "&lt;script&gt;" in card.html
    assert card.html.count("<img ") == 1


@pytest.mark.asyncio
async def test_product_is_one_native_rich_message_with_uploaded_photo_and_navigation():
    bot = SimpleNamespace(send_rich_message=AsyncMock(return_value=SimpleNamespace(message_id=42)))
    card = PRODUCTS["plla-activator"]
    buttons = [[callback_button("Все препараты", "pro:products")]]
    assert await AiogramMessenger(bot).send_product(10, card, buttons) == 42
    payload = bot.send_rich_message.call_args.kwargs
    rich = payload["rich_message"]
    assert rich.html == card.html and rich.skip_entity_detection
    assert rich.media[0].id == "product"
    assert isinstance(rich.media[0].media.media, FSInputFile)
    assert rich.media[0].media.media.path == card.photo
    assert "<tg-button" not in rich.html
    assert payload["reply_markup"].inline_keyboard[0][0].callback_data == "pro:products"


@pytest.mark.asyncio
async def test_scripted_rich_screen_escapes_literal_content():
    bot = SimpleNamespace(send_rich_message=AsyncMock(return_value=SimpleNamespace(message_id=1)))
    await AiogramMessenger(bot, rich=True).send(10, "G&L\n\nТекст <b> & 🌿\nЕщё строка")
    html = bot.send_rich_message.call_args.kwargs["rich_message"].html
    assert html == "<h2>G&amp;L</h2><p>Текст &lt;b&gt; &amp; 🌿<br/>Ещё строка</p>"


@pytest.mark.asyncio
async def test_network_timeout_does_not_send_a_duplicate_fallback():
    bot = SimpleNamespace(
        send_rich_message=AsyncMock(side_effect=TimeoutError), send_message=AsyncMock()
    )
    with pytest.raises(TimeoutError):
        await AiogramMessenger(bot).send_product(10, PRODUCTS["cute"], [])
    bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_cards_repeat_entry_stale_code_and_legacy_backend_remain_separate():
    fixture = Fixture()
    fixture.flow.product_cards = True
    fixture.repo.list_products = AsyncMock(side_effect=AssertionError("No content SQL/API read"))
    await fixture.message()
    await fixture.click(10, "section:pro")
    await fixture.click(10, "pro:products")
    catalog = fixture.messenger.messages[-1]
    assert [row[0]["text"] for row in catalog["buttons"][:4]] == [
        c.title for c in PRODUCTS.values()
    ]
    for slug in PRODUCTS:
        await fixture.click(10, f"product:card:{slug}")
        assert fixture.messenger.messages[-1]["text"] == PRODUCTS[slug].text
    await fixture.click(10, "product:card:../../secret")
    assert fixture.messenger.messages[-1]["text"] == "Карточка больше недоступна."
    await fixture.message()
    assert "Косметолог" in fixture.messenger.messages[-1]["text"]
    assert not fixture.repo.bookings


@pytest.mark.asyncio
async def test_foreign_role_cannot_open_product_card():
    fixture = Fixture()
    fixture.flow.product_cards = True
    await fixture.message()
    await fixture.click(10, "product:card:cute")
    assert all("Состав" not in message["text"] for message in fixture.messenger.messages)


@pytest.mark.asyncio
async def test_scripted_rich_text_uses_only_standard_inline_keyboard():
    bot = SimpleNamespace(send_rich_message=AsyncMock(return_value=SimpleNamespace(message_id=1)))
    buttons = [[callback_button(c.title, f"product:card:{c.slug}")] for c in PRODUCTS.values()]
    await AiogramMessenger(bot, rich=True).send(10, "Препараты", buttons)
    payload = bot.send_rich_message.call_args.kwargs
    html = payload["rich_message"].html
    assert "<tg-button" not in html
    keyboard = payload["reply_markup"].inline_keyboard
    assert [len(row) for row in keyboard] == [2, 2]
    assert [b.callback_data for row in keyboard for b in row] == [
        f"product:card:{c.slug}" for c in PRODUCTS.values()
    ]


@pytest.mark.asyncio
async def test_all_scripted_screens_keep_targets_in_external_keyboard():
    from collections import Counter

    from render_bot_preview import scenes

    bot = SimpleNamespace(
        send_rich_message=AsyncMock(return_value=SimpleNamespace(message_id=1)),
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=1)),
    )
    messenger = AiogramMessenger(bot, rich=True)
    for name, scene in (await scenes()).items():
        bot.send_rich_message.reset_mock()
        bot.send_message.reset_mock()
        if name.startswith("product_") and name.removeprefix("product_") in PRODUCTS:
            await messenger.send_product(
                10, PRODUCTS[name.removeprefix("product_")], scene["buttons"]
            )
        else:
            await messenger.send(10, scene["text"], scene["buttons"])
        call = (bot.send_rich_message.call_args or bot.send_message.call_args).kwargs
        if "rich_message" in call:
            assert "<tg-button" not in call["rich_message"].html, name
        targets = lambda buttons: Counter((b.get("callback_data"), b.get("url")) for b in buttons)
        original = [b for row in scene["buttons"] for b in row]
        markup = call["reply_markup"]
        actual = [b.model_dump() for row in markup.inline_keyboard for b in row] if markup else []
        assert targets(original) == targets(actual), name
        assert all(b.get("style") != "link" for b in actual), name


def test_product_upload_keeps_full_size_photo_without_added_canvas():
    expected = {
        "cute": (1000, 1260),
        "cute-ultra": (1000, 1407),
        "plla-activator": (1000, 882),
        "nad-skin-matrix": (1000, 631),
    }
    for slug, dimensions in expected.items():
        card = PRODUCTS[slug]
        assert card.photo.name == f"{slug}.png"
        assert struct.unpack(">II", card.photo.read_bytes()[16:24]) == dimensions
