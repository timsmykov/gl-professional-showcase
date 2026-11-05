"""Internal service boundary; never reflect credentials or request payloads."""

import asyncio
import hmac
import logging
from uuid import uuid4

from starlette.responses import JSONResponse

MAX_INTERNAL_BODY = 16 * 1024
logger = logging.getLogger(__name__)


class SecurityBoundary:
    def __init__(self, app, token: str | None):
        self.app = app
        self.expected = f"Bearer {token}".encode("ascii") if token else None

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id

        async def reject(status, detail):
            await JSONResponse(
                status_code=status,
                content={"detail": detail},
                headers={
                    "Cache-Control": "no-store",
                    "X-Content-Type-Options": "nosniff",
                    "X-Request-ID": request_id,
                },
            )(scope, receive, send)

        path = scope["path"]
        internal = path == "/internal" or path.startswith("/internal/")
        if internal and self.expected is not None:
            headers = scope["headers"]
            credentials = [value for name, value in headers if name.lower() == b"authorization"]
            if len(credentials) != 1 or not hmac.compare_digest(credentials[0], self.expected):
                await reject(401, "unauthorized")
                return
            # Browser requests have no role in this service-to-service contract.
            if any(name.lower() == b"origin" for name, _ in headers):
                await reject(403, "browser requests are not supported")
                return
            if scope["method"] == "POST":
                types = [value for name, value in headers if name.lower() == b"content-type"]
                if (
                    len(types) != 1
                    or types[0].split(b";", 1)[0].strip().lower() != b"application/json"
                ):
                    await reject(415, "JSON required")
                    return
                if any(name.lower() == b"content-encoding" for name, _ in headers):
                    await reject(415, "content encoding is not supported")
                    return
                # Count actual bytes, including chunked bodies, before any database operation.
                body = bytearray()
                try:
                    async with asyncio.timeout(5):
                        while True:
                            message = await receive()
                            if message["type"] == "http.disconnect":
                                return
                            chunk = message.get("body", b"")
                            if len(body) + len(chunk) > MAX_INTERNAL_BODY:
                                await reject(413, "request too large")
                                return
                            body.extend(chunk)
                            if not message.get("more_body", False):
                                break
                except TimeoutError:
                    await reject(408, "request timeout")
                    return

                original_receive = receive
                delivered = False

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
                if message["status"] >= 500:
                    logger.warning(
                        "api_response status=%s request_id=%s", message["status"], request_id
                    )
                message = dict(message)
                message["headers"] = list(message.get("headers", [])) + [
                    (b"cache-control", b"no-store"),
                    (b"x-content-type-options", b"nosniff"),
                    (b"x-request-id", request_id.encode("ascii")),
                ]
            await send(message)

        try:
            await self.app(scope, receive, safe_send)
        except Exception:  # noqa: BLE001 - sanitize the final HTTP error boundary
            # No exception text, SQL, URL, user IDs or traceback in operational logs.
            logger.error("request_failed request_id=%s", request_id)
            if not started:
                await reject(500, "internal server error")
