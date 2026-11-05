"""Presentation only: keep callback identities and backend actions intact."""

_LABELS = {"Назад в меню": "← Меню", "Каталог вебинаров": "Все вебинары"}


def _group(button: dict[str, str]) -> str:
    if button.get("style"):
        return "accent"
    data = button.get("callback_data", "")
    if data.startswith(("product:card:", "product:info:", "webinar:info:", "service:", "slot:")):
        return data.rsplit(":", 1)[0]
    if data.endswith(":menu") or data in {"pro:products", "pro:webinars"}:
        return "navigation"
    return "action"


def compact_rows(buttons: list[list[dict[str, str]]] | None) -> list[list[dict[str, str]]]:
    """Pair short adjacent peers; never pair a prominent or destructive action."""
    rows: list[list[dict[str, str]]] = []
    for original in buttons or []:
        row = [
            dict(button, text=_LABELS.get(button["text"], button["text"])) for button in original
        ]
        if not row:
            continue
        if (
            len(row) == 1
            and rows
            and len(rows[-1]) == 1
            and _group(row[0]) != "accent"
            and _group(row[0]) == _group(rows[-1][0])
            and len(row[0]["text"]) + len(rows[-1][0]["text"]) <= 32
        ):
            rows[-1].extend(row)
        else:
            rows.append(row)
    return rows


def is_navigation(button: dict[str, str]) -> bool:
    """The same callback can be a menu choice or a return link."""
    data, label = button.get("callback_data", ""), button["text"]
    if label == "Подробнее на сайте" and button.get("url", "").startswith(
        "https://glprofessional.ru/products/"
    ):
        return True
    if data.endswith(":menu") and label in {"Назад в меню", "← Меню"}:
        return True
    return (data, label) in {
        ("pro:products", "Все препараты"),
        ("pro:webinars", "Каталог вебинаров"),
        ("pro:webinars", "Все вебинары"),
        ("section:choose", "Сменить раздел"),
        ("section:choose", "Открыть меню"),
    }


def button_rows(buttons: list[list[dict[str, str]]] | None) -> list[list[dict[str, str]]]:
    """Shared standard inline keyboard presentation."""
    content, navigation = [], []
    product_card = any(
        b.get("callback_data") == "pro:products" and b["text"] == "Все препараты"
        for row in buttons or []
        for b in row
    )
    for row in buttons or []:
        for target, nav in [(content, False), (navigation, True)]:
            selected = [dict(b) for b in row if is_navigation(b) == nav]
            if selected:
                target.append(selected)
    rows = compact_rows(content) + compact_rows(navigation)
    icons = {
        "pro:webinars": "▶",
        "pro:products": "◇",
        "pro:mine": "▤",
        "client:book": "+",
        "client:services": "◇",
        "client:bookings": "◷",
        "pro:recover": "↻",
    }
    for row in rows:
        for button in row:
            data = button.get("callback_data", "")
            if is_navigation(button):
                button["text"] = {
                    "← Меню": "⌂ Меню",
                    "Все препараты": "← Все препараты",
                    "Все вебинары": "← Все вебинары",
                    "Сменить раздел": "⇄ Разделы",
                    "Открыть меню": "⌂ Меню",
                }.get(button["text"], button["text"])
            elif button["text"] == "Написать Герману":
                button["text"] = "Написать Герману ↗"
                if product_card and not button.get("style"):
                    button["style"] = "primary"
            elif data in icons:
                button["text"] = f"{icons[data]} {button['text']}"
            elif data.startswith(("webinar:buy:", "webinar:watch:")):
                button["text"] += " →"
            elif button.get("style") == "primary" and button.get("url"):
                button["text"] += " ↗"
    return rows
