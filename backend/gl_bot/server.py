from __future__ import annotations

import asyncio
import hmac
import logging
from collections.abc import AsyncIterator

import aiohttp
import asyncpg
from aiogram.types import Update
from aiohttp import web
from pydantic import ValidationError

from .api_recordings import ApiRecordingGateway
from .api_repository import ApiRepository, BackendError
from .flow import BotFlow, UpdateBusyError
from .repository import Repository
from .settings import Settings
from .telegram import AiogramMessenger
from .transport import create_bot

logger = logging.getLogger(__name__)
REPO_KEY = web.AppKey("repo", object)
FLOW_KEY = web.AppKey("flow", object)
SECRET_KEY = web.AppKey("webhook_secret", str)


async def health(request: web.Request) -> web.Response:
    try:
        await request.app[REPO_KEY].health()
    except Exception:  # noqa: BLE001 - health must fail closed on any repository error.
        return web.Response(status=503, text="unavailable")
    return web.Response(text="ok")


async def webhook(request: web.Request) -> web.Response:
    supplied = request.headers.getall("X-Telegram-Bot-Api-Secret-Token", [])
    if len(supplied) != 1 or not hmac.compare_digest(supplied[0], request.app[SECRET_KEY]):
        return web.Response(status=401, text="unauthorized")
    if request.content_type != "application/json":
        return web.Response(status=415, text="unsupported media type")
    try:
        payload = await request.json()
        update = Update.model_validate(payload)
    except (ValueError, ValidationError):
        return web.Response(status=400, text="invalid update")
    try:
        await request.app[FLOW_KEY].handle(update)
    except (UpdateBusyError, BackendError):
        return web.Response(status=503, text="temporary error")
    except Exception as error:  # noqa: BLE001 - keep webhook errors generic to Telegram.
        logger.error(
            "webhook_failed name=%s code=%s",
            type(error).__name__,
            getattr(error, "sqlstate", None),
        )
        return web.Response(status=500, text="temporary error")
    return web.Response(text="ok")


def create_app(webhook_secret: str, *, repo=None, flow=None) -> web.Application:
    app = web.Application(client_max_size=1024 * 1024)
    app[SECRET_KEY] = webhook_secret
    if repo is not None:
        app[REPO_KEY] = repo
    if flow is not None:
        app[FLOW_KEY] = flow
    app.router.add_get("/healthz", health)
    app.router.add_post("/telegram/webhook", webhook)
    return app


async def resources(app: web.Application, settings: Settings) -> AsyncIterator[None]:
    pool = None
    session = None
    bot = None
    dispatcher_task = None
    try:
        if settings.api_url:
            session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8))
            repo = ApiRepository(session, settings.api_url, settings.api_token or "")
        else:
            pool = await asyncpg.create_pool(
                dsn=settings.database_url, min_size=1, max_size=10, timeout=5
            )
            repo = Repository(pool)
        await repo.health()
        bot = create_bot(settings)
        app[REPO_KEY] = repo
        flow = BotFlow(
            repo,
            AiogramMessenger(bot, rich=True),
            german_telegram_id=settings.german_telegram_id,
            contact_url=settings.german_contact_url,
            time_zone=settings.display_time_zone,
            product_cards=True,
            recordings=ApiRecordingGateway(repo) if settings.recordings_api_enabled else None,
        )
        app[FLOW_KEY] = flow
        dispatcher_task = asyncio.create_task(dispatch_notifications(flow))
        yield
    finally:
        if dispatcher_task:
            dispatcher_task.cancel()
            try:
                await dispatcher_task
            except asyncio.CancelledError:
                pass
        if bot:
            await bot.session.close()
        if session:
            await session.close()
        if pool:
            await pool.close()


async def dispatch_notifications(flow: BotFlow) -> None:
    while True:
        try:
            delivered = await flow.deliver_pending()
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - a transient API failure must not stop delivery.
            logger.warning("booking_notification_worker_failed type=%s", type(error).__name__)
            delivered = False
        if not delivered:
            await asyncio.sleep(2)


def main() -> None:
    settings = Settings.from_env()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    app = create_app(settings.telegram_webhook_secret)
    app.cleanup_ctx.append(lambda current_app: resources(current_app, settings))
    web.run_app(app, host="0.0.0.0", port=settings.port)


if __name__ == "__main__":
    main()
