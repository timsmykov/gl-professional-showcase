import httpx
import pytest
from aiohttp import web

import gl_api.kinescope as provider
from gl_api.app import create_app
from gl_api.config import ConfigurationError, Settings, load_settings

TOKEN = "10000000-0000-4000-8000-000000000001"


@pytest.fixture
def configured(tmp_path):
    key, internal = tmp_path / "key", tmp_path / "internal"
    key.write_text(TOKEN)
    internal.write_text("s" * 40)
    return Settings(kinescope_token_file=key, bot_token_file=internal)


async def test_real_runtime_transport_requires_auth_and_projects_only(
    configured, monkeypatch, caplog
):
    requests = []

    async def projects(request):
        requests.append(request.path)
        assert request.headers["Authorization"] == "Bearer " + TOKEN
        return web.json_response(
            {"data": [{"id": "synthetic-project", "encrypted": False, "secret": TOKEN}]}
        )

    server = web.Application()
    server.router.add_get("/v1/projects", projects)
    runner = web.AppRunner(server, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    monkeypatch.setattr(
        provider, "API_ORIGIN", f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    )
    try:
        app = create_app(configured)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://internal"
        ) as client:
            path = "/internal/video-provider/status"
            assert (await client.get(path)).status_code == 401
            headers = {"Authorization": "Bearer " + "s" * 40}
            assert (
                await client.get(path, headers={**headers, "Origin": "https://site.example"})
            ).status_code == 403
            assert requests == []
            response = await client.get(path, headers=headers)
            assert response.json() == {
                "provider": "kinescope",
                "api_authenticated": True,
                "project_count": 1,
                "encrypted_project_count": 0,
                "inventory_complete": True,
            }
            assert TOKEN not in response.text
            app.state.stopping = True
            assert (await client.get(path, headers=headers)).status_code == 503
            assert requests == ["/v1/projects"]
    finally:
        await runner.cleanup()
    assert TOKEN not in caplog.text


@pytest.mark.parametrize("failure", ["network", "auth", "secret"])
async def test_provider_failure_is_sanitized_and_does_not_break_health(
    configured, monkeypatch, caplog, failure
):
    app = create_app(configured)

    async def fail(*args):
        if failure == "network":
            raise provider.KinescopeError("Kinescope unavailable")
        if failure == "secret":
            raise ConfigurationError("Kinescope API token file cannot be read")
        raise provider.KinescopeError("Kinescope authentication failed")

    monkeypatch.setattr(provider.KinescopeClient, "projects", fail)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://internal"
    ) as client:
        response = await client.get(
            "/internal/video-provider/status", headers={"Authorization": "Bearer " + "s" * 40}
        )
        assert response.status_code == 503
        assert response.json() == {"detail": "video provider unavailable"}
        assert (await client.get("/healthz")).status_code == 200
    assert TOKEN not in caplog.text


def test_key_without_internal_auth_cannot_start(configured):
    with pytest.raises(ConfigurationError, match="internal service authentication"):
        create_app(configured.model_copy(update={"bot_token_file": None}))


def test_invalid_key_is_rejected_without_value(configured):
    configured.kinescope_token_file.write_text("private-canary-invalid")
    with pytest.raises(ConfigurationError) as error:
        create_app(configured)
    assert "canary" not in str(error.value)


async def test_provider_not_configured_route_absent():
    app = create_app(Settings())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://internal"
    ) as client:
        assert (await client.get("/internal/video-provider/status")).status_code == 404


def test_empty_compose_path_is_disabled_and_raw_env_secret_rejected(monkeypatch):
    monkeypatch.setenv("GL_API_KINESCOPE_TOKEN_FILE", "")
    assert load_settings().kinescope_token_file is None
    monkeypatch.setenv("GL_API_KINESCOPE_TOKEN", "private-canary")
    with pytest.raises(ConfigurationError) as error:
        load_settings()
    assert "private-canary" not in str(error.value)
