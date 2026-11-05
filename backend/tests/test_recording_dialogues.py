from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from test_flow import Fixture

from gl_bot.api_repository import BackendError
from gl_bot.recordings import Checkout, OwnedRecording, Playback, Recording, format_price


class FakeRecordings:
    def __init__(self):
        self.calls = []
        self.recording = Recording(
            id=3, title="Демо-запись", description="Описание", price_kopeks=159900
        )
        self.result = Checkout(
            id=71,
            webinar_id=3,
            title="Демо-запись",
            amount_kopeks=159900,
            status="pending",
            payment_url="https://checkout.example.invalid/backend-issued-session",
            expires_at=datetime(2099, 1, 1, tzinfo=UTC),
        )
        self.access = []

    async def resolve(self, code):
        self.calls.append(("resolve", code))
        return self.recording if code == "demo_recording" else None

    async def checkout(self, telegram_id, webinar_id, source_update_id):
        self.calls.append(("checkout", telegram_id, webinar_id, source_update_id))
        return self.result

    async def order(self, telegram_id, order_id):
        self.calls.append(("order", telegram_id, order_id))
        return self.result if order_id == self.result.id else None

    async def owned(self, telegram_id):
        self.calls.append(("owned", telegram_id))
        return self.access

    async def playback(self, telegram_id, webinar_id):
        self.calls.append(("playback", telegram_id, webinar_id))
        if not any(item.webinar_id == webinar_id for item in self.access):
            return None
        return Playback(
            webinar_id=webinar_id,
            title=self.recording.title,
            url="https://video.example.invalid/backend-session",
        )


def setup():
    fixture = Fixture()
    gateway = FakeRecordings()
    fixture.flow.recordings = gateway
    return fixture, gateway


def links(fixture):
    return [
        button["url"]
        for row in fixture.messenger.messages[-1]["buttons"]
        for button in row
        if "url" in button
    ]


@pytest.mark.asyncio
async def test_public_code_opens_card_and_preserves_one_profile_on_reentry():
    fixture, gateway = setup()
    await fixture.message(text="/start demo_recording")
    assert "Демо-запись" in fixture.messenger.messages[-1]["text"]
    assert fixture.repo.users[10]["last_section"] == "pro"
    await fixture.click(10, "section:client")
    await fixture.message(text="/start demo_recording")
    await fixture.message()
    assert "Косметолог" in fixture.messenger.messages[-1]["text"]
    assert len(fixture.repo.users) == 1
    assert fixture.repo.sections == {(10, "client"), (10, "pro")}
    assert [call for call in gateway.calls if call[0] == "resolve"] == [
        ("resolve", "demo_recording"),
        ("resolve", "demo_recording"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", ["old-code", "x" * 65, "3/payment", "3 paid", "кириллица"])
async def test_stale_or_invalid_public_code_has_recovery_and_never_creates_order(payload):
    fixture, gateway = setup()
    await fixture.message(text=f"/start {payload}")
    assert "не найдена" in fixture.messenger.messages[-1]["text"]
    assert not any(call[0] == "checkout" for call in gateway.calls)
    assert len(fixture.messenger.messages[-1]["buttons"]) >= 3
    assert fixture.repo.sections == set()


@pytest.mark.asyncio
async def test_purchase_uses_update_sender_not_internal_user_or_callback_identity():
    fixture, gateway = setup()
    await fixture.message(text="/start demo_recording")
    fixture.repo.users[10]["id"] = 200
    fixture.repo.sections.add((200, "pro"))
    update_id = fixture.next_id
    await fixture.click(10, "webinar:buy:3")
    assert gateway.calls[-1] == ("checkout", 10, 3, update_id)
    assert links(fixture)[0] == gateway.result.payment_url
    assert "email для чека" in fixture.messenger.messages[-1]["text"]
    assert fixture.repo.notification is None


@pytest.mark.asyncio
async def test_duplicate_update_skipped_and_new_buy_click_can_reuse_backend_order():
    fixture, gateway = setup()
    await fixture.message(text="/start demo_recording")
    update_id = fixture.next_id
    await fixture.click(10, "webinar:buy:3")
    count = len(fixture.messenger.messages)
    fixture.next_id = update_id
    await fixture.click(10, "webinar:buy:3")
    assert len(fixture.messenger.messages) == count
    await fixture.click(10, "webinar:buy:3")
    calls = [call for call in gateway.calls if call[0] == "checkout"]
    assert calls == [("checkout", 10, 3, update_id), ("checkout", 10, 3, update_id + 1)]
    assert "Заказ №71" in fixture.messenger.messages[-1]["text"]
    assert links(fixture)[0] == gateway.result.payment_url


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["paid", "cancelled", "refunded"])
async def test_terminal_order_never_shows_payment_link_or_grants_access(status):
    fixture, gateway = setup()
    gateway.result = gateway.result.model_copy(update={"status": status})
    await fixture.message(text="/start demo_recording")
    await fixture.click(10, "order:status:71")
    assert gateway.result.payment_url not in links(fixture)
    assert gateway.access == []
    await fixture.click(10, "pro:mine")
    assert "нет доступных" in fixture.messenger.messages[-1]["text"]


@pytest.mark.asyncio
@pytest.mark.parametrize("expiry", [datetime(2020, 1, 1, tzinfo=UTC)])
async def test_expired_checkout_never_exposes_a_payment_link(expiry):
    fixture, gateway = setup()
    gateway.result = gateway.result.model_copy(update={"expires_at": expiry})
    await fixture.message(text="/start demo_recording")
    await fixture.click(10, "webinar:buy:3")
    assert gateway.result.payment_url not in links(fixture)
    buttons = str(fixture.messenger.messages[-1]["buttons"])
    assert "Проверить статус" in buttons


@pytest.mark.asyncio
async def test_owned_recordings_and_playback_come_only_from_gateway():
    fixture, gateway = setup()
    await fixture.message(text="/start demo_recording")
    await fixture.click(10, "webinar:watch:3")
    assert "недоступен" in fixture.messenger.messages[-1]["text"]
    assert all("video.example.invalid" not in url for url in links(fixture))
    gateway.access = [OwnedRecording(webinar_id=3, title="Доступная запись")]
    await fixture.click(10, "pro:mine")
    assert "webinar:watch:3" in str(fixture.messenger.messages[-1]["buttons"])
    await fixture.click(10, "webinar:watch:3")
    assert links(fixture)[0] == "https://video.example.invalid/backend-session"
    assert gateway.calls[-1] == ("playback", 10, 3)


@pytest.mark.asyncio
async def test_unknown_order_or_wrong_checkout_context_fails_safely():
    fixture, gateway = setup()
    await fixture.message(text="/start demo_recording")
    await fixture.click(10, "order:status:999")
    assert "заказ недоступен" in fixture.messenger.messages[-1]["text"]
    gateway.result = gateway.result.model_copy(update={"webinar_id": 999})
    with pytest.raises(BackendError):
        await fixture.click(10, "webinar:buy:3")
    assert "попробуйте ещё раз" in fixture.messenger.messages[-1]["text"].lower()
    assert gateway.result.payment_url not in links(fixture)


@pytest.mark.asyncio
async def test_api_failure_releases_update_and_exposes_no_payment_or_private_error():
    fixture, gateway = setup()
    await fixture.message(text="/start demo_recording")

    async def unavailable(*_args):
        raise BackendError("synthetic private checkout detail")

    gateway.checkout = unavailable
    update_id = fixture.next_id
    with pytest.raises(BackendError):
        await fixture.click(10, "webinar:buy:3")
    assert update_id not in fixture.repo.claimed
    assert "synthetic private" not in str(fixture.messenger.messages[-1])
    assert not links(fixture)
    assert fixture.messenger.messages[-1]["buttons"]


@pytest.mark.asyncio
async def test_default_legacy_flow_never_invokes_order_intent_or_invents_code_mapping():
    fixture = Fixture()
    await fixture.message(text="/start 3")
    assert "по этой ссылке сейчас недоступен" in fixture.messenger.messages[-1]["text"]
    await fixture.click(10, "section:pro")
    await fixture.click(10, "webinar:buy:3")
    assert "Покупка записи сейчас недоступна" in fixture.messenger.messages[-1]["text"]
    assert all("checkout" not in url for url in links(fixture))


@pytest.mark.parametrize(
    "url",
    [
        "http://cash.invalid",
        "javascript:alert(1)",
        "https://user:secret@cash.invalid",
        "https://cash.invalid/\n",
        "https://cash.invalid:8000/x",
        "https://cash.invalid\\evil",
    ],
)
def test_checkout_rejects_unsafe_urls(url):
    with pytest.raises(ValidationError):
        Checkout(
            id=1, webinar_id=3, title="Демо", amount_kopeks=100, status="pending", payment_url=url
        )


def test_expiry_requires_timezone():
    with pytest.raises(ValidationError):
        Checkout(
            id=1,
            webinar_id=3,
            title="Демо",
            amount_kopeks=100,
            status="pending",
            expires_at=datetime(2099, 1, 1),  # noqa: DTZ001 - test rejects naive expiry.
        )


@pytest.mark.asyncio
async def test_existing_entitlement_replaces_buy_with_watch_on_both_entries():
    fixture, gateway = setup()
    gateway.access = [OwnedRecording(webinar_id=3, title="Доступная запись")]
    await fixture.message(text="/start demo_recording")
    for action in (None, "webinar:info:3", "webinar:buy:3"):
        if action:
            await fixture.click(10, action)
        buttons = str(fixture.messenger.messages[-1]["buttons"])
        assert "webinar:watch:3" in buttons
        assert "webinar:buy:" not in buttons
        assert "уже есть доступ" in fixture.messenger.messages[-1]["text"]
    assert not any(call[0] == "checkout" for call in gateway.calls)


@pytest.mark.parametrize(
    "amount, expected", [(150000, "1 500 ₽"), (150099, "1 500.99 ₽"), (None, "Цена уточняется")]
)
def test_display_price_preserves_supplied_kopeks_in_russian_format(amount, expected):
    assert format_price(amount) == expected


@pytest.mark.asyncio
async def test_unknown_ttl_shows_backend_link_without_inventing_expiry():
    fixture, gateway = setup()
    gateway.result = gateway.result.model_copy(update={"expires_at": None})
    await fixture.message(text="/start demo_recording")
    await fixture.click(10, "webinar:buy:3")
    assert gateway.result.payment_url in links(fixture)
    assert "действует до" not in fixture.messenger.messages[-1]["text"]
    await fixture.click(10, "order:status:71")
    assert "Не оплачивайте повторно" in fixture.messenger.messages[-1]["text"]
    assert len([call for call in gateway.calls if call[0] == "checkout"]) == 1
