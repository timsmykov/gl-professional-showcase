import base64

import httpx
import pytest

from gl_api.app import create_app
from gl_api.config import Settings
from gl_api.video import VideoDenied, token_hash
from gl_api.video_edge import VideoEdgeSettings, create_video_edge

TOKEN = "s" * 43
PASSWORD = "synthetic-drm-password-123456789012345"
BASIC = "Basic " + base64.b64encode(f"synthetic:{PASSWORD}".encode()).decode()


class Videos:
    def __init__(self):
        self.calls = 0
        self.allowed = True
        self.offline = False

    async def exchange(self, ticket):
        self.calls += 1
        if self.offline:
            raise OSError("private-db-password-canary")
        if not self.allowed:
            raise VideoDenied()
        return {"video_id": "synthetic-video", "token": TOKEN}

    async def authorize(self, token, video_id):
        self.calls += 1
        if self.offline:
            raise OSError("private-db-password-canary")
        return self.allowed


@pytest.fixture
def edge(tmp_path):
    user, password = tmp_path / "user", tmp_path / "password"
    user.write_text("synthetic")
    password.write_text(PASSWORD)
    app = create_video_edge(
        VideoEdgeSettings(
            video_watch_origin="https://watch.example",
            drm_username_file=user,
            drm_password_file=password,
        )
    )
    app.state.videos = Videos()
    return app


async def test_player_page_security_headers_and_no_internal_routes(edge):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=edge), base_url="https://watch.example"
    ) as client:
        page = await client.get("/watch/")
        assert page.status_code == 200
        assert "history.replaceState" in page.text
        assert "drmauthtoken" in page.text
        assert page.headers["referrer-policy"] == "no-referrer"
        assert page.headers["cache-control"] == "no-store"
        assert "noindex" in page.headers["x-robots-tag"]
        assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
        assert "unsafe-inline" not in page.headers["content-security-policy"]
        for path in (
            "/internal/video-sessions",
            "/api/webinars",
            "/docs",
            "/openapi.json",
            "/healthz",
            "/watch/?token=private",
        ):
            assert (await client.get(path)).status_code == 404
    assert edge.state.videos.calls == 0


@pytest.mark.parametrize(
    "authorization",
    [None, "Bearer " + TOKEN, "Basic wrong", "\u043f\u0440\u0438\u0432\u0435\u0442"],
)
async def test_callback_requires_basic_before_any_database(edge, authorization):
    headers = {} if authorization is None else {"Authorization": authorization.encode()}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=edge), base_url="https://watch.example"
    ) as client:
        response = await client.post(
            "/callbacks/kinescope/drm",
            json={"id": "video", "type": "video", "token": TOKEN},
            headers=headers,
        )
        assert response.status_code == 403
    assert edge.state.videos.calls == 0


@pytest.mark.parametrize(
    "allowed,offline,status", [(True, False, 200), (False, False, 403), (True, True, 503)]
)
async def test_callback_returns_200_only_on_verified_access(
    edge, caplog, allowed, offline, status
):
    edge.state.videos.allowed, edge.state.videos.offline = allowed, offline
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=edge), base_url="https://watch.example"
    ) as client:
        response = await client.post(
            "/callbacks/kinescope/drm",
            json={
                "id": "video",
                "type": "video",
                "token": TOKEN,
                "ip": "synthetic-canary",
                "user_agent": "synthetic-canary",
            },
            headers={"Authorization": BASIC},
        )
        assert response.status_code == status
        assert "canary" not in response.text
    assert "canary" not in caplog.text and PASSWORD not in caplog.text


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"id": "../video", "type": "video", "token": TOKEN},
        {"id": "video", "type": "live", "token": TOKEN},
        {"id": "video", "type": "video", "token": "123"},
    ],
)
async def test_bad_callback_denied_before_db(edge, body):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=edge), base_url="https://watch.example"
    ) as client:
        assert (
            await client.post(
                "/callbacks/kinescope/drm", json=body, headers={"Authorization": BASIC}
            )
        ).status_code == 403
    assert edge.state.videos.calls == 0


async def test_exchange_origin_and_body_limits(edge):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=edge), base_url="https://watch.example"
    ) as client:
        assert (
            await client.post(
                "/watch/session",
                json={"ticket": TOKEN},
                headers={"Origin": "https://evil.example"},
            )
        ).status_code == 403
        assert (
            await client.post(
                "/watch/session",
                content=b"x" * 16385,
                headers={"Content-Type": "application/json"},
            )
        ).status_code == 403
        assert edge.state.videos.calls == 0
        response = await client.post(
            "/watch/session", json={"ticket": TOKEN}, headers={"Origin": "https://watch.example"}
        )
        assert response.status_code == 200
        assert response.json()["token"] == TOKEN


@pytest.mark.parametrize("token", ["123", "\u0430" * 43, "a" * 42, "a" * 44, "../" * 14])
def test_tokens_are_not_identifiers(token):
    with pytest.raises(VideoDenied):
        token_hash(token)


@pytest.mark.parametrize(
    "origin",
    [
        "http://watch.example",
        "https://user:pass@watch.example",
        "https://watch.example/path",
        "https://watch.example?token=x",
        "https://watch.example#x",
        "https://watch.example:8443",
        " https://watch.example",
        "https://watch.example#",
        "https://watch.example?",
        "https://\u043f\u0440\u0438\u043c\u0435\u0440.\u0440\u0444",
    ],
)
def test_watch_origin_rejects_unsafe_configuration(origin):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(video_watch_origin=origin)


async def test_internal_issue_disabled_by_default_and_authenticated_when_enabled(tmp_path):
    token = tmp_path / "internal"
    token.write_text("synthetic-internal-token-123456789012345")

    class Offline:
        def acquire(self):
            raise OSError("private-canary")

    for enabled in (False, True):
        app = create_app(
            Settings(
                bot_token_file=token,
                video_watch_origin="https://watch.example" if enabled else None,
            )
        )
        app.state.pool = Offline()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            body = {"telegram_id": 123, "webinar_id": 1}
            headers = {"Authorization": "Bearer " + token.read_text()}
            response = await client.post("/internal/video-sessions", json=body, headers=headers)
            assert response.status_code == (503 if enabled else 404)
            assert "canary" not in response.text
            if enabled:
                assert (
                    await client.post("/internal/video-sessions", json=body)
                ).status_code == 401
                assert (
                    await client.post(
                        "/internal/video-sessions",
                        json=body,
                        headers={**headers, "Origin": "https://watch.example"},
                    )
                ).status_code == 403
                for invalid in (
                    {**body, "paid": True},
                    {**body, "telegram_id": 0},
                    {**body, "webinar_id": "1"},
                ):
                    assert (
                        await client.post(
                            "/internal/video-sessions", json=invalid, headers=headers
                        )
                    ).status_code == 422
