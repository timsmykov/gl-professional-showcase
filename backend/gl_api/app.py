import asyncio
import logging
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from typing import Annotated

import aiohttp
import asyncpg
from fastapi import Depends, FastAPI, HTTPException, Path, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from gl_api.atol_pay import AtolPayClient, ReceiptPolicy
from gl_api.atol_pay import load_api_token as load_atol_token
from gl_api.bot_contract import ARG_TYPES, Call, valid_args
from gl_api.bot_repository import Repository
from gl_api.checkout import CheckoutPending, CheckoutService
from gl_api.config import ConfigurationError, Settings, load_settings
from gl_api.fiscal_notifications import NotificationCredentials
from gl_api.kinescope import KinescopeClient, KinescopeError, load_api_token
from gl_api.lifecycle import READINESS_TIMEOUT_SECONDS, DatabasePool, close_pool, require_serving
from gl_api.order_contract import OrderIntent, PendingOrder
from gl_api.orders import OrderRepository, OrderRequestConflict, OrderUnavailable
from gl_api.payment_worker import PaymentReconciler
from gl_api.payments import CheckoutUnavailable, PaymentRepository
from gl_api.security import SecurityBoundary
from gl_api.video import TICKET_SECONDS, VideoDenied, VideoRepository

logger = logging.getLogger(__name__)
DATABASE_ERRORS = (asyncpg.PostgresError, asyncpg.InterfaceError, OSError, TimeoutError)


class Webinar(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    description: str
    price_kopeks: int | None = Field(ge=0)


class WatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    telegram_id: int = Field(gt=0, le=2**63 - 1)
    webinar_id: int = Field(gt=0, le=2**63 - 1)


class CheckoutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    telegram_id: int = Field(ge=-(2**63), le=2**63 - 1)
    order_id: int = Field(gt=0, le=2**63 - 1)


def create_app(settings: Settings | None = None) -> FastAPI:
    config = settings or load_settings()
    bot_token = config.bot_token()
    notifications = None
    if config.payment is not None:
        if bot_token is None:
            raise ConfigurationError("ATOL Pay requires internal service authentication")
        load_atol_token(config.payment.token_file)
        if config.payment.notification_endpoint is not None:
            notifications = NotificationCredentials.from_file(
                config.payment.notification_endpoint,
                config.payment.environment,
                config.payment.notification_key_file,
            )
    if config.kinescope_token_file is not None:
        if bot_token is None:
            raise ConfigurationError("Kinescope requires internal service authentication")
        load_api_token(config.kinescope_token_file)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.stopping = False
        logger.info("api_starting")
        # Lazy connections allow health monitoring during a database outage.
        try:
            pool = await asyncpg.create_pool(
                dsn=config.database_url(),
                ssl=config.database_ssl(),
                min_size=0,
                max_size=config.pool_max_size,
                timeout=3,
                command_timeout=3,
            )
        except asyncio.CancelledError:
            app.state.stopping = True
            raise
        except Exception:  # noqa: BLE001 - startup must not leak private driver errors.
            app.state.stopping = True
            logger.error("api_start_failed")
            raise RuntimeError("API startup failed") from None
        app.state.pool = DatabasePool(pool)
        logger.info("api_started")
        worker = None
        try:
            async with AsyncExitStack() as stack:
                if config.payment is not None:
                    session = await stack.enter_async_context(
                        aiohttp.ClientSession(trust_env=False)
                    )
                    options = config.payment
                    app.state.checkout = CheckoutService(
                        PaymentRepository(app.state.pool),
                        AtolPayClient(
                            session,
                            options.token_file,
                            environment=options.environment,
                            payment_hosts=options.payment_hosts,
                        ),
                        options.environment,
                        ReceiptPolicy(
                            options.sno,
                            options.tax,
                            options.payment_method,
                            options.payment_subject,
                        ),
                        options.return_url,
                        notifications,
                    )
                    worker = asyncio.create_task(
                        PaymentReconciler(app.state.checkout, options.poll_interval_seconds).run()
                    )
                try:
                    yield
                finally:
                    app.state.stopping = True
                    if worker is not None:
                        worker.cancel()
                        with suppress(asyncio.CancelledError):
                            await worker
        finally:
            app.state.stopping = True
            logger.info("api_stopping")
            await close_pool(pool)
            logger.info("api_stopped")

    app = FastAPI(
        title="G&L Operational API",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        redirect_slashes=False,
    )
    app.state.stopping = False
    app.add_middleware(SecurityBoundary, token=bot_token)

    if config.kinescope_token_file is not None:

        @app.get("/internal/video-provider/status", dependencies=[Depends(require_serving)])
        async def video_provider_status():
            try:
                async with aiohttp.ClientSession(trust_env=False) as session:
                    projects = await KinescopeClient(
                        session, config.kinescope_token_file
                    ).projects()
            except (ConfigurationError, KinescopeError):
                raise HTTPException(status_code=503, detail="video provider unavailable") from None
            return {
                "provider": "kinescope",
                "api_authenticated": True,
                "project_count": len(projects),
                "encrypted_project_count": sum(project.encrypted is True for project in projects),
                "inventory_complete": len(projects) < 100,
            }

    if config.payment is not None:

        @app.post(
            "/internal/checkouts", include_in_schema=False, dependencies=[Depends(require_serving)]
        )
        async def create_checkout(body: CheckoutRequest, request: Request):
            try:
                link = await request.app.state.checkout.checkout(body.telegram_id, body.order_id)
            except CheckoutPending:
                raise HTTPException(
                    status_code=409, detail="checkout is being reconciled"
                ) from None
            except CheckoutUnavailable:
                raise HTTPException(status_code=404, detail="checkout unavailable") from None
            except DATABASE_ERRORS:
                raise HTTPException(status_code=503, detail="checkout unavailable") from None
            return {"order_id": link.order_id, "payment_url": link.payment_url}

    if config.video_watch_origin is not None and bot_token is not None:

        @app.post("/internal/video-sessions", dependencies=[Depends(require_serving)])
        async def issue_video_session(body: WatchRequest, request: Request):
            try:
                ticket = await VideoRepository(request.app.state.pool).issue(
                    body.telegram_id, body.webinar_id
                )
            except VideoDenied:
                raise HTTPException(status_code=403, detail="video unavailable") from None
            except DATABASE_ERRORS:
                raise HTTPException(status_code=503, detail="video unavailable") from None
            return {
                "watch_url": f"{config.video_watch_origin}/watch/#{ticket}",
                "expires_in": TICKET_SECONDS,
            }

    @app.exception_handler(RequestValidationError)
    async def safe_validation_error(request: Request, error: RequestValidationError):
        if request.url.path == "/internal/order-intents":
            return JSONResponse(status_code=422, content={"detail": "invalid order intent"})
        return JSONResponse(status_code=422, content={"detail": "invalid request"})

    @app.get("/healthz")
    async def healthz():
        return {"status": "alive"}

    @app.get("/readyz", dependencies=[Depends(require_serving)])
    async def readyz(request: Request):
        try:
            async with asyncio.timeout(READINESS_TIMEOUT_SECONDS):
                async with request.app.state.pool.acquire(timeout=3) as conn:
                    await conn.fetchval("SELECT 1 FROM webinars LIMIT 1")
                if config.payment is not None:
                    await request.app.state.pool.fetchval(
                        "SELECT order_id FROM atol_payment_attempts LIMIT 1"
                    )
                if bot_token is not None:
                    await Repository(request.app.state.pool).health()
                    await request.app.state.pool.fetchval(
                        "SELECT source_update_id FROM purchase_orders LIMIT 1"
                    )
                    await request.app.state.pool.fetchval(
                        "SELECT order_id FROM order_intent_requests LIMIT 1"
                    )
        except DATABASE_ERRORS:
            logger.warning("database_unavailable")
            raise HTTPException(status_code=503, detail="database unavailable") from None
        return {"status": "ready"}

    @app.get(
        "/api/webinars", response_model=list[Webinar], dependencies=[Depends(require_serving)]
    )
    async def webinars(request: Request):
        try:
            async with request.app.state.pool.acquire(timeout=3) as conn:
                rows = await conn.fetch(
                    "SELECT id, title, description, price_kopeks FROM webinars "
                    "WHERE active ORDER BY id DESC LIMIT 100"
                )
        except DATABASE_ERRORS:
            logger.warning("database_unavailable")
            raise HTTPException(status_code=503, detail="database unavailable") from None
        return [dict(row) for row in rows]

    @app.get(
        "/api/webinars/{webinar_id}",
        response_model=Webinar,
        dependencies=[Depends(require_serving)],
    )
    async def webinar(webinar_id: Annotated[int, Path(ge=1, le=2**63 - 1)], request: Request):
        try:
            async with request.app.state.pool.acquire(timeout=3) as conn:
                row = await conn.fetchrow(
                    "SELECT id, title, description, price_kopeks FROM webinars "
                    "WHERE id = $1 AND active",
                    webinar_id,
                )
        except DATABASE_ERRORS:
            logger.warning("database_unavailable")
            raise HTTPException(status_code=503, detail="database unavailable") from None
        if row is None:
            raise HTTPException(status_code=404, detail="webinar not found")
        return dict(row)

    if bot_token is not None:

        @app.post(
            "/internal/order-status",
            response_model=PendingOrder,
            include_in_schema=False,
            dependencies=[Depends(require_serving)],
        )
        async def order_status(body: CheckoutRequest, request: Request):
            try:
                return await OrderRepository(request.app.state.pool).status(
                    body.telegram_id, body.order_id
                )
            except ValueError:
                raise HTTPException(status_code=422, detail="invalid order identifiers") from None
            except OrderUnavailable:
                raise HTTPException(status_code=404, detail="order unavailable") from None
            except DATABASE_ERRORS:
                raise HTTPException(status_code=503, detail="order unavailable") from None

        @app.post(
            "/internal/bot/{operation}",
            include_in_schema=False,
            dependencies=[Depends(require_serving)],
        )
        async def bot_operation(operation: str, call: Call, request: Request):
            if operation not in ARG_TYPES:
                raise HTTPException(status_code=404, detail="operation not found")
            if not valid_args(operation, call.args):
                raise HTTPException(status_code=422, detail="invalid arguments")
            try:
                result = await getattr(Repository(request.app.state.pool), operation)(*call.args)
            except asyncpg.ForeignKeyViolationError:
                raise HTTPException(status_code=422, detail="invalid reference") from None
            except DATABASE_ERRORS:
                logger.warning("database_unavailable")
                raise HTTPException(status_code=503, detail="database unavailable") from None
            return {"result": jsonable_encoder(result)}

        @app.post(
            "/internal/order-intents",
            response_model=PendingOrder,
            include_in_schema=False,
            dependencies=[Depends(require_serving)],
        )
        async def create_order_intent(intent: OrderIntent, request: Request):
            try:
                order = await OrderRepository(request.app.state.pool).create_pending_order(
                    intent.telegram_id, intent.webinar_id, intent.source_update_id
                )
            except ValueError:
                raise HTTPException(status_code=422, detail="invalid order identifiers") from None
            except OrderRequestConflict:
                raise HTTPException(status_code=409, detail="order request conflict") from None
            except OrderUnavailable:
                raise HTTPException(status_code=404, detail="order unavailable") from None
            except DATABASE_ERRORS:
                logger.warning("database_unavailable")
                raise HTTPException(status_code=503, detail="database unavailable") from None
            try:
                return PendingOrder.model_validate(
                    {field: order[field] for field in PendingOrder.model_fields}
                )
            except (KeyError, ValidationError):
                logger.error("invalid_order_record")
                raise HTTPException(status_code=503, detail="order unavailable") from None

    return app


app = create_app()
