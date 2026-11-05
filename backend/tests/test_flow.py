from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from gl_bot import copy
from gl_bot.api_repository import BackendError
from gl_bot.flow import BotFlow, UpdateBusyError


def namespace(**values):
    return SimpleNamespace(**values)


class FakeRepository:
    def __init__(self):
        self.users = {}
        self.sections = set()
        self.claimed = set()
        self.leases = {}
        self.bookings = {}
        self.notification = None

    async def claim_update(self, update_id):
        if update_id in self.claimed:
            return "done"
        self.claimed.add(update_id)
        lease = datetime.now(UTC).isoformat()
        self.leases[update_id] = lease
        return {"status": "claimed", "lease": lease}

    async def complete_update(self, update_id, lease):
        assert self.leases[update_id] == lease
        return True

    async def release_update(self, update_id, lease):
        assert self.leases.pop(update_id) == lease
        self.claimed.discard(update_id)

    async def get_user(self, telegram_id):
        return self.users.setdefault(telegram_id, {"id": telegram_id, "last_section": None})

    async def select_section(self, user_id, section):
        self.sections.add((user_id, section))
        self.users[user_id]["last_section"] = section

    async def has_section(self, user_id, section):
        return (user_id, section) in self.sections

    async def list_services(self):
        return [{"id": 1, "title": "Услуга", "description": "Описание"}]

    async def get_service(self, service_id):
        return {"id": 1, "title": "Услуга", "description": "Описание"}

    async def list_slots(self, service_id):
        return [{"id": 7, "starts_at": datetime(2026, 10, 1, 10, tzinfo=UTC)}]

    async def create_booking(self, user_id, slot_id, update_id):
        self.bookings.setdefault(
            9,
            {
                "id": 9,
                "user_id": user_id,
                "slot_id": slot_id,
                "source_update_id": update_id,
                "status": "pending",
                "client_notified_at": None,
                "telegram_id": user_id,
                "title": "Услуга",
                "starts_at": datetime(2026, 10, 1, 10, tzinfo=UTC),
            },
        )
        return {"id": 9}

    async def get_booking(self, booking_id):
        return self.bookings.get(booking_id)

    async def decide_booking(self, booking_id, status):
        if self.bookings[booking_id]["status"] != "pending":
            return None
        self.bookings[booking_id]["status"] = status
        self.notification = {"id": 1, "booking_id": booking_id, "attempts": 0, "state": "pending"}
        return {"id": booking_id}

    async def claim_notification(self, booking_id):
        if not self.notification or self.notification["state"] != "pending":
            return None
        if booking_id and self.notification["booking_id"] != booking_id:
            return None
        self.notification["attempts"] += 1
        self.notification["state"] = "sending"
        booking = self.bookings[self.notification["booking_id"]]
        return {**booking, **self.notification}

    async def complete_notification(self, notification_id, attempts, message_id):
        if self.notification["state"] != "sending" or self.notification["attempts"] != attempts:
            return False
        self.notification["state"] = "sent"
        self.bookings[self.notification["booking_id"]]["client_notified_at"] = datetime.now(UTC)
        return True

    async def retry_notification(self, notification_id, attempts):
        self.notification["state"] = "pending"
        return True

    async def list_bookings(self, user_id):
        return [value for value in self.bookings.values() if value["user_id"] == user_id]

    async def list_products(self):
        return [{"id": 2, "title": "Препарат"}]

    async def get_product(self, product_id):
        return {"id": 2, "title": "Препарат", "description": "Информация"}

    async def list_webinars(self):
        return [{"id": 3, "title": "Вебинар", "price_kopeks": 10000}]

    async def get_webinar(self, webinar_id):
        return {"id": 3, "title": "Вебинар", "description": "Описание", "price_kopeks": 10000}

    async def list_entitlements(self, user_id):
        return [{"title": "Архив", "source": "imported"}, {"title": "Купленный", "source": "paid"}]


class FakeMessenger:
    def __init__(self):
        self.messages = []

    async def send(self, chat_id, text, buttons=None):
        self.messages.append({"chat_id": chat_id, "text": text, "buttons": buttons or []})

    async def answer(self, callback_id):
        pass


class Fixture:
    def __init__(self):
        self.repo = FakeRepository()
        self.messenger = FakeMessenger()
        self.flow = BotFlow(
            self.repo,
            self.messenger,
            german_telegram_id=42,
            contact_url="https://t.me/german_example",
        )
        self.next_id = 1

    async def message(self, user_id=10, text="/start"):
        update = namespace(
            update_id=self.next_id,
            message=namespace(
                from_user=namespace(id=user_id),
                chat=namespace(id=user_id, type="private"),
                text=text,
            ),
            callback_query=None,
        )
        self.next_id += 1
        await self.flow.handle(update)

    async def click(self, user_id, data):
        update = namespace(
            update_id=self.next_id,
            message=None,
            callback_query=namespace(
                id=str(self.next_id),
                from_user=namespace(id=user_id),
                message=namespace(chat=namespace(id=user_id, type="private")),
                data=data,
            ),
        )
        self.next_id += 1
        await self.flow.handle(update)


@pytest.mark.asyncio
@pytest.mark.parametrize("first", [True, False])
async def test_section_choices_share_one_neutral_standard_keyboard_row(first):
    from gl_bot.telegram import AiogramMessenger

    fixture = Fixture()
    await fixture.flow.choose(10, first=first)
    buttons = fixture.messenger.messages[-1]["buttons"]
    assert [[b["text"] for b in row] for row in buttons] == [["Клиент услуг", "Косметолог"]]
    markup = AiogramMessenger.markup(buttons)
    assert len(markup.inline_keyboard) == 1
    assert [b.callback_data for b in markup.inline_keyboard[0]] == [
        "section:client",
        "section:pro",
    ]
    assert all(b.style is None for b in markup.inline_keyboard[0])


@pytest.mark.asyncio
async def test_one_specialist_entry_and_both_sections_share_profile():
    fixture = Fixture()
    await fixture.message()
    buttons = [item for row in fixture.messenger.messages[-1]["buttons"] for item in row]
    assert [item["text"] for item in buttons].count("Косметолог") == 1
    await fixture.click(10, "section:pro")
    assert [item["text"] for item in fixture.messenger.messages[-1]["buttons"][0]] == [
        "Вебинары",
    ]
    assert fixture.messenger.messages[-1]["buttons"][0][0]["style"] == "primary"
    assert [item["text"] for item in fixture.messenger.messages[-1]["buttons"][1]] == [
        "Препараты",
        "Мои вебинары",
    ]
    await fixture.click(10, "section:choose")
    await fixture.click(10, "section:client")
    await fixture.message()
    assert "Клиент услуг" in fixture.messenger.messages[-1]["text"]
    assert len(fixture.repo.users) == 1
    assert fixture.repo.sections == {(10, "pro"), (10, "client")}


@pytest.mark.asyncio
async def test_booking_is_pending_until_german_confirms():
    fixture = Fixture()
    await fixture.message()
    await fixture.click(10, "section:client")
    await fixture.click(10, "client:book")
    await fixture.click(10, "service:slots:1")
    await fixture.click(10, "slot:request:7")
    assert fixture.repo.bookings[9]["status"] == "pending"
    assert "после подтверждения" in fixture.messenger.messages[-1]["text"]
    assert fixture.messenger.messages[-2]["chat_id"] == 42
    await fixture.click(11, "admin:confirm:9")
    assert fixture.repo.bookings[9]["status"] == "pending"
    await fixture.click(42, "admin:confirm:9")
    assert fixture.repo.bookings[9]["status"] == "confirmed"
    assert fixture.messenger.messages[-2]["chat_id"] == 10
    assert "подтверждена" in fixture.messenger.messages[-2]["text"]


@pytest.mark.asyncio
async def test_failed_client_send_keeps_decision_and_pending_notification(caplog):
    fixture = Fixture()
    await fixture.message()
    await fixture.click(10, "section:client")
    await fixture.click(10, "slot:request:7")
    original_send = fixture.messenger.send
    failed = False

    async def fail_once(chat_id, text, buttons=None):
        nonlocal failed
        if chat_id == 10 and not failed:
            failed = True
            raise TimeoutError("synthetic private Telegram detail")
        return await original_send(chat_id, text, buttons)

    fixture.messenger.send = fail_once
    await fixture.click(42, "admin:confirm:9")
    assert fixture.repo.bookings[9]["status"] == "confirmed"
    assert fixture.repo.bookings[9]["client_notified_at"] is None
    assert fixture.repo.notification["state"] == "pending"
    assert await fixture.flow.deliver_pending() is True
    assert fixture.repo.notification["state"] == "sent"
    assert fixture.repo.bookings[9]["client_notified_at"] is not None
    assert (
        sum(
            message["chat_id"] == 10 and "подтверждена" in message["text"]
            for message in fixture.messenger.messages
        )
        == 1
    )
    assert "synthetic private Telegram detail" not in caplog.text


@pytest.mark.asyncio
async def test_products_are_informational_and_purchase_does_not_grant_access():
    fixture = Fixture()
    await fixture.message()
    await fixture.click(10, "section:pro")
    await fixture.click(10, "product:info:2")
    assert "только лично" in fixture.messenger.messages[-1]["text"]
    assert [item["text"] for item in fixture.messenger.messages[-1]["buttons"][0]] == [
        "Написать Герману"
    ]
    await fixture.click(10, "webinar:buy:3")
    assert "Покупка записи сейчас недоступна" in fixture.messenger.messages[-1]["text"]
    await fixture.click(10, "pro:mine")
    assert "добавлен ранее" in fixture.messenger.messages[-1]["text"]
    assert "куплен" in fixture.messenger.messages[-1]["text"]


@pytest.mark.asyncio
async def test_duplicate_update_is_ignored():
    fixture = Fixture()
    await fixture.message()
    fixture.next_id = 1
    await fixture.message()
    assert len(fixture.messenger.messages) == 1


@pytest.mark.asyncio
async def test_back_buttons_return_to_current_menu_without_switching_section():
    fixture = Fixture()
    await fixture.message()
    await fixture.click(10, "section:client")
    await fixture.click(10, "client:services")
    assert fixture.messenger.messages[-1]["buttons"][-1][0]["callback_data"] == "client:menu"
    await fixture.click(10, "client:menu")
    assert "Клиент услуг" in fixture.messenger.messages[-1]["text"]

    await fixture.click(10, "section:pro")
    await fixture.click(10, "pro:webinars")
    assert fixture.messenger.messages[-1]["buttons"][-1][0]["callback_data"] == "pro:menu"
    await fixture.click(10, "pro:menu")
    assert "Косметолог" in fixture.messenger.messages[-1]["text"]

    # A button left in an older message does not change the current audience.
    await fixture.click(10, "client:menu")
    assert fixture.repo.users[10]["last_section"] == "pro"
    await fixture.message()
    assert "Косметолог" in fixture.messenger.messages[-1]["text"]


@pytest.mark.asyncio
async def test_empty_catalogs_and_missing_contact_keep_navigation():
    fixture = Fixture()
    fixture.repo.list_services = lambda: _empty_catalog()
    fixture.repo.list_webinars = lambda: _empty_catalog()
    await fixture.message()
    await fixture.click(10, "section:client")
    await fixture.click(10, "client:book")
    assert fixture.messenger.messages[-1]["buttons"][-1][0]["callback_data"] == "client:menu"
    await fixture.click(10, "section:pro")
    await fixture.click(10, "pro:webinars")
    assert fixture.messenger.messages[-1]["buttons"][-1][0]["callback_data"] == "pro:menu"
    fixture.flow.contact_url = None
    await fixture.click(10, "contact:missing")
    assert fixture.messenger.messages[-1]["buttons"][0][0]["callback_data"] == "pro:menu"


async def _empty_catalog():
    return []


@pytest.mark.asyncio
async def test_unexpected_update_claim_never_runs_user_action():
    fixture = Fixture()

    async def invalid_claim(_update_id):
        return "unexpected"

    fixture.repo.claim_update = invalid_claim
    with pytest.raises(UpdateBusyError):
        await fixture.message()
    assert fixture.messenger.messages == []
    assert fixture.repo.users == {}


@pytest.mark.asyncio
async def test_temporary_backend_failure_releases_update_for_retry():
    fixture = Fixture()
    original_get_user = fixture.repo.get_user
    attempts = 0

    async def flaky_get_user(telegram_id):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise BackendError("synthetic backend unavailable")
        return await original_get_user(telegram_id)

    fixture.repo.get_user = flaky_get_user
    with pytest.raises(BackendError):
        await fixture.message()
    assert fixture.repo.claimed == set()
    assert "попробуйте ещё раз" in fixture.messenger.messages[-1]["text"].lower()

    fixture.next_id = 1
    await fixture.message()
    assert attempts == 2
    assert "Выберите раздел" in fixture.messenger.messages[-1]["text"]


@pytest.mark.asyncio
async def test_expired_callback_answer_does_not_block_action():
    fixture = Fixture()
    await fixture.message()

    async def expired(_callback_id):
        raise RuntimeError("synthetic expired callback")

    fixture.messenger.answer = expired
    await fixture.click(10, "section:pro")
    assert (10, "pro") in fixture.repo.sections
    assert "Косметолог" in fixture.messenger.messages[-1]["text"]


@pytest.mark.asyncio
async def test_non_private_or_unattributed_updates_are_ignored_before_repository_access():
    fixture = Fixture()
    updates = [
        namespace(
            update_id=1,
            message=namespace(
                from_user=namespace(id=10), chat=namespace(id=-100, type="group"), text="/start"
            ),
            callback_query=None,
        ),
        namespace(
            update_id=2,
            message=namespace(from_user=None, chat=namespace(id=10, type="private")),
            callback_query=None,
        ),
        namespace(
            update_id=3,
            message=None,
            callback_query=namespace(from_user=namespace(id=10), message=None, data="section:pro"),
        ),
    ]
    for update in updates:
        await fixture.flow.handle(update)
    assert fixture.repo.claimed == set()
    assert fixture.repo.users == {}
    assert fixture.messenger.messages == []


@pytest.mark.asyncio
async def test_professional_cards_keep_navigation_when_catalog_changes():
    fixture = Fixture()
    await fixture.message()
    await fixture.click(10, "section:pro")
    await fixture.click(10, "pro:webinars")
    assert fixture.messenger.messages[-1]["buttons"][0][0]["callback_data"] == "webinar:info:3"

    async def unavailable(_item_id):
        return None

    fixture.repo.get_webinar = unavailable
    await fixture.click(10, "webinar:info:3")
    assert "больше недоступен" in fixture.messenger.messages[-1]["text"]
    assert fixture.messenger.messages[-1]["buttons"][0][0]["callback_data"] == "pro:menu"

    async def unpriced(_item_id):
        return {"id": 3, "title": "Запись", "description": "", "price_kopeks": None}

    fixture.repo.get_webinar = unpriced
    await fixture.click(10, "webinar:info:3")
    text = fixture.messenger.messages[-1]["text"]
    assert "Описание готовится" in text
    assert "Цена уточняется" in text
    await fixture.click(10, "webinar:buy:3")
    assert "Покупка записи сейчас недоступна" in fixture.messenger.messages[-1]["text"]

    fixture.repo.get_product = unavailable
    await fixture.click(10, "product:info:2")
    assert fixture.messenger.messages[-1]["text"] == "Информация больше недоступна."
    assert fixture.messenger.messages[-1]["buttons"][0][0]["url"] == (
        "https://t.me/german_example"
    )


@pytest.mark.asyncio
async def test_stale_professional_callback_requires_prior_section_and_valid_id():
    fixture = Fixture()
    queried = []

    async def get_webinar(item_id):
        queried.append(item_id)

    fixture.repo.get_webinar = get_webinar
    await fixture.message()
    await fixture.click(10, "webinar:info:3")
    assert "Выберите раздел" in fixture.messenger.messages[-1]["text"]
    assert queried == []

    await fixture.click(10, "section:pro")
    await fixture.click(10, "webinar:info:not-a-number")
    await fixture.click(10, "webinar:info:1000000000000000000")
    assert queried == []
    assert "Косметолог" in fixture.messenger.messages[-1]["text"]

    await fixture.click(10, "section:client")
    await fixture.click(10, "webinar:info:3")
    assert queried == []
    assert fixture.repo.users[10]["last_section"] == "client"
    assert fixture.messenger.messages[-1]["text"] == copy.CLIENT_MENU


@pytest.mark.asyncio
async def test_failed_message_send_releases_update_for_retry_without_duplicate_action():
    fixture = Fixture()
    original_send = fixture.messenger.send
    attempts = 0

    async def fail_first_send(chat_id, text, buttons=None):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TimeoutError("synthetic Telegram transport detail")
        return await original_send(chat_id, text, buttons)

    fixture.messenger.send = fail_first_send
    with pytest.raises(TimeoutError):
        await fixture.message()
    assert fixture.repo.claimed == set()
    assert "попробуйте ещё раз" in fixture.messenger.messages[-1]["text"].lower()

    fixture.next_id = 1
    await fixture.message()
    assert "Выберите раздел" in fixture.messenger.messages[-1]["text"]


@pytest.mark.asyncio
async def test_stale_update_completion_is_retryable_without_erasing_new_owner():
    fixture = Fixture()

    async def stale_complete(_update_id, _lease):
        return False

    fixture.repo.complete_update = stale_complete
    with pytest.raises(UpdateBusyError):
        await fixture.message()
    assert fixture.repo.claimed == {1}
    assert len(fixture.messenger.messages) == 1
    assert "попробуйте ещё раз" not in fixture.messenger.messages[-1]["text"]
