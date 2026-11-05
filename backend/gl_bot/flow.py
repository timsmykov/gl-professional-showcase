from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from aiogram.types import Update

from . import copy
from .api_repository import BackendError
from .products import PRODUCTS
from .recordings import Checkout, RecordingGateway, format_price
from .telegram import Keyboard, Messenger, callback_button, url_button

logger = logging.getLogger(__name__)


def _valid_id(value: str | None) -> int | None:
    if value and re.fullmatch(r"\d{1,18}", value):
        return int(value) or None
    return None


class UpdateBusyError(Exception):
    """An update is still in flight; Telegram should retry it."""


class BotFlow:
    def __init__(
        self,
        repo: Any,
        messenger: Messenger,
        *,
        german_telegram_id: int | None = None,
        contact_url: str | None = None,
        time_zone: str = "Europe/Moscow",
        recordings: RecordingGateway | None = None,
        product_cards: bool = False,
    ) -> None:
        self.repo = repo
        self.messenger = messenger
        self.german_telegram_id = german_telegram_id
        self.contact_url = contact_url
        self.time_zone = ZoneInfo(time_zone)
        self.recordings = recordings
        self.product_cards = product_cards

    @property
    def contact_button(self) -> dict[str, str]:
        if self.contact_url:
            return url_button("Написать Герману", self.contact_url)
        return callback_button("Написать Герману", "contact:missing")

    @property
    def switch_button(self) -> list[dict[str, str]]:
        return [callback_button("Сменить раздел", "section:choose")]

    @staticmethod
    def back_button(section: str) -> list[dict[str, str]]:
        return [callback_button("Назад в меню", f"{section}:menu")]

    def format_time(self, value: datetime) -> str:
        return value.astimezone(self.time_zone).strftime("%d.%m.%Y %H:%M")

    @property
    def time_label(self) -> str:
        return "время московское" if self.time_zone.key == "Europe/Moscow" else self.time_zone.key

    async def choose(self, chat_id: int, *, first: bool = False) -> None:
        await self.messenger.send(
            chat_id,
            copy.WELCOME if first else copy.CHOOSE_SECTION,
            [
                [
                    callback_button("Клиент услуг", "section:client"),
                    callback_button("Косметолог", "section:pro"),
                ],
            ],
        )

    def information_buttons(self, section: str) -> list[dict[str, str]]:
        questions = callback_button("Вопросы", "info:faq")
        return (
            [callback_button("О G&L", "info:about"), questions]
            if section == "pro"
            else [questions]
        )

    @staticmethod
    def information_back(user: Any) -> list[dict[str, str]]:
        section = user["last_section"]
        return (
            BotFlow.back_button(section)
            if section
            else [callback_button("Разделы", "section:choose")]
        )

    async def information(self, chat_id: int, user: Any, action: str) -> None:
        section = user["last_section"]
        if section not in ("client", "pro"):
            return await self.choose(chat_id)
        back = self.information_back(user)
        if action == "info:about" and section == "client":
            return await self.menu(chat_id, "client")
        questions = copy.FAQ_PRO if section == "pro" else copy.FAQ_CLIENT
        if action == "info:about":
            return await self.messenger.send(
                chat_id,
                copy.ABOUT,
                [
                    [url_button("Подробнее о G&L на сайте", copy.ABOUT_URL)],
                    [callback_button("Вопросы", "info:faq")],
                    back,
                ],
            )
        question = (
            questions.get(action.removeprefix("faq:")) if action.startswith("faq:") else None
        )
        if question:
            title, answer = question
            return await self.messenger.send(
                chat_id,
                f"{title}\n\n{answer}",
                [[self.contact_button], [callback_button("Все вопросы", "info:faq")], back],
            )
        return await self.messenger.send(
            chat_id,
            copy.FAQ_INTRO,
            [[callback_button(title, f"faq:{key}")] for key, (title, _answer) in questions.items()]
            + [back],
        )

    async def menu(self, chat_id: int, section: str, *, lead: str = "") -> None:
        if section == "client":
            await self.messenger.send(
                chat_id,
                f"{lead}\n\n{copy.CLIENT_MENU}" if lead else copy.CLIENT_MENU,
                [
                    [callback_button("Записаться", "client:book", style="primary")],
                    [
                        callback_button("Услуги", "client:services"),
                        callback_button("Мои записи", "client:bookings"),
                    ],
                    [self.contact_button],
                    self.information_buttons(section),
                    self.switch_button,
                ],
            )
        else:
            await self.messenger.send(
                chat_id,
                f"{lead}\n\n{copy.PRO_MENU}" if lead else copy.PRO_MENU,
                [
                    [callback_button("Вебинары", "pro:webinars", style="primary")],
                    [
                        callback_button("Препараты", "pro:products"),
                        callback_button("Мои вебинары", "pro:mine"),
                    ],
                    [self.contact_button],
                    self.information_buttons(section),
                    self.switch_button,
                ],
            )

    async def menu_or_choose(self, chat_id: int, user: Any, *, lead: str = "") -> None:
        if user["last_section"]:
            await self.menu(chat_id, user["last_section"], lead=lead)
        else:
            await self.choose(chat_id, first=True)

    async def require_section(self, chat_id: int, user: Any, section: str) -> bool:
        if await self.repo.has_section(user["id"], section):
            if user["last_section"] != section:
                await self.repo.select_section(user["id"], section)
            return True
        await self.choose(chat_id)
        return False

    async def list_services(self, chat_id: int, for_booking: bool) -> None:
        services = await self.repo.list_services()
        if not services:
            await self.messenger.send(
                chat_id,
                "Услуги и расписание пока не опубликованы. По вопросам записи можно написать Герману.",
                [[self.contact_button], self.back_button("client")],
            )
            return
        prefix = "service:slots" if for_booking else "service:info"
        buttons = [
            [callback_button(service["title"], f"{prefix}:{service['id']}")]
            for service in services
        ]
        buttons.append(self.back_button("client"))
        await self.messenger.send(
            chat_id,
            "Запись на услугу\n\nВыберите услугу, затем удобное время. Заявку подтвердит Герман."
            if for_booking
            else "Услуги\n\nВыберите услугу, чтобы узнать подробности или отправить заявку на запись.",
            buttons,
        )

    async def client_action(self, chat_id: int, user: Any, action: str, update_id: int) -> None:
        if action == "client:menu":
            return await self.menu(chat_id, "client")
        if action == "client:services":
            return await self.list_services(chat_id, False)
        if action == "client:book":
            return await self.list_services(chat_id, True)
        if action == "client:bookings":
            bookings = await self.repo.list_bookings(user["id"])
            statuses = {
                "pending": "ожидает подтверждения",
                "confirmed": "подтверждена",
                "declined": "отклонена",
            }
            details = "\n\n".join(
                f"Заявка №{item['id']} · {item['title']}\n"
                f"{self.format_time(item['starts_at'])} · {self.time_label}\n"
                f"Статус: {statuses[item['status']]}"
                for item in bookings
            )
            text = "Мои записи\n\n" + (
                details or "Вы ещё не отправляли заявку. Выберите услугу и удобное время."
            )
            return await self.messenger.send(
                chat_id,
                text,
                [
                    [callback_button("Записаться", "client:book")],
                    [self.contact_button],
                    self.back_button("client"),
                ],
            )

        parts = action.split(":")
        if len(parts) != 3 or (item_id := _valid_id(parts[2])) is None:
            return await self.menu(chat_id, "client")
        kind, operation, _ = parts
        if kind == "service":
            service = await self.repo.get_service(item_id)
            if not service:
                return await self.messenger.send(
                    chat_id,
                    "Услуга больше недоступна.",
                    [[callback_button("Услуги", "client:services")], self.back_button("client")],
                )
            if operation == "info":
                return await self.messenger.send(
                    chat_id,
                    f"{service['title']}\n\n"
                    f"{service['description'] or 'Подробности услуги можно уточнить у Германа.'}",
                    [
                        [
                            callback_button(
                                "Записаться", f"service:slots:{item_id}", style="primary"
                            )
                        ],
                        [self.contact_button],
                        self.back_button("client"),
                    ],
                )
            if operation == "slots":
                if not self.german_telegram_id:
                    return await self.messenger.send(
                        chat_id,
                        "Онлайн-запись пока не подключена. Можно написать Герману.",
                        [[self.contact_button], self.back_button("client")],
                    )
                slots = await self.repo.list_slots(item_id)
                buttons: Keyboard = (
                    [
                        [
                            callback_button(
                                self.format_time(slot["starts_at"]), f"slot:request:{slot['id']}"
                            )
                        ]
                        for slot in slots
                    ]
                    if slots
                    else [[self.contact_button]]
                )
                buttons.append(self.back_button("client"))
                text = (
                    f"{service['title']}\n\nВыберите удобное время ({self.time_label}). "
                    "Вы отправите заявку; запись состоится после подтверждения Германом."
                    if slots
                    else "Свободного времени для записи пока нет. Можно обсудить другое время с Германом."
                )
                return await self.messenger.send(chat_id, text, buttons)

        if kind == "slot" and operation == "request":
            if not self.german_telegram_id:
                return await self.messenger.send(
                    chat_id,
                    "Онлайн-запись пока не подключена.",
                    [[self.contact_button], self.back_button("client")],
                )
            created = await self.repo.create_booking(user["id"], item_id, update_id)
            if not created:
                return await self.messenger.send(
                    chat_id,
                    "Это время уже занято или недоступно. Выберите другое время или напишите Герману.",
                    [[callback_button("Записаться", "client:book")], self.back_button("client")],
                )
            booking = await self.repo.get_booking(created["id"])
            await self.messenger.send(
                self.german_telegram_id,
                f"Новая заявка №{booking['id']}: {booking['title']}, "
                f"{self.format_time(booking['starts_at'])}. Подтвердите или отклоните.",
                [
                    [
                        callback_button(
                            "Подтвердить", f"admin:confirm:{booking['id']}", style="success"
                        ),
                        callback_button(
                            "Отклонить", f"admin:decline:{booking['id']}", style="danger"
                        ),
                    ]
                ],
            )
            return await self.messenger.send(
                chat_id,
                f"Заявка №{booking['id']} отправлена.\n\n"
                f"{booking['title']}\n{self.format_time(booking['starts_at'])} · {self.time_label}\n\n"
                "Запись состоится после подтверждения Германом. "
                "Статус можно посмотреть в «Мои записи».",
                [
                    [callback_button("Мои записи", "client:bookings")],
                    [self.contact_button],
                    self.back_button("client"),
                ],
            )
        return await self.menu(chat_id, "client")

    async def pro_action(
        self, chat_id: int, user: Any, action: str, telegram_id: int, update_id: int
    ) -> None:
        if action == "pro:menu":
            return await self.menu(chat_id, "pro")
        if action == "pro:webinars":
            webinars = await self.repo.list_webinars()
            buttons = (
                [
                    [callback_button(item["title"], f"webinar:info:{item['id']}")]
                    for item in webinars
                ]
                if webinars
                else []
            )
            buttons.extend(
                [[callback_button("Мои вебинары", "pro:mine")], self.back_button("pro")]
            )
            return await self.messenger.send(
                chat_id,
                copy.RECORDING_CATALOG if webinars else copy.EMPTY_RECORDINGS,
                buttons,
            )
        if action == "pro:products":
            if self.product_cards:
                return await self.messenger.send(
                    chat_id,
                    "Препараты G&L\n\nВыберите препарат: фото, описание и состав можно посмотреть здесь. "
                    "По вопросам препарата напишите Герману лично.",
                    [
                        [callback_button(card.title, f"product:card:{card.slug}")]
                        for card in PRODUCTS.values()
                    ]
                    + [[self.contact_button], self.back_button("pro")],
                )
            products = await self.repo.list_products()
            buttons = [
                [callback_button(item["title"], f"product:info:{item['id']}")] for item in products
            ] + [[self.contact_button], self.back_button("pro")]
            return await self.messenger.send(
                chat_id,
                "Препараты G&L\n\nВыберите препарат, чтобы прочитать описание. "
                "По вопросам приобретения напишите Герману лично."
                if products
                else "Информация о препаратах пока не опубликована. По вопросам можно написать Герману.",
                buttons,
            )
        if action == "pro:mine":
            if self.recordings:
                owned = await self.recordings.owned(telegram_id)
                buttons = [
                    [callback_button(item.title, f"webinar:watch:{item.webinar_id}")]
                    for item in owned
                ]
                buttons.extend(self.owned_navigation())
                return await self.messenger.send(
                    chat_id,
                    "Мои вебинары\n\nВыберите запись для просмотра. "
                    "К доступным записям можно возвращаться в этом разделе."
                    if owned
                    else copy.EMPTY_OWNED,
                    buttons,
                )
            entitlements = await self.repo.list_entitlements(user["id"])
            text = (
                "\n".join(
                    f"{item['title']} — {'куплен' if item['source'] == 'paid' else 'добавлен ранее'}"
                    for item in entitlements
                )
                or copy.EMPTY_OWNED
            )
            if entitlements:
                text = "Мои вебинары\n\n" + text + "\n\nПросмотр записей пока недоступен."
            return await self.messenger.send(chat_id, text, self.owned_navigation())
        if action == "pro:recover":
            return await self.messenger.send(
                chat_id,
                copy.RECOVER_ACCESS,
                [
                    [self.contact_button],
                    [callback_button("Мои вебинары", "pro:mine")],
                    self.back_button("pro"),
                ],
            )

        if action.startswith("product:card:") and self.product_cards:
            card = PRODUCTS.get(action.removeprefix("product:card:"))
            buttons = [
                [self.contact_button],
                *(
                    [[url_button("Подробнее на сайте", f"{copy.SITE_URL}products/{card.slug}/")]]
                    if card
                    else []
                ),
                [callback_button("Все препараты", "pro:products")],
                self.back_button("pro"),
            ]
            if card is None:
                return await self.messenger.send(chat_id, "Карточка больше недоступна.", buttons)
            sender = getattr(self.messenger, "send_product", None)
            if sender:
                return await sender(chat_id, card, buttons)
            return await self.messenger.send(chat_id, card.text, buttons)

        parts = action.split(":")
        if len(parts) != 3 or (item_id := _valid_id(parts[2])) is None:
            return await self.menu(chat_id, "pro")
        kind, operation, _ = parts
        if kind == "product" and operation == "info":
            product = await self.repo.get_product(item_id)
            text = (
                f"{product['title']}\n\n{product['description'] or 'Подробности уточняйте у Германа.'}"
                "\n\nПрепарат можно обсудить только лично с Германом."
                if product
                else "Информация больше недоступна."
            )
            return await self.messenger.send(
                chat_id, text, [[self.contact_button], self.back_button("pro")]
            )
        if kind == "webinar" and operation == "info":
            webinar = await self.repo.get_webinar(item_id)
            if not webinar:
                return await self.messenger.send(
                    chat_id, "Вебинар больше недоступен.", [self.back_button("pro")]
                )
            owned = bool(self.recordings) and any(
                item.webinar_id == item_id for item in await self.recordings.owned(telegram_id)
            )
            return await self.messenger.send(
                chat_id,
                self.recording_text(webinar, owned=owned),
                self.recording_buttons(item_id, webinar["price_kopeks"], owned=owned),
            )
        if kind == "webinar" and operation == "buy":
            if self.recordings:
                existing = next(
                    (
                        item
                        for item in await self.recordings.owned(telegram_id)
                        if item.webinar_id == item_id
                    ),
                    None,
                )
                if existing:
                    return await self.messenger.send(
                        chat_id,
                        f"{existing.title}\n\nУ вас уже есть доступ к этой записи. Нажмите «Смотреть запись».",
                        self.recording_buttons(item_id, None, owned=True),
                    )
                checkout = await self.recordings.checkout(telegram_id, item_id, update_id)
                if checkout.webinar_id != item_id:
                    raise BackendError("invalid checkout context")
                return await self.show_checkout(chat_id, checkout)
            return await self.messenger.send(
                chat_id,
                copy.PURCHASE_UNAVAILABLE,
                [
                    [self.contact_button],
                    [callback_button("Мои вебинары", "pro:mine")],
                    self.back_button("pro"),
                ],
            )
        if kind == "order" and operation == "status":
            checkout = (
                await self.recordings.order(telegram_id, item_id) if self.recordings else None
            )
            if checkout is not None:
                if checkout.id != item_id:
                    raise BackendError("invalid order context")
                return await self.show_checkout(chat_id, checkout, checked=True)
            return await self.messenger.send(
                chat_id,
                "Этот заказ недоступен. Проверьте «Мои вебинары» или напишите Герману.",
                self.owned_navigation(),
            )
        if kind == "webinar" and operation == "watch":
            playback = (
                await self.recordings.playback(telegram_id, item_id) if self.recordings else None
            )
            if playback is not None:
                if playback.webinar_id != item_id:
                    raise BackendError("invalid playback context")
                return await self.messenger.send(
                    chat_id,
                    f"{playback.title}\n\nНажмите «Смотреть запись». "
                    "Она откроется на странице видеосервиса.",
                    [
                        [url_button("Смотреть запись", playback.url, style="primary")],
                        *self.owned_navigation(),
                    ],
                )
            return await self.messenger.send(
                chat_id, copy.PLAYBACK_UNAVAILABLE, self.owned_navigation()
            )
        return await self.menu(chat_id, "pro")

    def owned_navigation(self) -> Keyboard:
        return [
            [callback_button("Каталог вебинаров", "pro:webinars")],
            [callback_button("Восстановить доступ", "pro:recover")],
            [self.contact_button],
            self.back_button("pro"),
        ]

    @staticmethod
    def recording_text(webinar: Any, *, owned: bool = False) -> str:
        return (
            f"{webinar['title']}\n\n"
            f"{webinar['description'] or 'Описание готовится. Подробности можно уточнить у Германа.'}\n\n"
            f"{format_price(webinar['price_kopeks'])}\n"
            "Формат: запись вебинара.\n"
            + (
                "У вас уже есть доступ к этой записи. Нажмите «Смотреть запись»."
                if owned
                else "После подтверждённой оплаты доступ к записи бессрочный."
            )
        )

    def recording_buttons(
        self, webinar_id: int, price: int | None, *, owned: bool = False
    ) -> Keyboard:
        buttons = []
        if owned:
            buttons.append(
                [
                    callback_button(
                        "Смотреть запись", f"webinar:watch:{webinar_id}", style="primary"
                    )
                ]
            )
        elif price is not None and price > 0:
            buttons.append(
                [callback_button("Купить запись", f"webinar:buy:{webinar_id}", style="primary")]
            )
        else:
            buttons.append([self.contact_button])
        buttons.extend(
            [
                [callback_button("Мои вебинары", "pro:mine")],
                [callback_button("Каталог вебинаров", "pro:webinars")],
                self.back_button("pro"),
            ]
        )
        return buttons

    async def show_checkout(self, chat_id: int, order: Checkout, *, checked: bool = False) -> None:
        heading = f"{order.title}\nЗаказ №{order.id} · {format_price(order.amount_kopeks)}\n\n"
        buttons: Keyboard = []
        if order.status == "paid":
            text = (
                "Оплата подтверждена.\n\n"
                "Откройте «Мои вебинары», чтобы проверить доступ к записи. "
                "Если запись не появилась, нажмите «Восстановить доступ»."
            )
        elif order.status == "refunded":
            text = (
                "По этому заказу оформлен возврат. "
                "Актуальные доступные записи — в «Мои вебинары». "
                "По вопросам возврата можно написать Герману."
            )
        elif order.status == "cancelled":
            text = "Этот заказ отменён. Если хотите купить запись, начните новый заказ."
            buttons.append(
                [callback_button("Вернуться к записи", f"webinar:info:{order.webinar_id}")]
            )
        elif order.payment_url and (
            order.expires_at is None or order.expires_at > datetime.now(UTC)
        ):
            text = (
                "Ссылка для оплаты готова.\n\n"
                "Нажмите «Перейти к оплате». Откроется внешняя касса; "
                "email для чека укажите на её странице.\n\n"
                "После оплаты вернитесь сюда и нажмите «Проверить статус». "
                "Запись появится в «Мои вебинары» после подтверждения оплаты.\n\n"
                + (
                    f"Ссылка действует до {self.format_time(order.expires_at)} ({self.time_label})."
                    if order.expires_at is not None
                    else ""
                )
            )
            buttons.append([url_button("Перейти к оплате", order.payment_url, style="primary")])
            if checked:
                text = (
                    "Оплата пока не подтверждена.\n\n"
                    "Если вы уже оплатили, подождите подтверждения и проверьте статус ещё раз. "
                    "Не оплачивайте повторно, пока статус не уточнён.\n\n"
                    "Если не оплачивали, можно перейти на внешнюю кассу по действующей ссылке."
                )
        elif order.payment_url and order.expires_at:
            text = (
                "Срок действия ссылки оплаты истёк.\n\n"
                "Если вы уже оплатили, сначала проверьте статус заказа. "
                "Если не оплачивали, запросите новую ссылку."
            )
            buttons.append(
                [callback_button("Запросить ссылку", f"webinar:buy:{order.webinar_id}")]
            )
        else:
            text = (
                "Заказ ожидает оплаты. Ссылка для оплаты пока недоступна.\n\n"
                "Если вы уже оплатили, подождите подтверждения и проверьте статус ещё раз. "
                "Не оплачивайте повторно, пока статус не уточнён. "
                "Если нужна помощь, напишите Герману."
            )
        if order.status == "pending":
            buttons.append([callback_button("Проверить статус", f"order:status:{order.id}")])
        buttons.extend(
            [
                [callback_button("Мои вебинары", "pro:mine")],
                [callback_button("Восстановить доступ", "pro:recover")],
                [self.contact_button],
                self.back_button("pro"),
            ]
        )
        await self.messenger.send(chat_id, heading + text, buttons)

    async def start_recording(
        self, chat_id: int, user: Any, payload: str, telegram_id: int
    ) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", payload):
            return await self.messenger.send(
                chat_id, copy.INVALID_RECORDING, self.owned_navigation()
            )
        if not self.recordings:
            return await self.messenger.send(
                chat_id, copy.RECORDING_LINK_UNAVAILABLE, self.owned_navigation()
            )
        recording = await self.recordings.resolve(payload)
        if recording is None:
            return await self.messenger.send(
                chat_id, copy.INVALID_RECORDING, self.owned_navigation()
            )
        # A public code is context, not proof of a purchase or entitlement.
        await self.repo.select_section(user["id"], "pro")
        owned = any(
            item.webinar_id == recording.id for item in await self.recordings.owned(telegram_id)
        )
        return await self.messenger.send(
            chat_id,
            self.recording_text(recording.model_dump(), owned=owned),
            self.recording_buttons(recording.id, recording.price_kopeks, owned=owned),
        )

    async def user_action(
        self, chat_id: int, user: Any, action: str, update_id: int, telegram_id: int
    ) -> None:
        if action.startswith(("info:", "faq:")):
            return await self.information(chat_id, user, action)
        if action == "section:choose":
            return await self.choose(chat_id)
        if action in ("section:client", "section:pro"):
            section = action.split(":")[1]
            await self.repo.select_section(user["id"], section)
            return await self.menu(chat_id, section)
        if action == "contact:missing":
            return await self.messenger.send(
                chat_id,
                copy.CONTACT_MISSING,
                [self.back_button(user["last_section"])] if user["last_section"] else None,
            )
        # Old keyboards must not move a dual-role user into another audience.
        target = (
            "client"
            if action.startswith(("client:", "service:", "slot:"))
            else "pro"
            if action.startswith(("pro:", "webinar:", "product:", "order:"))
            else None
        )
        if target and user["last_section"] and target != user["last_section"]:
            return await self.menu_or_choose(chat_id, user)
        if action.startswith(("client:", "service:", "slot:")):
            if await self.require_section(chat_id, user, "client"):
                await self.client_action(chat_id, user, action, update_id)
            return
        if action.startswith(("pro:", "webinar:", "product:", "order:")):
            if await self.require_section(chat_id, user, "pro"):
                await self.pro_action(chat_id, user, action, telegram_id, update_id)
            return
        await self.menu_or_choose(chat_id, user)

    async def admin_action(self, chat_id: int, telegram_id: int, action: str) -> None:
        if not self.german_telegram_id or telegram_id != self.german_telegram_id:
            return await self.messenger.send(chat_id, "Действие недоступно.")
        parts = action.split(":")
        if len(parts) != 3 or parts[1] not in ("confirm", "decline"):
            return
        booking_id = _valid_id(parts[2])
        if booking_id is None:
            return
        booking = await self.repo.get_booking(booking_id)
        if not booking:
            return await self.messenger.send(chat_id, "Заявка не найдена.")
        status = "confirmed" if parts[1] == "confirm" else "declined"
        if booking["status"] == "pending":
            if not await self.repo.decide_booking(booking_id, status):
                return await self.messenger.send(chat_id, "Заявка уже обработана.")
        elif booking["status"] != status:
            return await self.messenger.send(chat_id, "Заявка уже обработана.")
        await self.deliver_pending(booking_id)
        return await self.messenger.send(
            chat_id,
            f"Заявка №{booking_id}: {'подтверждена' if status == 'confirmed' else 'отклонена'}.",
        )

    async def deliver_pending(self, booking_id: int = 0) -> bool:
        notification = await self.repo.claim_notification(booking_id)
        if notification is None:
            return False
        status = notification["status"]
        text = (
            f"Запись №{booking_id or notification['booking_id']} подтверждена.\n\n"
            f"{notification['title']}\n{self.format_time(notification['starts_at'])} · {self.time_label}\n\n"
            "По вопросам записи или изменения времени можно написать Герману."
            if status == "confirmed"
            else f"Заявка №{booking_id or notification['booking_id']} отклонена. "
            "Вы можете выбрать другое время или написать Герману."
        )
        try:
            message_id = await self.messenger.send(
                notification["telegram_id"],
                text,
                [[callback_button("Мои записи", "client:bookings")], [self.contact_button]],
            )
        except Exception as error:  # noqa: BLE001 - keep durable retry and omit Telegram details.
            await self.repo.retry_notification(notification["id"], notification["attempts"])
            logger.warning("booking_notification_send_failed type=%s", type(error).__name__)
            if notification["attempts"] >= 5:
                logger.warning("booking_notification_needs_review")
            return False
        completed = await self.repo.complete_notification(
            notification["id"], notification["attempts"], message_id or 0
        )
        if not completed:
            logger.warning("booking_notification_claim_expired")
        return completed

    async def handle(self, update: Update) -> None:
        message = update.message
        callback = update.callback_query
        sender = message.from_user if message else callback.from_user if callback else None
        chat = (
            message.chat
            if message
            else getattr(callback.message, "chat", None)
            if callback
            else None
        )
        if not sender or not chat or chat.type != "private":
            return

        claim = await self.repo.claim_update(update.update_id)
        if claim == "done":
            return
        if (
            not isinstance(claim, dict)
            or claim.get("status") != "claimed"
            or not isinstance(claim.get("lease"), str)
        ):
            raise UpdateBusyError()
        lease = claim["lease"]

        try:
            if callback:
                try:
                    await self.messenger.answer(callback.id)
                except Exception:  # noqa: BLE001 - callback answers can expire independently.
                    logger.warning("callback_answer_failed")
            action = callback.data if callback else None
            if action and action.startswith("admin:"):
                await self.admin_action(chat.id, sender.id, action)
            else:
                user = await self.repo.get_user(sender.id)
                if message:
                    parts = (message.text or "").split(maxsplit=1)
                    command = parts[0].split("@", 1)[0] if parts else ""
                    if command == "/start" and len(parts) == 2:
                        await self.start_recording(chat.id, user, parts[1], sender.id)
                    elif command == "/switch":
                        await self.choose(chat.id)
                    elif command == "/about":
                        await self.information(chat.id, user, "info:about")
                    elif command == "/faq":
                        await self.information(chat.id, user, "info:faq")
                    elif command in {"/contact", "/help"}:
                        await self.messenger.send(
                            chat.id,
                            copy.CONTACT if self.contact_url else copy.CONTACT_MISSING,
                            [[self.contact_button], self.information_back(user)],
                        )
                    else:
                        await self.menu_or_choose(
                            chat.id,
                            user,
                            lead=copy.FREE_TEXT if command not in {"/start", "/menu"} else "",
                        )
                elif action:
                    await self.user_action(chat.id, user, action, update.update_id, sender.id)
            if not await self.repo.complete_update(update.update_id, lease):
                raise UpdateBusyError()
        except UpdateBusyError:
            # Another worker now owns the update. Do not acknowledge or delete it.
            raise
        except Exception:
            await self.repo.release_update(update.update_id, lease)
            try:
                await self.messenger.send(
                    chat.id,
                    copy.SERVICE_ERROR,
                    [[callback_button("Открыть меню", "section:choose")]],
                )
            except Exception:  # noqa: BLE001 - preserve the original action error.
                logger.warning("bot_failure_notice_failed")
            raise
