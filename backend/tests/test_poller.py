import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from gl_bot.poller import (
    PreviewMessenger,
    allowed_private_update,
    message_label_from_env,
    read_allowed_user_id,
    run,
)


def test_preview_accepts_only_allowed_private_updates():
    allowed = SimpleNamespace(
        message=SimpleNamespace(
            from_user=SimpleNamespace(id=10), chat=SimpleNamespace(id=10, type="private")
        ),
        callback_query=None,
    )
    assert allowed_private_update(allowed, 10)
    assert not allowed_private_update(allowed, 11)
    allowed.message.chat.type = "group"
    assert not allowed_private_update(allowed, 10)
    allowed.message = None
    allowed.callback_query = SimpleNamespace(
        from_user=SimpleNamespace(id=10),
        message=SimpleNamespace(chat=SimpleNamespace(id=10, type="private")),
    )
    assert allowed_private_update(allowed, 10)


def test_allowed_user_id_file_rejects_invalid_value(tmp_path: Path):
    path = tmp_path / "allowed"
    path.write_text("10\n", encoding="utf-8")
    assert read_allowed_user_id(str(path)) == 10
    path.write_text("10,11", encoding="utf-8")
    with pytest.raises(ValueError):
        read_allowed_user_id(str(path))


@pytest.mark.asyncio
async def test_preview_marks_every_message_as_demo(monkeypatch):
    sent = []

    class FakeDelegate:
        async def send(self, chat_id, text, buttons=None):
            sent.append((chat_id, text, buttons))

        async def answer(self, callback_id):
            pass

    monkeypatch.setattr("gl_bot.poller.AiogramMessenger", lambda bot, **kwargs: FakeDelegate())
    messenger = PreviewMessenger(object())
    await messenger.send(10, "Карточка", [[{"text": "Назад", "callback_data": "pro:menu"}]])
    assert sent[0][1].startswith("🧪 ДЕМО · G&L\n\nКарточка")
    assert sent[0][2][0][0]["text"] == "Назад"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("actual_bot_id", "webhook_url", "message"),
    [
        (124, "", "different bot"),
        (123, "https://example.invalid/webhook", "existing webhook"),
    ],
)
async def test_preview_refuses_wrong_bot_or_existing_webhook(
    monkeypatch, tmp_path: Path, actual_bot_id: int, webhook_url: str, message: str
):
    allowed_file = tmp_path / "allowed-user"
    allowed_file.write_text("10", encoding="utf-8")
    monkeypatch.setenv("PREVIEW_ALLOWED_USER_ID_FILE", str(allowed_file))
    monkeypatch.setenv("PREVIEW_EXPECTED_BOT_ID", "123")
    closed = []

    class Session:
        def __init__(self, label="api", **_kwargs):
            self.label = label

        async def close(self):
            closed.append(self.label)

    class Bot:
        def __init__(self, **_kwargs):
            self.session = Session(label="telegram")

        async def get_me(self):
            return SimpleNamespace(id=actual_bot_id)

        async def get_webhook_info(self):
            return SimpleNamespace(url=webhook_url)

        async def get_updates(self, **_kwargs):
            raise AssertionError("preview consumed updates before safety checks")

    class Repo:
        def __init__(self, *_args):
            pass

        async def health(self):
            return None

    async def forbidden_pool(**_kwargs):
        raise AssertionError("preview API mode opened a DB pool")

    monkeypatch.setattr(
        "gl_bot.poller.Settings.from_env",
        lambda: SimpleNamespace(
            api_url="http://api:8000",
            api_token="synthetic-internal-token-1234567890",
            telegram_bot_token="123456:synthetic-bot-token",
        ),
    )
    monkeypatch.setattr("gl_bot.poller.aiohttp.ClientSession", Session)
    monkeypatch.setattr("gl_bot.poller.ApiRepository", Repo)
    monkeypatch.setattr("gl_bot.poller.asyncpg.create_pool", forbidden_pool)
    monkeypatch.setattr("gl_bot.poller.create_bot", lambda _settings: Bot())
    with pytest.raises(RuntimeError, match=message):
        await run()
    assert closed == ["telegram", "api"]


def test_preview_rejects_callback_with_another_chat_or_missing_message():
    callback = SimpleNamespace(
        message=None,
        callback_query=SimpleNamespace(
            from_user=SimpleNamespace(id=10),
            message=SimpleNamespace(chat=SimpleNamespace(id=11, type="private")),
        ),
    )
    assert not allowed_private_update(callback, 10)
    callback.callback_query.message = None
    assert not allowed_private_update(callback, 10)


@pytest.mark.asyncio
async def test_preview_retries_failed_update_without_skipping_later_updates(
    monkeypatch, tmp_path: Path, caplog
):
    allowed_file = tmp_path / "allowed-user"
    allowed_file.write_text("10", encoding="utf-8")
    monkeypatch.setenv("PREVIEW_ALLOWED_USER_ID_FILE", str(allowed_file))
    monkeypatch.setenv("PREVIEW_EXPECTED_BOT_ID", "123")
    monkeypatch.setattr("gl_bot.poller.HEARTBEAT", tmp_path / "heartbeat")
    observed_offsets = []
    handled = []
    closed = []

    def update(update_id, user_id):
        return SimpleNamespace(
            update_id=update_id,
            message=SimpleNamespace(
                from_user=SimpleNamespace(id=user_id),
                chat=SimpleNamespace(id=user_id, type="private"),
            ),
            callback_query=None,
        )

    class Session:
        def __init__(self, **_kwargs):
            pass

        async def close(self):
            closed.append("api")

    class Bot:
        def __init__(self, **_kwargs):
            self.session = SimpleNamespace(close=self.close)

        async def close(self):
            closed.append("telegram")

        async def get_me(self):
            return SimpleNamespace(id=123)

        async def get_webhook_info(self):
            return SimpleNamespace(url="")

        async def get_updates(self, *, offset, **_kwargs):
            observed_offsets.append(offset)
            if len(observed_offsets) == 1:
                return [update(4, 11), update(5, 10)]
            if len(observed_offsets) == 2:
                return [update(5, 10), update(6, 10)]
            raise asyncio.CancelledError()

    class Repo:
        def __init__(self, *_args):
            pass

        async def health(self):
            return None

    class Flow:
        def __init__(self, *_args, **kwargs):
            assert kwargs["contact_url"] == "https://t.me/synthetic_contact"

        async def handle(self, item):
            handled.append(item.update_id)
            if handled == [5]:
                raise RuntimeError("synthetic private update detail")

    async def no_delay(_seconds):
        return None

    monkeypatch.setattr(
        "gl_bot.poller.Settings.from_env",
        lambda: SimpleNamespace(
            api_url="http://api:8000",
            api_token="synthetic-internal-token-1234567890",
            telegram_bot_token="123456:synthetic-bot-token",
            display_time_zone="Europe/Moscow",
            recordings_api_enabled=False,
            german_contact_url="https://t.me/synthetic_contact",
        ),
    )
    monkeypatch.setattr("gl_bot.poller.aiohttp.ClientSession", Session)
    monkeypatch.setattr("gl_bot.poller.ApiRepository", Repo)
    monkeypatch.setattr("gl_bot.poller.create_bot", lambda _settings: Bot())
    monkeypatch.setattr("gl_bot.poller.BotFlow", Flow)
    monkeypatch.setattr("gl_bot.poller.asyncio.sleep", no_delay)
    with pytest.raises(asyncio.CancelledError):
        await run()
    assert observed_offsets == [None, 5, 7]
    assert handled == [5, 5, 6]
    assert closed == ["telegram", "api"]
    assert (tmp_path / "heartbeat").exists()
    assert "synthetic private update detail" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("label", ["", "🛠 DEV · G&L"])
async def test_environment_label_does_not_leak_demo_into_live_messages(monkeypatch, label):
    sent = []

    class Delegate:
        async def send(self, chat_id, text, buttons=None):
            sent.append((chat_id, text, buttons))
            return 123

    monkeypatch.setattr("gl_bot.poller.AiogramMessenger", lambda bot, **kwargs: Delegate())
    monkeypatch.setenv("BOT_MESSAGE_LABEL", label)
    messenger = PreviewMessenger(object(), label=message_label_from_env())
    buttons = [[{"text": "Назад", "callback_data": "pro:menu"}]]
    assert await messenger.send(10, "Карточка", buttons) == 123
    assert sent == [(10, f"{label}\n\nКарточка" if label else "Карточка", buttons)]
    assert "ДЕМО" not in sent[0][1]


@pytest.mark.parametrize("label", ["x" * 81, "DEV\npretend message", "DEV\t", "DEV\x7f"])
def test_message_label_rejects_control_characters_and_oversized_values(monkeypatch, label):
    monkeypatch.setenv("BOT_MESSAGE_LABEL", label)
    with pytest.raises(ValueError, match="BOT_MESSAGE_LABEL is invalid"):
        message_label_from_env()


def test_existing_preview_label_remains_default(monkeypatch):
    monkeypatch.delenv("BOT_MESSAGE_LABEL", raising=False)
    assert message_label_from_env() == "🧪 ДЕМО · G&L"
