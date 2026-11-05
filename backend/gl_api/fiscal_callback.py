"""Separate receipt-only ASGI listener; never mount the internal bot/API app here."""

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

import asyncpg
from fastapi import FastAPI, HTTPException, Request
from pydantic import Field, ValidationError
from pydantic_settings import SettingsConfigDict

from gl_api.config import ConfigurationError, DatabaseSettings
from gl_api.fiscal import ReceiptRepository, ReceiptUnavailable
from gl_api.fiscal_notifications import (
    MAX_NOTIFICATION_BYTES,
    InvalidNotification,
    NotificationCredentials,
    decode_notification,
    normalize_receipt,
)
from gl_api.lifecycle import DatabasePool, close_pool

logger = logging.getLogger(__name__)


class FiscalSettings(DatabaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="GL_FISCAL_", extra="ignore", hide_input_in_errors=True
    )
    database_user: str = "gl_receipt_app"
    require_database_tls: bool = True
    environment: Literal["sandbox", "production"]
    notification_endpoint: str = Field(repr=False)
    notification_key_file: Path = Field(repr=False)


def create_app(settings: FiscalSettings | None = None) -> FastAPI:
    try:
        config = settings or FiscalSettings()
    except ValidationError:
        raise ConfigurationError("invalid fiscal callback settings") from None
    credentials = NotificationCredentials.from_file(
        config.notification_endpoint, config.environment, config.notification_key_file
    )

    @asynccontextmanager
    async def lifespan(app):
        app.state.stopping = False
        pool = None
        try:
            pool = await asyncpg.create_pool(
                config.database_url(),
                ssl=config.database_ssl(),
                min_size=0,
                max_size=2,
                timeout=3,
                command_timeout=3,
            )
            app.state.receipts = ReceiptRepository(DatabasePool(pool))
            yield
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - do not log driver credentials or request data.
            raise RuntimeError("fiscal callback lifecycle failed") from None
        finally:
            app.state.stopping = True
            if pool is not None:
                await close_pool(pool)

    app = FastAPI(
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        redirect_slashes=False,
    )
    app.state.stopping = False

    @app.get("/healthz")
    async def health():
        return {"status": "alive"}

    @app.get("/readyz")
    async def ready():
        if app.state.stopping:
            raise HTTPException(503, "notification unavailable")
        try:
            await app.state.receipts.pool.fetchval(
                "SELECT receipt_id FROM payment_receipts LIMIT 1"
            )
        except (asyncpg.PostgresError, asyncpg.InterfaceError, OSError, TimeoutError):
            raise HTTPException(503, "notification unavailable") from None
        return {"status": "ready"}

    @app.post("/callbacks/atol/receipts")
    async def notification(request: Request):
        if request.headers.get("origin") is not None or request.headers.get("cookie") is not None:
            raise HTTPException(403, "notification forbidden")
        if request.app.state.stopping:
            raise HTTPException(503, "notification unavailable")
        tokens = request.query_params.getlist("token")
        if len(tokens) != 1 or set(request.query_params) != {"token"}:
            raise HTTPException(401, "notification unauthorized")
        if request.headers.get("content-type", "").split(";", 1)[0].lower() != "application/json":
            raise HTTPException(415, "JSON required")
        try:
            raw = bytearray()
            async with asyncio.timeout(5):
                async for chunk in request.stream():
                    raw.extend(chunk)
                    if len(raw) > MAX_NOTIFICATION_BYTES:
                        raise HTTPException(413, "notification too large")
            body = decode_notification(bytes(raw))
            if not credentials.verify(body["orderId"], tokens[0]):
                raise HTTPException(401, "notification unauthorized")
            observation = normalize_receipt(body, config.environment)
        except InvalidNotification:
            raise HTTPException(400, "invalid notification") from None
        except TimeoutError:
            raise HTTPException(408, "notification timeout") from None
        if observation is not None:
            try:
                await request.app.state.receipts.record(
                    observation, authenticated_notification=True
                )
            except ReceiptUnavailable:
                raise HTTPException(409, "receipt unavailable") from None
            except (asyncpg.PostgresError, asyncpg.InterfaceError, OSError, TimeoutError):
                logger.warning("receipt_database_unavailable")
                raise HTTPException(503, "notification unavailable") from None
        return {"status": "success"}

    return app


def main():
    import uvicorn

    # Access logs would disclose the order-scoped query credential.
    app = create_app()
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8001,
        access_log=False,
        proxy_headers=False,
        server_header=False,
        limit_concurrency=16,
        timeout_graceful_shutdown=10,
    )


if __name__ == "__main__":
    main()
