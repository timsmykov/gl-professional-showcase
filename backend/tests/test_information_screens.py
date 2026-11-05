from unittest.mock import AsyncMock

import pytest
from test_flow import Fixture

from gl_bot import copy
from gl_bot.telegram import AiogramMessenger


@pytest.mark.asyncio
@pytest.mark.parametrize("section", ["client", "pro"])
async def test_information_is_scoped_and_preserves_current_section(section):
    fixture = Fixture()
    await fixture.message()
    await fixture.click(10, "section:pro")
    await fixture.click(10, "section:client")
    await fixture.click(10, f"section:{section}")
    user_before = dict(fixture.repo.users[10])
    fixture.repo.select_section = AsyncMock(side_effect=AssertionError("No profile changes"))
    await fixture.click(10, "info:about")
    message = fixture.messenger.messages[-1]
    assert message["text"] == (copy.ABOUT if section == "pro" else copy.CLIENT_MENU)
    questions = copy.FAQ_PRO if section == "pro" else copy.FAQ_CLIENT
    await fixture.message(text="/faq")
    callbacks = [
        b.get("callback_data") for row in fixture.messenger.messages[-1]["buttons"] for b in row
    ]
    assert callbacks == [*(f"faq:{key}" for key in questions), f"{section}:menu"]
    for key, (title, answer) in questions.items():
        await fixture.click(10, f"faq:{key}")
        message = fixture.messenger.messages[-1]
        assert message["text"] == f"{title}\n\n{answer}"
        assert message["buttons"][0][0]["url"] == fixture.flow.contact_url
    foreign = copy.FAQ_CLIENT if section == "pro" else copy.FAQ_PRO
    for key in [*foreign, "retired-question"]:
        await fixture.click(10, f"faq:{key}")
        assert fixture.messenger.messages[-1]["text"] == copy.FAQ_INTRO
    assert fixture.repo.users[10] == user_before
    fixture.repo.select_section.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["/about", "/faq"])
async def test_information_without_selected_audience_asks_for_section(command):
    fixture = Fixture()
    await fixture.message(text=command)
    assert fixture.messenger.messages[-1]["text"] == copy.CHOOSE_SECTION
    assert fixture.repo.users[10]["last_section"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action",
    ["pro:products", "product:card:cute", "pro:webinars", "webinar:info:3", "order:status:71"],
)
async def test_old_professional_buttons_do_not_move_client_into_professional_flow(action):
    fixture = Fixture()
    fixture.flow.product_cards = True
    await fixture.message()
    await fixture.click(10, "section:pro")
    await fixture.click(10, "section:client")
    await fixture.click(10, action)
    assert fixture.repo.users[10]["last_section"] == "client"
    assert fixture.messenger.messages[-1]["text"] == copy.CLIENT_MENU
    assert not any(
        "url" in b and "glprofessional.ru" in b["url"]
        for row in fixture.messenger.messages[-1]["buttons"]
        for b in row
    )
    await fixture.click(10, "section:pro")
    await fixture.click(10, "info:about")
    assert fixture.messenger.messages[-1]["text"] == copy.ABOUT


@pytest.mark.asyncio
@pytest.mark.parametrize("section", ["client", "pro"])
async def test_menus_link_to_information_and_contact_is_direct(section):
    fixture = Fixture()
    await fixture.message()
    await fixture.click(10, f"section:{section}")
    buttons = fixture.messenger.messages[-1]["buttons"]
    assert any(
        [b.get("callback_data") for b in row]
        == (["info:about", "info:faq"] if section == "pro" else ["info:faq"])
        for row in buttons
    )
    contact = next(b for row in buttons for b in row if b["text"] == "Написать Герману")
    assert contact == {"text": "Написать Герману", "url": fixture.flow.contact_url}
    assert "?" not in contact["url"]
    await fixture.message(text="/contact")
    assert fixture.messenger.messages[-1]["text"] == copy.CONTACT
    assert fixture.messenger.messages[-1]["buttons"][0][0] == contact


@pytest.mark.asyncio
async def test_product_links_to_its_existing_site_page_without_changing_contact():
    fixture = Fixture()
    fixture.flow.product_cards = True
    await fixture.message()
    await fixture.click(10, "section:pro")
    for slug in ["cute", "cute-ultra", "plla-activator", "nad-skin-matrix"]:
        await fixture.click(10, f"product:card:{slug}")
        buttons = fixture.messenger.messages[-1]["buttons"]
        markup = AiogramMessenger.markup(buttons)
        links = [b.url for row in markup.inline_keyboard for b in row if b.url]
        assert links == [fixture.flow.contact_url, f"{copy.SITE_URL}products/{slug}/"]
        assert [len(row) for row in markup.inline_keyboard] == [1, 1, 2]
        assert not any("wa.me" in url or "?" in url for url in links)


@pytest.mark.asyncio
async def test_faq_command_and_missing_contact_have_recovery_navigation():
    fixture = Fixture()
    await fixture.message(text="/faq")
    assert fixture.messenger.messages[-1]["text"] == copy.CHOOSE_SECTION
    fixture.flow.contact_url = None
    await fixture.message(text="/contact")
    assert fixture.messenger.messages[-1]["text"] == copy.CONTACT_MISSING
    await fixture.click(10, "contact:missing")
    assert fixture.messenger.messages[-1]["text"] == copy.CONTACT_MISSING
