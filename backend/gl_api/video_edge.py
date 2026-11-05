"""Separate video edge application; it does not import or mount the internal API."""

import asyncio
import base64
import hashlib
import hmac
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

import asyncpg
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from gl_api.config import ConfigurationError, Settings, load_settings, read_secret
from gl_api.lifecycle import DatabasePool, close_pool
from gl_api.video import VideoDenied, VideoRepository


class VideoEdgeSettings(Settings):
    database_user: str = "gl_video_edge"
    database_password_file: Path = Field(
        default=Path("/run/secrets/gl_video_edge_password"), repr=False
    )
    drm_username_file: Path = Field(repr=False)
    drm_password_file: Path = Field(repr=False)


class Exchange(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    ticket: str = Field(pattern=r"^[A-Za-z0-9_-]{43}$", repr=False)


class DrmRequest(BaseModel):
    # Provider sends IP/UA; ignore them, never persist/log or use as identity.
    model_config = ConfigDict(extra="ignore", strict=True)
    id: str = Field(pattern=r"^[A-Za-z0-9-]{1,64}$")
    type: Literal["video"]
    token: str = Field(pattern=r"^[A-Za-z0-9_-]{43}$", repr=False)


SCRIPT = """let generation=0;async function openRecording(){const current=++generation;
const token=location.hash.slice(1);history.replaceState(null,'','/watch/');
const status=document.getElementById('status');
document.getElementById('player').replaceChildren();status.textContent='Проверяем доступ…';
try{
if(!/^[A-Za-z0-9_-]{43}$/.test(token))throw Error();
const response=await fetch('/watch/session',{method:'POST',headers:{'Content-Type':'application/json'},
body:JSON.stringify({ticket:token}),credentials:'omit',cache:'no-store',referrerPolicy:'no-referrer'});
if(!response.ok)throw Error();const session=await response.json();
if(current!==generation)return;
const frame=document.createElement('iframe');frame.title='Запись вебинара';
frame.referrerPolicy='no-referrer';frame.allow='autoplay; fullscreen; encrypted-media; picture-in-picture';
frame.allowFullscreen=true;frame.src='https://kinescope.io/embed/'+encodeURIComponent(session.video_id)
+'?drmauthtoken='+encodeURIComponent(session.token);document.getElementById('player').append(frame);
status.textContent='';
}catch{if(current===generation)status.textContent='Ссылка недоступна. Откройте новую ссылку в боте.';}
}addEventListener('hashchange',openRecording);openRecording();"""
STYLE = "body{margin:0;background:#111;color:#fff;font:18px system-ui}main{max-width:1200px;margin:auto;padding:24px}iframe{width:100%;aspect-ratio:16/9;border:0}"
HTML = (
    "<!doctype html><html lang='ru'><meta charset='utf-8'>"
    "<meta name='viewport' content='width=device-width,initial-scale=1'>"
    "<title>Просмотр вебинара G&amp;L</title><style>" + STYLE + "</style>"
    "<main><h1>Запись вебинара</h1><p id='status'>Проверяем доступ…</p>"
    "<div id='player'></div><noscript>Для просмотра включите JavaScript.</noscript></main>"
    "<script>" + SCRIPT + "</script></html>"
)


def csp_hash(value: str) -> str:
    return "'sha256-" + base64.b64encode(hashlib.sha256(value.encode()).digest()).decode() + "'"


class EdgeBoundary:
    def __init__(self, app, basic: bytes, origin: str):
        self.app, self.basic, self.origin = app, basic, origin

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = scope["headers"]
        path, method = scope["path"], scope["method"]

        async def reject(status):
            await JSONResponse(
                {"detail": "video unavailable"},
                status_code=status,
                headers={"Cache-Control": "no-store"},
            )(scope, receive, send)

        if scope.get("query_string") or (method, path) not in {
            ("GET", "/watch/"),
            ("POST", "/watch/session"),
            ("POST", "/callbacks/kinescope/drm"),
        }:
            await reject(404)
            return
        origins = [v for k, v in headers if k.lower() == b"origin"]
        if path == "/callbacks/kinescope/drm":
            credentials = [v for k, v in headers if k.lower() == b"authorization"]
            if (
                origins
                or len(credentials) != 1
                or not hmac.compare_digest(credentials[0], self.basic)
            ):
                await reject(403)
                return
        elif origins and (len(origins) != 1 or origins[0] != self.origin.encode("ascii")):
            await reject(403)
            return
        if method == "POST":
            types = [v for k, v in headers if k.lower() == b"content-type"]
            if (
                len(types) != 1
                or types[0].split(b";", 1)[0].strip().lower() != b"application/json"
                or any(k.lower() == b"content-encoding" for k, _ in headers)
            ):
                await reject(403)
                return
            body = bytearray()
            try:
                async with asyncio.timeout(5):
                    while True:
                        message = await receive()
                        if message["type"] == "http.disconnect":
                            return
                        chunk = message.get("body", b"")
                        if len(body) + len(chunk) > 16384:
                            await reject(403)
                            return
                        body.extend(chunk)
                        if not message.get("more_body", False):
                            break
            except TimeoutError:
                await reject(503)
                return
            original_receive, delivered = receive, False

            async def buffered_receive():
                nonlocal delivered
                if delivered:
                    return await original_receive()
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}

            receive = buffered_receive

        started = False

        async def safe_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                message = dict(message)
                message["headers"] = list(message.get("headers", [])) + [
                    (b"cache-control", b"no-store"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"x-content-type-options", b"nosniff"),
                    (b"x-frame-options", b"DENY"),
                    (b"x-robots-tag", b"noindex, nofollow, noarchive"),
                    (
                        b"content-security-policy",
                        (
                            "default-src 'none'; frame-src https://kinescope.io; connect-src 'self'; "
                            f"script-src {csp_hash(SCRIPT)}; style-src {csp_hash(STYLE)}; "
                            "base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
                        ).encode("ascii"),
                    ),
                ]
            await send(message)

        try:
            await self.app(scope, receive, safe_send)
        except Exception:  # noqa: BLE001 - no provider, driver or buyer data in traceback
            if not started:
                await JSONResponse({"detail": "video unavailable"}, status_code=503)(
                    scope, receive, safe_send
                )


def create_video_edge(settings: VideoEdgeSettings) -> FastAPI:
    if settings.video_watch_origin is None:
        raise ConfigurationError("video watch origin required")
    username = read_secret(settings.drm_username_file, "DRM username file")
    password = read_secret(settings.drm_password_file, "DRM password file")
    if ":" in username or len(password) < 32:
        raise ConfigurationError("invalid DRM credentials")
    basic = b"Basic " + base64.b64encode(f"{username}:{password}".encode())

    @asynccontextmanager
    async def lifespan(app):
        try:
            pool = await asyncpg.create_pool(
                dsn=settings.database_url(),
                ssl=settings.database_ssl(),
                min_size=0,
                max_size=3,
                timeout=3,
                command_timeout=3,
            )
        except Exception:  # noqa: BLE001 - sanitized startup failure
            raise RuntimeError("video edge startup failed") from None
        app.state.videos = VideoRepository(DatabasePool(pool))
        try:
            yield
        finally:
            await close_pool(pool)

    app = FastAPI(
        lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False
    )
    app.add_middleware(EdgeBoundary, basic=basic, origin=settings.video_watch_origin)

    @app.exception_handler(RequestValidationError)
    async def invalid(request, error):
        return JSONResponse({"detail": "video unavailable"}, status_code=403)

    @app.get("/watch/")
    async def watch():
        return HTMLResponse(HTML)

    @app.post("/watch/session")
    async def exchange(body: Exchange, request: Request):
        try:
            return await request.app.state.videos.exchange(body.ticket)
        except VideoDenied:
            return JSONResponse({"detail": "video unavailable"}, status_code=403)

    @app.post("/callbacks/kinescope/drm")
    async def drm(body: DrmRequest, request: Request):
        try:
            allowed = await request.app.state.videos.authorize(body.token, body.id)
        except VideoDenied:
            allowed = False
        return Response(status_code=200 if allowed else 403)

    return app


def from_environment() -> FastAPI:
    return create_video_edge(load_settings(VideoEdgeSettings))


def main() -> int:
    import uvicorn

    try:
        app = from_environment()
    except ConfigurationError:
        print("video_edge_configuration_error")
        return 2
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8001,
        access_log=False,
        proxy_headers=False,
        server_header=False,
        limit_concurrency=32,
        timeout_graceful_shutdown=10,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
