import asyncio
import socket

import pytest
from aiogram.exceptions import TelegramNetworkError
from aiohttp_socks import ProxyError

from gl_bot.settings import Settings
from gl_bot.transport import create_bot

BASE = {
    "DATABASE_URL": "postgresql://synthetic:synthetic@localhost/synthetic",
    "TELEGRAM_BOT_TOKEN": "123456:synthetic_bot_token",
    "TELEGRAM_WEBHOOK_SECRET": "synthetic_secret",
}


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com:19881",
        "https://127.0.0.1:19881",
        "http://user:password@127.0.0.1:19881",
        "http://127.0.0.1:19881/path",
        "http://127.0.0.1:19881?secret=value",
        "http://127.0.0.1:19881#fragment",
        "http://127.0.0.1:bad",
        "http://127.0.0.1",
    ],
)
def test_proxy_rejects_external_or_ambiguous_destinations(url):
    with pytest.raises(ValueError, match="loopback HTTP proxy") as error:
        Settings.from_env({**BASE, "TELEGRAM_PROXY_URL": url})
    assert "password" not in str(error.value)
    assert "secret=value" not in str(error.value)


def test_direct_transport_remains_available_for_old_environments():
    assert Settings.from_env(BASE).telegram_proxy_url is None


@pytest.mark.asyncio
async def test_proxy_denial_never_falls_back_to_direct_telegram(monkeypatch):
    lines = []
    dns_hosts = []
    original = socket.getaddrinfo

    def resolve(host, *args, **kwargs):
        dns_hosts.append(host)
        assert host != "api.telegram.org", "direct Telegram resolution after proxy failure"
        return original(host, *args, **kwargs)

    async def reject(reader, writer):
        data = await reader.readuntil(b"\r\n\r\n")
        lines.append(data)
        writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(reject, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    bot = create_bot(Settings.from_env({**BASE, "TELEGRAM_PROXY_URL": f"http://127.0.0.1:{port}"}))
    try:
        with pytest.raises((TelegramNetworkError, ProxyError)):
            await asyncio.wait_for(bot.get_me(), 3)
    finally:
        await bot.session.close()
        server.close()
        await server.wait_closed()
    assert lines and lines[0].startswith(b"CONNECT api.telegram.org:443 ")
    assert BASE["TELEGRAM_BOT_TOKEN"].encode() not in lines[0]
    assert "api.telegram.org" not in dns_hosts


def test_startup_logs_only_exception_type(monkeypatch, caplog):
    from gl_bot import poller

    async def fail():
        raise RuntimeError("synthetic-secret-and-personal-payload")

    monkeypatch.setattr(poller, "run", fail)
    with pytest.raises(SystemExit) as error:
        poller.main()
    assert error.value.code == 1
    assert "bot_startup_failed type=RuntimeError" in caplog.text
    assert "synthetic-secret-and-personal-payload" not in caplog.text
