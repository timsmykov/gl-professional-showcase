"""Render an offline dialogue preview from real BotFlow and synthetic test doubles.

Run explicitly with `uv run --locked --extra test python tests/render_bot_preview.py`.
This script cannot connect to Telegram, PostgreSQL, payments or video providers.
"""

import argparse
import asyncio
import base64
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from test_flow import Fixture
from test_recording_dialogues import FakeRecordings

from gl_bot import copy
from gl_bot.api_repository import BackendError
from gl_bot.buttons import button_rows
from gl_bot.products import PRODUCTS
from gl_bot.recordings import OwnedRecording, Recording
from gl_bot.telegram import message_entities


async def scenes():
    fixture = Fixture()
    gateway = FakeRecordings()
    fixture.flow.recordings = gateway
    fixture.flow.product_cards = True

    async def send_product(chat_id, card, buttons):
        fixture.messenger.messages.append(
            {
                "chat_id": chat_id,
                "text": card.text,
                "buttons": buttons,
                "rich_html": card.html.replace(
                    "tg://photo?id=product", "../../gl_bot/assets/products/" + card.photo.name
                ),
            }
        )

    fixture.messenger.send_product = send_product
    fixture.flow.contact_url = "https://contact.example.invalid/german"
    gateway.recording = Recording(
        id=3,
        title="Знакомство с препаратами G&L",
        description="Демонстрационная карточка записи. Здесь будут описание и программа, согласованные с Германом.",
        price_kopeks=150000,
    )
    pending = gateway.result.model_copy(
        update={
            "title": gateway.recording.title,
            "amount_kopeks": 150000,
            "expires_at": datetime.now(UTC) + timedelta(hours=1),
        }
    )
    gateway.result = pending

    async def webinar(_id):
        return gateway.recording.model_dump() if _id == 3 else None

    async def webinars():
        return [gateway.recording.model_dump()]

    fixture.repo.get_webinar = webinar
    fixture.repo.list_webinars = webinars

    async def services():
        return [
            {
                "id": 1,
                "title": "Консультация · демо",
                "description": "Демонстрация выбора услуги. Здесь будет описание, согласованное с Германом.",
            }
        ]

    async def service(_id):
        return (await services())[0] if _id == 1 else None

    async def slots(_id):
        return [{"id": 7, "starts_at": datetime(2026, 10, 10, 10, tzinfo=UTC)}]

    create_booking = fixture.repo.create_booking

    async def booking(user_id, slot_id, update_id):
        result = await create_booking(user_id, slot_id, update_id)
        fixture.repo.bookings[result["id"]].update(
            title="Консультация · демо", starts_at=(await slots(1))[0]["starts_at"]
        )
        return result

    async def products():
        return [{"id": 2, "title": "G&L CUTE"}]

    async def product(_id):
        return (
            {
                "id": 2,
                "title": "G&L CUTE",
                "description": "Демонстрационная карточка препарата. Здесь будет информация из проверенных материалов G&L.",
            }
            if _id == 2
            else None
        )

    fixture.repo.list_services = services
    fixture.repo.get_service = service
    fixture.repo.list_slots = slots
    fixture.repo.create_booking = booking
    fixture.repo.list_products = products
    fixture.repo.get_product = product
    result = {}

    async def capture(key, action=None, text=None):
        if text is not None:
            await fixture.message(text=text)
        else:
            await fixture.click(10, action)
        result[key] = fixture.messenger.messages[-1]

    await capture("welcome", text="/start")
    await capture("pro", "section:pro")
    await capture("switch", "section:choose")
    await capture("catalog", "pro:webinars")
    await capture("card", "webinar:info:3")
    await capture("deep_link", text="/start demo_recording")
    await capture("pending", "webinar:buy:3")
    await capture("awaiting", "order:status:71")
    await capture("empty_owned", "pro:mine")
    await capture("recover", "pro:recover")
    await capture("invalid", text="/start old-code")
    await capture("contact", text="/contact")
    await capture("about", text="/about")
    await capture("faq", text="/faq")
    for key in copy.FAQ_PRO:
        await capture(f"faq_{key}", f"faq:{key}")
    await capture("free_text", text="Как выбрать вебинар?")

    for key, changes in {
        "expired": {"expires_at": datetime(2020, 1, 1, tzinfo=UTC)},
        "missing_link": {"payment_url": None},
        "paid": {"status": "paid"},
        "cancelled": {"status": "cancelled"},
        "refunded": {"status": "refunded"},
    }.items():
        gateway.result = pending.model_copy(update=changes)
        await capture(key, "order:status:71")
    gateway.result = pending

    async def fail(*_args):
        raise BackendError("synthetic unavailable")

    checkout = gateway.checkout
    gateway.checkout = fail
    try:
        await capture("api_error", "webinar:buy:3")
    except BackendError:
        result["api_error"] = fixture.messenger.messages[-1]
    gateway.checkout = checkout
    fixture.flow.recordings = None
    await capture("not_connected", "webinar:buy:3")
    fixture.flow.recordings = gateway

    gateway.access = [OwnedRecording(webinar_id=3, title=gateway.recording.title)]
    await capture("owned", "pro:mine")
    await capture("owned_card", "webinar:info:3")
    await capture("watch", "webinar:watch:3")
    await capture("products", "pro:products")
    await capture("product", "product:card:cute")
    for slug in PRODUCTS:
        await capture("product_" + slug, "product:card:" + slug)
    await capture("client", "section:client")
    await capture("client_faq", "info:faq")
    await capture("faq_booking", "faq:booking")
    await capture("services", "client:services")
    await capture("book_services", "client:book")
    await capture("service", "service:info:1")
    await capture("slots", "service:slots:1")
    await capture("empty_bookings", "client:bookings")
    await capture("booking_pending", "slot:request:7")
    await capture("bookings", "client:bookings")
    await fixture.click(42, "admin:confirm:9")
    result["booking_confirmed"] = fixture.messenger.messages[-2]
    await capture("bookings_confirmed", "client:bookings")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("docs/bot-preview/index.html"))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    data = {
        "scenes": asyncio.run(scenes()),
        "profile": {
            "name": copy.BOT_NAME,
            "short": copy.SHORT_DESCRIPTION,
            "description": copy.DESCRIPTION,
        },
        "commands": copy.COMMANDS,
    }
    for scene in data["scenes"].values():
        scene["buttons"] = button_rows(scene["buttons"])
        scene["entities"] = [
            entity.model_dump(exclude_none=True) for entity in message_entities(scene["text"])
        ]
    template = (root / "tests/assets/bot-preview.html").read_text()
    avatar = (root / "gl_bot/assets/avatar.svg").read_text()
    result = template.replace("<!-- AVATAR -->", avatar).replace(
        "/* PREVIEW_DATA */",
        "const DATA = " + json.dumps(data, ensure_ascii=False).replace("<", "\\u003c") + ";",
    )
    result = result.replace(
        "<!-- FAVICON -->",
        '<link rel="icon" href="data:image/svg+xml;base64,'
        + base64.b64encode(avatar.encode()).decode()
        + '">',
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(result)
    transcript = [
        "# G&L Professional — тексты экранов\n",
        "Срез из настоящего BotFlow на синтетических данных. Названия, цены, даты и состояния заказов вымышлены. Это предпросмотр диалогов, а не подключённая покупка.\n",
    ]
    for key, scene in data["scenes"].items():
        transcript.extend([f"## {key}\n", scene["text"] + "\n", "Кнопки:\n"])
        for row in scene["buttons"]:
            transcript.append("- " + " · ".join(button["text"] for button in row))
        transcript.append("")
    args.output.with_name("dialogues.md").write_text("\n".join(transcript))
    print(f"Rendered {len(data['scenes'])} synthetic dialogue screens: {args.output.resolve()}")


if __name__ == "__main__":
    main()
