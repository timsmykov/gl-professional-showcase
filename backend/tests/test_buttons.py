from copy import deepcopy

import pytest

from gl_bot.buttons import button_rows, compact_rows
from gl_bot.products import PRODUCTS
from gl_bot.telegram import AiogramMessenger, callback_button, url_button


def test_catalog_pairs_without_mutating_actions():
    buttons = [[callback_button(c.title, f"product:card:{c.slug}")] for c in PRODUCTS.values()]
    buttons += [[url_button("Написать Герману", "https://example.test")]]
    original = deepcopy(buttons)
    rows = compact_rows(buttons)
    assert [len(row) for row in rows] == [2, 2, 1]
    assert [b for row in rows for b in row] == [b for row in buttons for b in row]
    assert buttons == original


def test_long_labels_and_prominent_actions_stay_separate():
    buttons = [
        [callback_button("Очень длинное название медицинской процедуры", "service:1")],
        [callback_button("Услуга", "service:2")],
        [callback_button("Купить", "webinar:buy:1", style="primary")],
        [callback_button("Отменить", "cancel", style="danger")],
    ]
    assert compact_rows(buttons) == buttons


def test_card_standard_navigation_and_primary_contact_preserve_targets():
    buttons = [
        [url_button("Написать Герману", "https://example.test")],
        [callback_button("Все препараты", "pro:products")],
        [callback_button("Назад в меню", "pro:menu")],
    ]
    original = deepcopy(buttons)
    markup = AiogramMessenger.markup(buttons)
    assert [len(row) for row in markup.inline_keyboard] == [1, 2]
    assert markup.inline_keyboard[0][0].style == "primary"
    assert markup.inline_keyboard[0][0].url == "https://example.test"
    assert [b.callback_data for b in markup.inline_keyboard[1]] == ["pro:products", "pro:menu"]
    assert all(b.style != "link" for row in markup.inline_keyboard for b in row)
    assert buttons == original


def test_external_title_remains_literal_and_cannot_impersonate_navigation():
    label = "<b>Все препараты</b> & 🌿"
    markup = AiogramMessenger.markup([[callback_button(label, "webinar:info:1")]])
    assert markup.inline_keyboard[0][0].text == label
    assert markup.inline_keyboard[0][0].style is None


@pytest.mark.parametrize("buttons", [None, [], [[]]])
def test_empty_layout(buttons):
    assert button_rows(buttons) == []
    assert compact_rows(buttons) == []
    assert AiogramMessenger.markup(buttons) is None
