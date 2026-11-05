from pathlib import Path

import httpx
import pytest

from gl_api.app import create_app
from gl_api.config import Settings
from gl_api.security import MAX_INTERNAL_BODY

TOKEN = "synthetic-internal-token-1234567890"
CANARY = "synthetic-private-contact@invalid"


@pytest.fixture
def app(tmp_path: Path):
    token_file = tmp_path / "token"
    token_file.write_text(TOKEN)
    return create_app(Settings(bot_token_file=token_file))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path", ["/internal", "/internal/order-intents/", "/internal/bot/get_user", "/internal/future"]
)
@pytest.mark.parametrize("method", ["GET", "POST", "OPTIONS", "DELETE"])
async def test_entire_internal_namespace_requires_auth_before_parsing(app, path, method):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.request(method, path, content=CANARY)
    assert response.status_code == 401
    assert response.json() == {"detail": "unauthorized"}
    assert "location" not in response.headers
    assert CANARY not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"args": [{"email": CANARY}]},
        {"args": [1], "unexpected": CANARY},
        {"args": [CANARY] * 4},
    ],
)
async def test_validation_errors_do_not_reflect_payload(app, body, caplog):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/internal/bot/get_user", json=body, headers={"Authorization": f"Bearer {TOKEN}"}
        )
    assert response.status_code == 422
    assert response.json() == {"detail": "invalid request"}
    assert CANARY not in response.text + caplog.text
    assert TOKEN not in caplog.text


@pytest.mark.asyncio
async def test_chunked_oversized_body_rejected_before_database(app):
    async def chunks():
        yield b'{"args": ["'
        yield b"x" * MAX_INTERNAL_BODY
        yield b'"]}'

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/internal/bot/get_user",
            content=chunks(),
            headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
        )
    assert response.status_code == 413  # No pool is installed: a DB call would fail.


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "extra, status",
    [
        ({"Origin": "https://synthetic.invalid", "Content-Type": "application/json"}, 403),
        ({"Content-Type": "text/plain"}, 415),
        ({"Content-Type": "application/json", "Content-Encoding": "gzip"}, 415),
    ],
)
async def test_unsupported_browser_and_body_transport_rejected(app, extra, status):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/internal/bot/get_user",
            content=b'{"args":[1]}',
            headers={"Authorization": f"Bearer {TOKEN}", **extra},
        )
    assert response.status_code == status


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json", "/internal/order-intents/"])
async def test_documentation_and_automatic_redirects_disabled(app, path):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(path, headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 404
    assert "location" not in response.headers


@pytest.mark.asyncio
async def test_unexpected_exception_is_sanitized_and_health_remains_available(app, caplog):
    @app.get("/synthetic-failure")
    async def fail():
        raise RuntimeError(f"{CANARY} {TOKEN}")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/synthetic-failure")
        health = await client.get("/healthz")
    assert response.status_code == 500
    assert response.json() == {"detail": "internal server error"}
    assert health.status_code == 200
    assert CANARY not in response.text + caplog.text
    assert TOKEN not in response.text + caplog.text
    assert "request_failed" in caplog.text
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_non_ascii_and_duplicate_credentials_fail_closed(app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        for headers in (
            [(b"Authorization", "Bearer синтетический".encode())],
            [
                (b"Authorization", f"Bearer {TOKEN}".encode()),
                (b"Authorization", f"Bearer {TOKEN}".encode()),
            ],
        ):
            response = await client.post(
                "/internal/bot/health", json={"args": []}, headers=headers
            )
            assert response.status_code == 401


@pytest.mark.asyncio
async def test_slow_chunked_body_times_out_before_dispatch(monkeypatch):
    import asyncio

    from gl_api.security import SecurityBoundary

    timeout = asyncio.timeout
    monkeypatch.setattr("gl_api.security.asyncio.timeout", lambda _: timeout(0.01))
    dispatched = False
    messages = []

    async def downstream(*_args):
        nonlocal dispatched
        dispatched = True

    async def receive():
        await asyncio.sleep(0.1)
        return {"type": "http.request", "body": b"", "more_body": True}

    async def send(message):
        messages.append(message)

    await SecurityBoundary(downstream, TOKEN)(
        {
            "type": "http",
            "path": "/internal/bot/health",
            "method": "POST",
            "headers": [
                (b"authorization", f"Bearer {TOKEN}".encode()),
                (b"content-type", b"application/json"),
            ],
        },
        receive,
        send,
    )
    assert not dispatched
    assert messages[0]["status"] == 408


@pytest.mark.asyncio
async def test_disconnected_client_never_dispatches():
    from gl_api.security import SecurityBoundary

    async def fail(*_args):
        pytest.fail("disconnected client reached handler")

    async def receive():
        return {"type": "http.disconnect"}

    await SecurityBoundary(fail, TOKEN)(
        {
            "type": "http",
            "path": "/internal/bot/health",
            "method": "POST",
            "headers": [
                (b"authorization", f"Bearer {TOKEN}".encode()),
                (b"content-type", b"application/json"),
            ],
        },
        receive,
        fail,
    )
