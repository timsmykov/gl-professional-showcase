"""Real local HTTP transport, synthetic provider JSON and no external credentials."""

import asyncio
import json
from contextlib import asynccontextmanager

import aiohttp
import pytest
from aiohttp import web

import gl_api.kinescope as module
from gl_api.config import ConfigurationError
from gl_api.kinescope import DrmPolicy, KinescopeClient, KinescopeError, resource_id

TOKEN = "10000000-0000-4000-8000-000000000001"
VIDEO = "10000000-0000-4000-8000-000000000002"
PROJECT = "10000000-0000-4000-8000-000000000003"


@asynccontextmanager
async def provider(monkeypatch, tmp_path, handler):
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    monkeypatch.setattr(module, "API_ORIGIN", f"http://127.0.0.1:{port}")
    secret = tmp_path / "synthetic-token"
    secret.write_text(TOKEN)
    try:
        async with aiohttp.ClientSession(trust_env=False) as session:
            yield KinescopeClient(session, secret)
    finally:
        await runner.cleanup()


async def test_transport_projects_video_and_drm_discard_sensitive_fields(monkeypatch, tmp_path):
    async def handler(request):
        assert request.method == "GET"
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        if request.path == "/v1/projects":
            assert request.query == {"page": "1", "per_page": "100"}
            data = [{"id": PROJECT, "encrypted": True, "secret": "provider-canary"}]
        elif request.path == f"/v1/videos/{VIDEO}":
            data = {
                "id": VIDEO,
                "project_id": PROJECT,
                "status": "done",
                "duration": 60,
                "download_link": "provider-canary",
                "assets": ["provider-canary"],
            }
        else:
            assert request.path == f"/v1/drm/auth/{PROJECT}"
            data = {
                "url": "https://watch.example/callbacks/kinescope/drm",
                "strict": True,
                "password": "provider-canary",
            }
        return web.json_response({"data": data})

    async with provider(monkeypatch, tmp_path, handler) as client:
        results = [
            await client.projects(),
            await client.video(VIDEO),
            await client.drm_policy(PROJECT),
        ]
        assert "provider-canary" not in repr(results)
        assert results[1].duration == 60
        assert results[2].verified_for("https://watch.example/callbacks/kinescope/drm")


@pytest.mark.parametrize("status", [301, 302, 307, 401, 403, 404, 429, 500, 503])
async def test_http_errors_and_redirects_never_echo_body_or_follow(
    monkeypatch, tmp_path, status, caplog
):
    requests = []

    async def handler(request):
        requests.append(request.path)
        return web.Response(
            status=status,
            text=TOKEN + "private-provider-canary",
            headers={"Location": "/secret-target"},
        )

    async with provider(monkeypatch, tmp_path, handler) as client:
        with pytest.raises(KinescopeError) as error:
            await client.video(VIDEO)
        assert TOKEN not in str(error.value) and "canary" not in str(error.value)
    assert requests == [f"/v1/videos/{VIDEO}"]
    assert TOKEN not in caplog.text


@pytest.mark.parametrize(
    "payload", ["not-json", "[]", "null", "{}", '{"data":null}', '{"data":{"id":"wrong"}}']
)
async def test_bad_provider_payload_fails_closed(monkeypatch, tmp_path, payload):
    async def handler(request):
        return web.Response(text=payload)

    async with provider(monkeypatch, tmp_path, handler) as client:
        with pytest.raises(KinescopeError):
            await client.video(VIDEO)


async def test_stream_limit(monkeypatch, tmp_path):
    monkeypatch.setattr(module, "MAX_RESPONSE_BYTES", 100)

    async def handler(request):
        return web.Response(body=b"x" * 101)

    async with provider(monkeypatch, tmp_path, handler) as client:
        with pytest.raises(KinescopeError, match="too large"):
            await client.video(VIDEO)


async def test_cancellation_is_not_converted_to_success(monkeypatch, tmp_path):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def handler(request):
        entered.set()
        await release.wait()
        return web.json_response({"data": {}})

    async with provider(monkeypatch, tmp_path, handler) as client:
        task = asyncio.create_task(client.video(VIDEO))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()


@pytest.mark.parametrize("value", ["../x", "id?secret=x", "x/y", "", "\u0430", "x" * 65])
def test_path_injection_rejected(value):
    with pytest.raises(ValueError):
        resource_id(value)


@pytest.mark.parametrize("strict", [None, False, True])
def test_drm_missing_or_non_strict_is_unverified(strict):
    policy = DrmPolicy(url="https://watch.example/callbacks/kinescope/drm", strict=strict)
    assert policy.verified_for(policy.url) is (strict is True)
    assert policy.verified_for("https://other.example/drm") is False


def test_invalid_token_is_safe_and_no_request_created(tmp_path):
    secret = tmp_path / "token"
    secret.write_text("private-bad-token")
    with pytest.raises(ConfigurationError) as error:
        KinescopeClient(None, secret)
    assert "private" not in str(error.value)


@pytest.mark.parametrize("failure", [TimeoutError, OSError, aiohttp.ClientConnectionError])
async def test_network_errors_are_sanitized(tmp_path, failure):
    class Session:
        def get(self, *args, **kwargs):
            assert kwargs["ssl"] is True and kwargs["allow_redirects"] is False
            assert kwargs["timeout"].total == 8
            raise failure("private-token-canary")

    secret = tmp_path / "token"
    secret.write_text(TOKEN)
    with pytest.raises(KinescopeError, match="^Kinescope unavailable$"):
        await KinescopeClient(Session(), secret).video(VIDEO)


@pytest.mark.parametrize(
    "data",
    [[], [{"id": PROJECT, "encrypted": "true"}], [{"id": "../bad"}], [{"id": PROJECT}] * 101],
)
async def test_project_contract_validates_lists(monkeypatch, tmp_path, data):
    async def handler(request):
        return web.Response(text=json.dumps({"data": data}), content_type="application/json")

    async with provider(monkeypatch, tmp_path, handler) as client:
        if not data:
            assert await client.projects() == []
        else:
            with pytest.raises(KinescopeError):
                await client.projects()
