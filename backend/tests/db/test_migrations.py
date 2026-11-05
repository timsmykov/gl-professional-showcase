"""Exercise Alembic and PostgreSQL invariants on disposable synthetic databases."""

import asyncio
import os
import subprocess
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest

from gl_api.bot_repository import Repository
from gl_api.config import Settings, read_secret
from gl_api.video import VideoDenied, VideoRepository, token_hash

pytestmark = pytest.mark.skipif(
    os.getenv("GL_API_TEST_DATABASE") != "1", reason="isolated PostgreSQL test container required"
)

ROOT = Path(__file__).resolve().parents[2]
LEGACY_SQL = ROOT / "sql/001_init.sql"
FROZEN_LEGACY_SQL = ROOT / "migrations/legacy_001_init.sql"
EXPECTED_TABLES = {
    "users",
    "user_sections",
    "services",
    "appointment_slots",
    "booking_requests",
    "booking_notification_outbox",
    "products",
    "webinars",
    "webinar_entitlements",
    "processed_updates",
    "purchase_orders",
    "order_intent_requests",
    "payment_events",
    "alembic_version",
}
EXPECTED_FOREIGN_KEYS = {
    ("user_sections", "users"),
    ("appointment_slots", "services"),
    ("booking_requests", "users"),
    ("booking_requests", "appointment_slots"),
    ("booking_notification_outbox", "booking_requests"),
    ("webinar_entitlements", "users"),
    ("webinar_entitlements", "webinars"),
    ("purchase_orders", "users"),
    ("purchase_orders", "webinars"),
    ("order_intent_requests", "purchase_orders"),
    ("payment_events", "purchase_orders"),
}


def database_settings(name: str, user: str) -> Settings:
    password_file = {
        "postgres": "GL_API_TEST_ADMIN_PASSWORD_FILE",
        "gl_api_migrator": "GL_API_TEST_MIGRATOR_PASSWORD_FILE",
        "gl_api_app": "GL_API_DATABASE_PASSWORD_FILE",
    }[user]
    return Settings(
        database_host=os.environ["GL_API_DATABASE_HOST"],
        database_name=name,
        database_user=user,
        database_password_file=Path(os.environ[password_file]),
    )


async def connect(name: str, user: str = "gl_api_migrator") -> asyncpg.Connection:
    return await asyncpg.connect(database_settings(name, user).database_url())


@asynccontextmanager
async def isolated_database():
    """Create only a uniquely named test DB and remove only that DB on exit."""
    name = f"gl_test_{uuid4().hex}"
    admin = await connect("postgres", "postgres")
    created = False
    try:
        await admin.execute(f'CREATE DATABASE "{name}" OWNER gl_api_migrator')
        created = True
        await admin.execute(f'REVOKE CONNECT ON DATABASE "{name}" FROM PUBLIC')
        await admin.execute(f'GRANT CONNECT ON DATABASE "{name}" TO gl_api_app, gl_api_migrator')
        database_admin = await connect(name, "postgres")
        try:
            await database_admin.execute("REVOKE ALL ON SCHEMA public FROM PUBLIC")
            await database_admin.execute("GRANT USAGE ON SCHEMA public TO gl_api_app")
        finally:
            await database_admin.close()
        yield name
    finally:
        if created:
            await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        await admin.close()


def alembic(name: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(
        GL_API_DATABASE_NAME=name,
        GL_API_DATABASE_USER="gl_api_migrator",
        GL_API_DATABASE_PASSWORD_FILE=os.environ["GL_API_TEST_MIGRATOR_PASSWORD_FILE"],
    )
    return subprocess.run(
        [str(ROOT / ".venv/bin/alembic"), *arguments],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=45,
        check=False,
    )


def assert_upgrade(name: str, *arguments: str) -> None:
    result = alembic(name, *arguments)
    assert result.returncode == 0, f"Alembic {arguments} failed with exit {result.returncode}"


def assert_no_passwords_in_output(result: subprocess.CompletedProcess[str]) -> None:
    output = result.stdout + result.stderr
    for key in (
        "GL_API_TEST_ADMIN_PASSWORD_FILE",
        "GL_API_TEST_MIGRATOR_PASSWORD_FILE",
        "GL_API_DATABASE_PASSWORD_FILE",
    ):
        assert Path(os.environ[key]).read_text().strip() not in output


async def rejects(connection: asyncpg.Connection, sql: str, *args, error: type[Exception]):
    with pytest.raises(error):
        async with connection.transaction():
            await connection.execute(sql, *args)


@pytest.mark.asyncio
async def test_video_tickets_concurrency_revocation_and_least_privilege():
    async with isolated_database() as name:
        assert_upgrade(name, "upgrade", "head")
        assert_upgrade(name, "upgrade", "head")
        migrator = await connect(name)
        pool = await asyncpg.create_pool(database_settings(name, "gl_api_app").database_url())
        repo = VideoRepository(pool)
        try:
            user = await migrator.fetchval(
                "INSERT INTO users(telegram_id) VALUES (9100000555) RETURNING id"
            )
            webinar = await migrator.fetchval(
                "INSERT INTO webinars(title,active) VALUES ('Synthetic video',true) RETURNING id"
            )
            with pytest.raises(VideoDenied):
                await repo.issue(9100000555, webinar)
            await migrator.execute(
                "INSERT INTO webinar_entitlements(user_id,webinar_id,source) VALUES ($1,$2,'imported')",
                user,
                webinar,
            )
            await migrator.execute(
                "INSERT INTO webinar_videos(webinar_id,project_id,video_id) VALUES ($1,'project','video')",
                webinar,
            )
            with pytest.raises(VideoDenied):
                await repo.issue(9100000555, webinar)
            await rejects(
                migrator,
                "UPDATE webinar_videos SET active=true WHERE webinar_id=$1",
                webinar,
                error=asyncpg.CheckViolationError,
            )
            await migrator.execute(
                "UPDATE webinar_videos SET active=true,protection_verified_at=clock_timestamp() "
                "WHERE webinar_id=$1",
                webinar,
            )
            with pytest.raises(VideoDenied):
                await repo.issue(9100000556, webinar)
            ticket = await repo.issue(9100000555, webinar)
            assert await migrator.fetchval(
                "SELECT ticket_hash FROM video_sessions WHERE user_id=$1", user
            ) == token_hash(ticket)
            outcomes = await asyncio.gather(
                repo.exchange(ticket), repo.exchange(ticket), return_exceptions=True
            )
            success = [value for value in outcomes if isinstance(value, dict)]
            assert len(success) == 1
            assert sum(isinstance(value, VideoDenied) for value in outcomes) == 1
            playback = success[0]["token"]
            assert success[0]["video_id"] == "video"
            assert await repo.authorize(playback, "video") is True
            assert await repo.authorize(playback, "another-video") is False
            assert await repo.authorize(ticket, "video") is False
            # Binding edits cannot retarget an already issued session.
            await migrator.execute(
                "UPDATE webinar_videos SET video_id='other' WHERE webinar_id=$1", webinar
            )
            assert await repo.authorize(playback, "video") is False
            await migrator.execute(
                "UPDATE webinar_videos SET video_id='video' WHERE webinar_id=$1", webinar
            )
            await migrator.execute("UPDATE webinars SET active=false WHERE id=$1", webinar)
            assert await repo.authorize(playback, "video") is False
            await migrator.execute("UPDATE webinars SET active=true WHERE id=$1", webinar)
            await migrator.execute("DELETE FROM webinar_entitlements WHERE user_id=$1", user)
            assert await repo.authorize(playback, "video") is False
            await migrator.execute(
                "INSERT INTO webinar_entitlements(user_id,webinar_id,source) VALUES ($1,$2,'imported')",
                user,
                webinar,
            )
            assert await repo.authorize(playback, "video") is True
            await migrator.execute(
                "UPDATE video_sessions SET revoked_at=clock_timestamp() WHERE ticket_hash=$1",
                token_hash(ticket),
            )
            assert await repo.authorize(playback, "video") is False
            async with pool.acquire() as app:
                await rejects(
                    app,
                    "UPDATE webinar_videos SET active=false",
                    error=asyncpg.InsufficientPrivilegeError,
                )
                await rejects(
                    app,
                    "UPDATE webinar_entitlements SET source='imported' WHERE user_id=$1 AND webinar_id=$2",
                    user,
                    webinar,
                    error=asyncpg.InsufficientPrivilegeError,
                )
                await rejects(
                    app,
                    "UPDATE video_sessions SET user_id=$1",
                    user,
                    error=asyncpg.InsufficientPrivilegeError,
                )
                await rejects(
                    app,
                    "UPDATE video_sessions SET revoked_at=NULL",
                    error=asyncpg.InsufficientPrivilegeError,
                )
        finally:
            await pool.close()
            await migrator.close()


@pytest.mark.asyncio
async def test_video_expired_ticket_session_and_failed_exchange_are_safe():
    async with isolated_database() as name:
        assert_upgrade(name, "upgrade", "head")
        migrator = await connect(name)
        pool = await asyncpg.create_pool(database_settings(name, "gl_api_app").database_url())
        repo = VideoRepository(pool)
        try:
            user = await migrator.fetchval(
                "INSERT INTO users(telegram_id) VALUES (9100000666) RETURNING id"
            )
            webinar = await migrator.fetchval(
                "INSERT INTO webinars(title,active) VALUES ('Synthetic expiry',true) RETURNING id"
            )
            await migrator.execute(
                "INSERT INTO webinar_entitlements(user_id,webinar_id,source) VALUES ($1,$2,'imported')",
                user,
                webinar,
            )
            await migrator.execute(
                "INSERT INTO webinar_videos(webinar_id,project_id,video_id,active,protection_verified_at) VALUES ($1,'project','video',true,clock_timestamp())",
                webinar,
            )
            ticket = await repo.issue(9100000666, webinar)
            await migrator.execute(
                "UPDATE video_sessions SET created_at=clock_timestamp()-interval '2 minutes',ticket_expires_at=clock_timestamp()-interval '1 minute' WHERE ticket_hash=$1",
                token_hash(ticket),
            )
            with pytest.raises(VideoDenied):
                await repo.exchange(ticket)
            assert (
                await migrator.fetchval(
                    "SELECT consumed_at FROM video_sessions WHERE ticket_hash=$1",
                    token_hash(ticket),
                )
                is None
            )
            ticket = await repo.issue(9100000666, webinar)
            playback = (await repo.exchange(ticket))["token"]
            await migrator.execute(
                "UPDATE video_sessions SET consumed_at=clock_timestamp()-interval '2 minutes',playback_expires_at=clock_timestamp()-interval '1 minute' WHERE ticket_hash=$1",
                token_hash(ticket),
            )
            assert await repo.authorize(playback, "video") is False
            ticket = await repo.issue(9100000666, webinar)
            await migrator.execute(
                "UPDATE webinar_videos SET active=false WHERE webinar_id=$1", webinar
            )
            with pytest.raises(VideoDenied):
                await repo.exchange(ticket)
        finally:
            await pool.close()
            await migrator.close()


@pytest.mark.asyncio
async def test_video_http_path_with_separate_restricted_edge_role(tmp_path):
    import base64
    import secrets

    import httpx

    from gl_api.app import create_app
    from gl_api.video_edge import VideoEdgeSettings, create_video_edge

    role = "video_test_" + uuid4().hex
    password = secrets.token_hex(24)  # Synthetic ephemeral role, never managed credentials.
    admin = await connect("postgres", "postgres")
    try:
        await admin.execute(
            f"CREATE ROLE {role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD '{password}'"
        )
        async with isolated_database() as name:
            assert_upgrade(name, "upgrade", "head")
            migrator = await connect(name)
            app_pool = await asyncpg.create_pool(
                database_settings(name, "gl_api_app").database_url()
            )
            edge_pool = None
            try:
                await migrator.execute(f"GRANT CONNECT ON DATABASE {name} TO {role}")
                await migrator.execute(f"GRANT USAGE ON SCHEMA public TO {role}")
                await migrator.execute(
                    f"GRANT SELECT ON webinar_videos,video_sessions,webinars,webinar_entitlements TO {role}"
                )
                await migrator.execute(
                    f"GRANT UPDATE(playback_hash,consumed_at,playback_expires_at) ON video_sessions TO {role}"
                )
                secret = tmp_path / "edge_password"
                secret.write_text(password)
                edge_settings = VideoEdgeSettings(
                    database_host=os.environ["GL_API_DATABASE_HOST"],
                    database_name=name,
                    database_user=role,
                    database_password_file=secret,
                    video_watch_origin="https://watch.example",
                    drm_username_file=tmp_path / "drm_user",
                    drm_password_file=tmp_path / "drm_password",
                )
                edge_settings.drm_username_file.write_text("synthetic")
                edge_settings.drm_password_file.write_text(
                    "synthetic-password-12345678901234567890"
                )
                token = tmp_path / "internal"
                token.write_text("synthetic-internal-token-123456789012345")
                edge_pool = await asyncpg.create_pool(edge_settings.database_url())
                uid = await migrator.fetchval(
                    "INSERT INTO users(telegram_id) VALUES (9100000777) RETURNING id"
                )
                webinar = await migrator.fetchval(
                    "INSERT INTO webinars(title,active) VALUES ('Synthetic HTTP video',true) RETURNING id"
                )
                await migrator.execute(
                    "INSERT INTO webinar_videos(webinar_id,project_id,video_id,active,protection_verified_at) VALUES ($1,'project','video',true,clock_timestamp())",
                    webinar,
                )
                await migrator.execute(
                    "INSERT INTO webinar_entitlements(user_id,webinar_id,source) VALUES ($1,$2,'imported')",
                    uid,
                    webinar,
                )
                api = create_app(
                    Settings(bot_token_file=token, video_watch_origin="https://watch.example")
                )
                api.state.pool = app_pool
                edge = create_video_edge(edge_settings)
                edge.state.videos = VideoRepository(edge_pool)
                credentials = (
                    "Basic "
                    + base64.b64encode(
                        ("synthetic:" + edge_settings.drm_password_file.read_text()).encode()
                    ).decode()
                )
                async with (
                    edge.router.lifespan_context(edge),
                    httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=api), base_url="http://internal"
                    ) as bot,
                    httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=edge), base_url="https://watch.example"
                    ) as viewer,
                ):
                    issued = await bot.post(
                        "/internal/video-sessions",
                        json={"telegram_id": 9100000777, "webinar_id": webinar},
                        headers={"Authorization": "Bearer " + token.read_text()},
                    )
                    assert issued.status_code == 200
                    ticket = issued.json()["watch_url"].split("#")[1]
                    playback = await viewer.post("/watch/session", json={"ticket": ticket})
                    assert playback.status_code == 200
                    assert (
                        await viewer.post("/watch/session", json={"ticket": ticket})
                    ).status_code == 403
                    body = {"id": "video", "type": "video", "token": playback.json()["token"]}
                    assert (
                        await viewer.post(
                            "/callbacks/kinescope/drm",
                            json=body,
                            headers={"Authorization": credentials},
                        )
                    ).status_code == 200
                    assert (
                        await viewer.post(
                            "/callbacks/kinescope/drm",
                            json={**body, "id": "other"},
                            headers={"Authorization": credentials},
                        )
                    ).status_code == 403
                    await migrator.execute(
                        "DELETE FROM webinar_entitlements WHERE user_id=$1", uid
                    )
                    assert (
                        await viewer.post(
                            "/callbacks/kinescope/drm",
                            json=body,
                            headers={"Authorization": credentials},
                        )
                    ).status_code == 403
                    await migrator.execute(f"REVOKE SELECT ON webinar_entitlements FROM {role}")
                    assert (
                        await viewer.post(
                            "/callbacks/kinescope/drm",
                            json=body,
                            headers={"Authorization": credentials},
                        )
                    ).status_code == 503
                async with edge_pool.acquire() as connection:
                    for query in (
                        "SELECT * FROM users",
                        "SELECT * FROM purchase_orders",
                        "INSERT INTO video_sessions(ticket_hash) VALUES ('bad')",
                        "UPDATE webinar_videos SET active=false",
                        "CREATE TABLE forbidden(id int)",
                    ):
                        await rejects(connection, query, error=asyncpg.InsufficientPrivilegeError)
            finally:
                if edge_pool is not None:
                    await edge_pool.close()
                await app_pool.close()
                await migrator.close()
    finally:
        await admin.execute(f"DROP ROLE IF EXISTS {role}")
        await admin.close()


@pytest.mark.asyncio
async def test_booking_notification_is_atomic_claimed_once_and_fenced():
    async with isolated_database() as name:
        assert_upgrade(name, "upgrade", "head")
        migrator = await connect(name)
        pool = await asyncpg.create_pool(database_settings(name, "gl_api_app").database_url())
        try:
            user_id = await migrator.fetchval(
                "INSERT INTO users(telegram_id) VALUES ($1) RETURNING id", -9_100_000_777
            )
            service_id = await migrator.fetchval(
                "INSERT INTO services(title) VALUES ('Synthetic outbox') RETURNING id"
            )
            slot_id = await migrator.fetchval(
                """INSERT INTO appointment_slots(service_id, starts_at, ends_at)
                VALUES ($1, now() + interval '2 days', now() + interval '2 days 1 hour')
                RETURNING id""",
                service_id,
            )
            booking_id = await migrator.fetchval(
                "INSERT INTO booking_requests(user_id, slot_id, source_update_id) "
                "VALUES ($1, $2, $3) RETURNING id",
                user_id,
                slot_id,
                9_100_000_777,
            )
            repo = Repository(pool)
            await migrator.execute(
                "INSERT INTO booking_notification_outbox(booking_id) VALUES ($1)", booking_id
            )
            with pytest.raises(asyncpg.UniqueViolationError):
                await repo.decide_booking(booking_id, "confirmed")
            assert (
                await migrator.fetchval(
                    "SELECT status FROM booking_requests WHERE id = $1", booking_id
                )
                == "pending"
            )
            await migrator.execute(
                "DELETE FROM booking_notification_outbox WHERE booking_id = $1", booking_id
            )
            assert (await repo.decide_booking(booking_id, "confirmed"))["id"] == booking_id
            assert await repo.decide_booking(booking_id, "declined") is None
            claims = await asyncio.gather(
                repo.claim_notification(booking_id), repo.claim_notification(booking_id)
            )
            claimed = next(item for item in claims if item is not None)
            assert sum(item is not None for item in claims) == 1
            assert claimed["telegram_id"] == -9_100_000_777
            assert await repo.complete_notification(claimed["id"], 0, 11) is False
            assert await repo.retry_notification(claimed["id"], claimed["attempts"])
            assert await repo.claim_notification(booking_id) is None
            await migrator.execute(
                "UPDATE booking_notification_outbox SET available_at = now() - interval '1 second'"
            )
            retried = await repo.claim_notification(booking_id)
            assert retried["attempts"] == claimed["attempts"] + 1
            assert (
                await repo.complete_notification(claimed["id"], claimed["attempts"], 11) is False
            )
            assert await repo.complete_notification(retried["id"], retried["attempts"], 12)
            assert await repo.claim_notification(booking_id) is None
            assert await migrator.fetchval(
                "SELECT client_notified_at IS NOT NULL FROM booking_requests WHERE id = $1",
                booking_id,
            )
            assert (
                await migrator.fetchval(
                    "SELECT telegram_message_id FROM booking_notification_outbox WHERE booking_id = $1",
                    booking_id,
                )
                == 12
            )
            second_slot = await migrator.fetchval(
                """INSERT INTO appointment_slots(service_id, starts_at, ends_at)
                VALUES ($1, now() + interval '3 days', now() + interval '3 days 1 hour')
                RETURNING id""",
                service_id,
            )
            second_booking = await migrator.fetchval(
                "INSERT INTO booking_requests(user_id, slot_id, source_update_id) "
                "VALUES ($1, $2, $3) RETURNING id",
                user_id,
                second_slot,
                9_100_000_778,
            )
            assert await repo.decide_booking(second_booking, "declined")
            await migrator.execute(
                "UPDATE booking_notification_outbox SET attempts = 4 WHERE booking_id = $1",
                second_booking,
            )
            last_attempt = await repo.claim_notification(second_booking)
            assert last_attempt["attempts"] == 5
            assert await repo.retry_notification(last_attempt["id"], 5)
            assert (
                await migrator.fetchval(
                    "SELECT status FROM booking_notification_outbox WHERE booking_id = $1",
                    second_booking,
                )
                == "needs_review"
            )
            assert await repo.claim_notification(second_booking) is None
        finally:
            await pool.close()
            await migrator.close()


@pytest.mark.asyncio
async def test_fresh_upgrade_is_repeatable_and_keeps_data():
    assert LEGACY_SQL.read_bytes() == FROZEN_LEGACY_SQL.read_bytes()
    async with isolated_database() as name:
        assert_upgrade(name, "upgrade", "0005_booking_notification_outbox")
        connection = await connect(name)
        try:
            tables = {
                row["tablename"]
                for row in await connection.fetch(
                    "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
                )
            }
            assert tables == EXPECTED_TABLES - {"order_intent_requests"}
            assert {
                (row["source"], row["target"])
                for row in await connection.fetch(
                    "SELECT conrelid::regclass::text AS source, "
                    "confrelid::regclass::text AS target "
                    "FROM pg_constraint WHERE contype = 'f'"
                )
            } == EXPECTED_FOREIGN_KEYS - {("order_intent_requests", "purchase_orders")}
            user_id = await connection.fetchval(
                "INSERT INTO users(telegram_id) VALUES ($1) RETURNING id", -9_100_000_001
            )
            webinar_id = await connection.fetchval(
                "INSERT INTO webinars(title, price_kopeks) VALUES ($1, $2) RETURNING id",
                "Synthetic migration sentinel",
                12300,
            )
            order_id = await connection.fetchval(
                "INSERT INTO purchase_orders(user_id, webinar_id, amount_kopeks) "
                "VALUES ($1, $2, $3) RETURNING id",
                user_id,
                webinar_id,
                12300,
            )
            assert_upgrade(name, "upgrade", "head")
            assert_upgrade(name, "upgrade", "head")
            assert await connection.fetchval("SELECT version_num FROM alembic_version") == (
                "0011_recording_codes"
            )
            assert (
                await connection.fetchval(
                    "SELECT amount_kopeks FROM purchase_orders WHERE id = $1", order_id
                )
                == 12300
            )
            assert (
                await connection.fetchval(
                    "SELECT source_update_id FROM purchase_orders WHERE id = $1", order_id
                )
                is None
            )
            assert await connection.fetchval(
                "SELECT telegram_id FROM users WHERE id = $1", user_id
            ) == (-9_100_000_001)
            assert {
                row["tablename"]
                for row in await connection.fetch(
                    "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
                )
            } == EXPECTED_TABLES | {
                "webinar_videos",
                "video_sessions",
                "atol_payment_attempts",
                "payment_receipts",
                "receipt_observations",
            }
        finally:
            await connection.close()


@pytest.mark.asyncio
async def test_order_request_migration_preserves_old_mapping_and_rejects_duplicate_pending():
    async with isolated_database() as name:
        assert_upgrade(name, "upgrade", "0006_order_request_idempotency")
        connection = await connect(name)
        try:
            user_id = await connection.fetchval(
                "INSERT INTO users(telegram_id) VALUES (-9100000099) RETURNING id"
            )
            webinar_id = await connection.fetchval(
                "INSERT INTO webinars(title, price_kopeks) VALUES ('Synthetic migration', 100) RETURNING id"
            )
            order_id = await connection.fetchval(
                "INSERT INTO purchase_orders(user_id, webinar_id, amount_kopeks, source_update_id) "
                "VALUES ($1, $2, 100, 9100000099) RETURNING id",
                user_id,
                webinar_id,
            )
            assert_upgrade(name, "upgrade", "head")
            assert (
                await connection.fetchval(
                    "SELECT order_id FROM order_intent_requests WHERE source_update_id = 9100000099"
                )
                == order_id
            )
            await rejects(
                connection,
                "INSERT INTO purchase_orders(user_id, webinar_id, amount_kopeks) "
                "VALUES ($1, $2, 100)",
                user_id,
                webinar_id,
                error=asyncpg.UniqueViolationError,
            )
            await connection.execute(
                "UPDATE purchase_orders SET status = 'cancelled' WHERE id = $1", order_id
            )
            assert (
                await connection.fetchval(
                    "INSERT INTO purchase_orders(user_id, webinar_id, amount_kopeks) "
                    "VALUES ($1, $2, 100) RETURNING id",
                    user_id,
                    webinar_id,
                )
                != order_id
            )
        finally:
            await connection.close()


@pytest.mark.asyncio
async def test_duplicate_old_pending_orders_stop_upgrade_without_deleting_rows():
    async with isolated_database() as name:
        assert_upgrade(name, "upgrade", "0006_order_request_idempotency")
        connection = await connect(name)
        try:
            user_id = await connection.fetchval(
                "INSERT INTO users(telegram_id) VALUES (-9100000100) RETURNING id"
            )
            webinar_id = await connection.fetchval(
                "INSERT INTO webinars(title, price_kopeks) VALUES ('Synthetic duplicate', 100) RETURNING id"
            )
            for update_id in (9100000100, 9100000101):
                await connection.execute(
                    "INSERT INTO purchase_orders(user_id, webinar_id, amount_kopeks, source_update_id) "
                    "VALUES ($1, $2, 100, $3)",
                    user_id,
                    webinar_id,
                    update_id,
                )
            result = alembic(name, "upgrade", "head")
            assert result.returncode != 0
            assert_no_passwords_in_output(result)
            assert "Duplicate pending orders require manual reconciliation" in result.stderr
            assert str(-9_100_000_100) not in result.stderr
            assert await connection.fetchval("SELECT version_num FROM alembic_version") == (
                "0006_order_request_idempotency"
            )
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM purchase_orders WHERE user_id = $1", user_id
                )
                == 2
            )
            assert (
                await connection.fetchval("SELECT to_regclass('public.order_intent_requests')")
                is None
            )
        finally:
            await connection.close()


@pytest.mark.asyncio
async def test_unstamped_legacy_schema_is_refused_without_data_loss():
    async with isolated_database() as name:
        connection = await connect(name)
        try:
            await connection.execute(LEGACY_SQL.read_text())
            await connection.execute("INSERT INTO users(telegram_id) VALUES ($1)", -9_100_000_002)
            result = alembic(name, "upgrade", "head")
            assert result.returncode != 0
            assert_no_passwords_in_output(result)
            assert "Existing bot tables detected" in result.stderr
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM users WHERE telegram_id = $1", -9_100_000_002
                )
                == 1
            )
            assert (
                await connection.fetchval("SELECT to_regclass('public.purchase_orders')") is None
            )
        finally:
            await connection.close()


@pytest.mark.asyncio
async def test_verified_legacy_stamp_then_upgrade_preserves_rows():
    async with isolated_database() as name:
        connection = await connect(name)
        try:
            await connection.execute(LEGACY_SQL.read_text())
            user_id = await connection.fetchval(
                "INSERT INTO users(telegram_id) VALUES ($1) RETURNING id", -9_100_000_003
            )
            webinar_id = await connection.fetchval(
                "INSERT INTO webinars(title, price_kopeks) VALUES ($1, $2) RETURNING id",
                "Synthetic legacy webinar",
                5000,
            )
            await connection.execute(
                "INSERT INTO webinar_entitlements(user_id, webinar_id, source) "
                "VALUES ($1, $2, 'imported')",
                user_id,
                webinar_id,
            )
            assert_upgrade(name, "stamp", "0001_legacy_bot_schema")
            assert_upgrade(name, "upgrade", "head")
            assert_upgrade(name, "upgrade", "head")
            assert (
                await connection.fetchval(
                    "SELECT source FROM webinar_entitlements WHERE user_id = $1 AND webinar_id = $2",
                    user_id,
                    webinar_id,
                )
                == "imported"
            )
            assert await connection.fetchval("SELECT to_regclass('public.payment_events')")
            assert await connection.fetchval("SELECT version_num FROM alembic_version") == (
                "0011_recording_codes"
            )
        finally:
            await connection.close()


@pytest.mark.asyncio
async def test_legacy_tables_owned_by_another_role_fail_without_data_loss():
    async with isolated_database() as name:
        owner = await connect(name, "postgres")
        migrator = await connect(name)
        try:
            await owner.execute(LEGACY_SQL.read_text())
            await owner.execute("INSERT INTO users(telegram_id) VALUES ($1)", -9_100_000_030)
            assert_upgrade(name, "stamp", "0001_legacy_bot_schema")
            result = alembic(name, "upgrade", "head")
            assert result.returncode != 0
            assert_no_passwords_in_output(result)
            assert (
                await owner.fetchval(
                    "SELECT count(*) FROM users WHERE telegram_id = $1", -9_100_000_030
                )
                == 1
            )
            assert await migrator.fetchval("SELECT version_num FROM alembic_version") == (
                "0001_legacy_bot_schema"
            )
            assert await migrator.fetchval("SELECT to_regclass('public.purchase_orders')") is None
        finally:
            await owner.close()
            await migrator.close()


@pytest.mark.asyncio
async def test_failed_upgrade_rolls_back_only_its_changes():
    async with isolated_database() as name:
        connection = await connect(name)
        try:
            await connection.execute(LEGACY_SQL.read_text())
            await connection.execute("CREATE TABLE payment_events (sentinel TEXT NOT NULL)")
            await connection.execute("INSERT INTO payment_events VALUES ('keep-me')")
            assert_upgrade(name, "stamp", "0001_legacy_bot_schema")
            result = alembic(name, "upgrade", "head")
            assert result.returncode != 0
            assert_no_passwords_in_output(result)
            assert await connection.fetchval("SELECT version_num FROM alembic_version") == (
                "0001_legacy_bot_schema"
            )
            assert (
                await connection.fetchval("SELECT to_regclass('public.purchase_orders')") is None
            )
            assert await connection.fetchval("SELECT sentinel FROM payment_events") == "keep-me"
        finally:
            await connection.close()


@pytest.mark.asyncio
async def test_fresh_upgrade_failure_does_not_leave_unversioned_schema():
    async with isolated_database() as name:
        connection = await connect(name)
        try:
            await connection.execute("CREATE TABLE payment_events (sentinel TEXT NOT NULL)")
            await connection.execute("INSERT INTO payment_events VALUES ('keep-me')")
            result = alembic(name, "upgrade", "head")
            assert result.returncode != 0
            assert_no_passwords_in_output(result)
            assert await connection.fetchval("SELECT sentinel FROM payment_events") == "keep-me"
            assert await connection.fetchval("SELECT to_regclass('public.users')") is None
            assert (
                await connection.fetchval("SELECT to_regclass('public.purchase_orders')") is None
            )
            assert (
                await connection.fetchval("SELECT to_regclass('public.alembic_version')") is None
            )
        finally:
            await connection.close()


@pytest.mark.asyncio
async def test_downgrade_refuses_to_drop_tables_or_rows():
    async with isolated_database() as name:
        assert_upgrade(name, "upgrade", "head")
        connection = await connect(name)
        try:
            await connection.execute("INSERT INTO users(telegram_id) VALUES ($1)", -9_100_000_004)
            result = alembic(name, "downgrade", "-1")
            assert result.returncode != 0
            assert_no_passwords_in_output(result)
            assert await connection.fetchval("SELECT version_num FROM alembic_version") == (
                "0011_recording_codes"
            )
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM users WHERE telegram_id = $1", -9_100_000_004
                )
                == 1
            )
        finally:
            await connection.close()


@pytest.mark.asyncio
async def test_pre_payment_runtime_role_cannot_confirm_payment_or_grant_access():
    async with isolated_database() as name:
        assert_upgrade(name, "upgrade", "0008_video_sessions")
        app = await connect(name, "gl_api_app")
        admin = await connect(name, "postgres")
        migrator = await connect(name)
        try:
            expected_privileges = {
                "users": {"SELECT", "INSERT", "UPDATE"},
                "user_sections": {"SELECT", "INSERT", "UPDATE"},
                "services": {"SELECT"},
                "appointment_slots": {"SELECT"},
                "booking_requests": {"SELECT", "INSERT", "UPDATE"},
                "booking_notification_outbox": {"SELECT", "INSERT", "UPDATE"},
                "products": {"SELECT"},
                "webinars": {"SELECT"},
                "webinar_entitlements": {"SELECT"},
                "processed_updates": {"SELECT", "INSERT", "UPDATE", "DELETE"},
                "purchase_orders": {"SELECT"},
                "order_intent_requests": {"SELECT", "INSERT"},
                "payment_events": set(),
                "alembic_version": set(),
            }
            for table, allowed in expected_privileges.items():
                for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                    assert await app.fetchval(
                        "SELECT has_table_privilege(current_user, $1::text, $2::text)",
                        f"public.{table}",
                        privilege,
                    ) is (privilege in allowed), (table, privilege)
            for sequence in (
                "users_id_seq",
                "booking_requests_id_seq",
                "booking_notification_outbox_id_seq",
                "webinars_id_seq",
                "purchase_orders_id_seq",
                "payment_events_id_seq",
            ):
                assert await app.fetchval(
                    "SELECT has_sequence_privilege(current_user, $1::text, 'USAGE')",
                    f"public.{sequence}",
                ) is (
                    sequence
                    in {
                        "users_id_seq",
                        "booking_requests_id_seq",
                        "booking_notification_outbox_id_seq",
                        "purchase_orders_id_seq",
                    }
                ), sequence
            assert not await app.fetchval(
                "SELECT has_schema_privilege(current_user, 'public', 'CREATE')"
            )
            assert (
                await app.fetchval(
                    "INSERT INTO users(telegram_id) VALUES ($1) RETURNING telegram_id",
                    -9_100_000_005,
                )
                == -9_100_000_005
            )
            await rejects(
                app,
                "UPDATE purchase_orders SET status = 'paid' WHERE false",
                error=asyncpg.InsufficientPrivilegeError,
            )
            await rejects(
                app,
                "UPDATE order_intent_requests SET order_id = 1 WHERE false",
                error=asyncpg.InsufficientPrivilegeError,
            )
            for column in ("user_id", "webinar_id", "amount_kopeks", "source_update_id"):
                assert await app.fetchval(
                    "SELECT has_column_privilege(current_user, 'public.purchase_orders', $1, 'INSERT')",
                    column,
                )
            for column in ("status", "currency", "paid_at"):
                assert not await app.fetchval(
                    "SELECT has_column_privilege(current_user, 'public.purchase_orders', $1, 'INSERT')",
                    column,
                )
            await rejects(
                app,
                "INSERT INTO purchase_orders(user_id, webinar_id, amount_kopeks, status) "
                "VALUES (1, 1, 100, 'paid')",
                error=asyncpg.InsufficientPrivilegeError,
            )
            await rejects(
                app,
                "INSERT INTO payment_events(provider, provider_event_id, order_id, event_type) "
                "VALUES ('synthetic', 'forbidden', 1, 'paid')",
                error=asyncpg.InsufficientPrivilegeError,
            )
            await rejects(
                app,
                "INSERT INTO webinar_entitlements(user_id, webinar_id, source) "
                "VALUES (1, 1, 'paid')",
                error=asyncpg.InsufficientPrivilegeError,
            )
            assert (
                await admin.fetchval(
                    "SELECT rolsuper OR rolcreaterole FROM pg_roles WHERE rolname = 'gl_api_app'"
                )
                is False
            )
            assert (
                await admin.fetchval(
                    "SELECT rolsuper OR rolcreaterole FROM pg_roles WHERE rolname = 'gl_api_migrator'"
                )
                is False
            )
            await migrator.execute("CREATE TABLE future_private_record (id INTEGER PRIMARY KEY)")
            await rejects(
                app,
                "SELECT * FROM future_private_record",
                error=asyncpg.InsufficientPrivilegeError,
            )
        finally:
            await app.close()
            await admin.close()
            await migrator.close()


@pytest.mark.asyncio
async def test_versioned_grants_change_from_catalog_read_to_bot_runtime():
    async with isolated_database() as name:
        assert_upgrade(name, "upgrade", "0002_purchase_records")
        app = await connect(name, "gl_api_app")
        try:
            assert not await app.fetchval(
                "SELECT has_table_privilege(current_user, 'public.webinars', 'SELECT')"
            )
            assert_upgrade(name, "upgrade", "0003_runtime_read_privileges")
            assert await app.fetchval(
                "SELECT has_table_privilege(current_user, 'public.webinars', 'SELECT')"
            )
            assert not await app.fetchval(
                "SELECT has_table_privilege(current_user, 'public.users', 'SELECT')"
            )
            assert_upgrade(name, "upgrade", "head")
            assert await app.fetchval(
                "SELECT has_table_privilege(current_user, 'public.users', 'SELECT')"
            )
            assert await app.fetchval(
                "SELECT has_table_privilege(current_user, 'public.users', 'INSERT')"
            )
            assert not await app.fetchval(
                "SELECT has_table_privilege(current_user, 'public.purchase_orders', 'INSERT')"
            )
            assert await app.fetchval(
                "SELECT has_table_privilege(current_user, 'public.payment_events', 'INSERT')"
            )
        finally:
            await app.close()


@pytest.mark.asyncio
async def test_constraints_and_foreign_keys_reject_invalid_records():
    async with isolated_database() as name:
        assert_upgrade(name, "upgrade", "head")
        connection = await connect(name)
        try:
            user_id = await connection.fetchval(
                "INSERT INTO users(telegram_id) VALUES ($1) RETURNING id", -9_100_000_006
            )
            webinar_id = await connection.fetchval(
                "INSERT INTO webinars(title, price_kopeks) VALUES ($1, 100) RETURNING id",
                "Synthetic",
            )
            service_id = await connection.fetchval(
                "INSERT INTO services(title, active) VALUES ($1, true) RETURNING id", "Synthetic"
            )
            await rejects(
                connection,
                "INSERT INTO users(telegram_id) VALUES ($1)",
                -9_100_000_006,
                error=asyncpg.UniqueViolationError,
            )
            await rejects(
                connection,
                "INSERT INTO user_sections(user_id, section) VALUES ($1, 'admin')",
                user_id,
                error=asyncpg.CheckViolationError,
            )
            await rejects(
                connection,
                "INSERT INTO user_sections(user_id, section) VALUES ($1, 'pro')",
                user_id + 1_000_000,
                error=asyncpg.ForeignKeyViolationError,
            )
            await rejects(
                connection,
                "INSERT INTO webinar_entitlements(user_id, webinar_id, source) "
                "VALUES ($1, $2, 'gift')",
                user_id,
                webinar_id,
                error=asyncpg.CheckViolationError,
            )
            await connection.execute(
                "INSERT INTO webinar_entitlements(user_id, webinar_id, source) "
                "VALUES ($1, $2, 'imported')",
                user_id,
                webinar_id,
            )
            await rejects(
                connection,
                "INSERT INTO webinar_entitlements(user_id, webinar_id, source) "
                "VALUES ($1, $2, 'paid')",
                user_id,
                webinar_id,
                error=asyncpg.UniqueViolationError,
            )
            await rejects(
                connection,
                "INSERT INTO webinars(title, price_kopeks) VALUES ($1, -1)",
                "Invalid price",
                error=asyncpg.CheckViolationError,
            )
            await rejects(
                connection,
                "INSERT INTO appointment_slots(service_id, starts_at, ends_at) "
                "VALUES ($1, $2, $3)",
                service_id,
                datetime.now(UTC) + timedelta(days=1),
                datetime.now(UTC),
                error=asyncpg.CheckViolationError,
            )
            await rejects(
                connection,
                "INSERT INTO purchase_orders(user_id, webinar_id, amount_kopeks) "
                "VALUES ($1, $2, -1)",
                user_id,
                webinar_id,
                error=asyncpg.CheckViolationError,
            )
            await rejects(
                connection,
                "INSERT INTO purchase_orders(user_id, webinar_id, amount_kopeks, status) "
                "VALUES ($1, $2, 100, 'unknown')",
                user_id,
                webinar_id,
                error=asyncpg.CheckViolationError,
            )
            await rejects(
                connection,
                "INSERT INTO purchase_orders(user_id, webinar_id, amount_kopeks, currency) "
                "VALUES ($1, $2, 100, 'USD')",
                user_id,
                webinar_id,
                error=asyncpg.CheckViolationError,
            )
            await rejects(
                connection,
                "INSERT INTO purchase_orders(user_id, webinar_id, amount_kopeks) "
                "VALUES ($1, $2, 100)",
                user_id + 1_000_000,
                webinar_id,
                error=asyncpg.ForeignKeyViolationError,
            )
            await rejects(
                connection,
                "INSERT INTO purchase_orders(user_id, webinar_id, amount_kopeks, source_update_id) "
                "VALUES ($1, $2, 100, 0)",
                user_id,
                webinar_id,
                error=asyncpg.CheckViolationError,
            )
            await connection.execute(
                "INSERT INTO purchase_orders(user_id, webinar_id, amount_kopeks, source_update_id) "
                "VALUES ($1, $2, 100, 9100000006)",
                user_id,
                webinar_id,
            )
            await rejects(
                connection,
                "INSERT INTO purchase_orders(user_id, webinar_id, amount_kopeks, source_update_id) "
                "VALUES ($1, $2, 100, 9100000006)",
                user_id,
                webinar_id,
                error=asyncpg.UniqueViolationError,
            )
            await rejects(
                connection,
                "INSERT INTO processed_updates(update_id, status) VALUES ($1, 'lost')",
                9_100_000_006,
                error=asyncpg.CheckViolationError,
            )
            with pytest.raises(asyncpg.CheckViolationError):
                async with connection.transaction():
                    await connection.execute(
                        "INSERT INTO user_sections(user_id, section) VALUES ($1, 'pro')", user_id
                    )
                    await connection.execute(
                        "UPDATE users SET last_section = 'admin' WHERE id = $1", user_id
                    )
            assert not await connection.fetchval(
                "SELECT EXISTS(SELECT 1 FROM user_sections WHERE user_id = $1)", user_id
            )
            assert (
                await connection.fetchval("SELECT last_section FROM users WHERE id = $1", user_id)
                is None
            )
        finally:
            await connection.close()


@pytest.mark.asyncio
async def test_concurrent_booking_is_unique_and_declined_slot_reopens():
    async with isolated_database() as name:
        assert_upgrade(name, "upgrade", "head")
        seed = await connect(name)
        try:
            users = [
                await seed.fetchval(
                    "INSERT INTO users(telegram_id) VALUES ($1) RETURNING id", -9_100_000_010 - i
                )
                for i in range(2)
            ]
            service_id = await seed.fetchval(
                "INSERT INTO services(title, active) VALUES ($1, true) RETURNING id", "Synthetic"
            )
            slot_id = await seed.fetchval(
                "INSERT INTO appointment_slots(service_id, starts_at, ends_at) "
                "VALUES ($1, now() + interval '1 day', now() + interval '1 day 1 hour') "
                "RETURNING id",
                service_id,
            )
        finally:
            await seed.close()

        async def book(user_id: int, update_id: int) -> tuple[bool, int | None]:
            connection = await connect(name)
            try:
                booking_id = await connection.fetchval(
                    "INSERT INTO booking_requests(user_id, slot_id, source_update_id) "
                    "VALUES ($1, $2, $3) RETURNING id",
                    user_id,
                    slot_id,
                    update_id,
                )
                return True, booking_id
            except asyncpg.UniqueViolationError:
                return False, None
            finally:
                await connection.close()

        outcomes = await asyncio.gather(
            book(users[0], 9_100_000_010), book(users[1], 9_100_000_011)
        )
        assert sorted(success for success, _ in outcomes) == [False, True]
        winner_id = next(booking_id for success, booking_id in outcomes if success)
        connection = await connect(name)
        try:
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM booking_requests WHERE slot_id = $1", slot_id
                )
                == 1
            )
            await connection.execute(
                "UPDATE booking_requests SET status = 'declined' WHERE id = $1", winner_id
            )
            assert await connection.fetchval(
                "INSERT INTO booking_requests(user_id, slot_id, source_update_id) "
                "VALUES ($1, $2, $3) RETURNING id",
                users[0],
                slot_id,
                9_100_000_012,
            )
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM booking_requests "
                    "WHERE slot_id = $1 AND status IN ('pending', 'confirmed')",
                    slot_id,
                )
                == 1
            )
        finally:
            await connection.close()


@pytest.mark.asyncio
async def test_concurrent_payment_event_keys_are_unique():
    async with isolated_database() as name:
        assert_upgrade(name, "upgrade", "head")
        seed = await connect(name)
        try:
            user_id = await seed.fetchval(
                "INSERT INTO users(telegram_id) VALUES ($1) RETURNING id", -9_100_000_020
            )
            webinar_id = await seed.fetchval(
                "INSERT INTO webinars(title, price_kopeks) VALUES ($1, 100) RETURNING id",
                "Synthetic",
            )
            order_id = await seed.fetchval(
                "INSERT INTO purchase_orders(user_id, webinar_id, amount_kopeks) "
                "VALUES ($1, $2, 100) RETURNING id",
                user_id,
                webinar_id,
            )
        finally:
            await seed.close()

        async def record_event() -> bool:
            connection = await connect(name)
            try:
                await connection.execute(
                    "INSERT INTO payment_events(provider, provider_event_id, order_id, event_type) "
                    "VALUES ('synthetic', 'same-event', $1, 'confirmed')",
                    order_id,
                )
                return True
            except asyncpg.UniqueViolationError:
                return False
            finally:
                await connection.close()

        assert sorted(await asyncio.gather(record_event(), record_event())) == [False, True]
        connection = await connect(name)
        try:
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM payment_events WHERE order_id = $1", order_id
                )
                == 1
            )
        finally:
            await connection.close()


@pytest.mark.asyncio
async def test_configured_roles_receive_only_their_database_privileges():
    """A new managed environment must not grant privileges to historical roles."""
    suffix = uuid4().hex
    name = f"gl_test_{suffix}"
    app_role = f"test_app_{suffix}"
    migration_role = f"test_migrate_{suffix}"
    admin = await connect("postgres", "postgres")
    password_file = Path(os.environ["GL_API_TEST_MIGRATOR_PASSWORD_FILE"])
    literal = await admin.fetchval(
        "SELECT quote_literal($1)", read_secret(password_file, "synthetic migration password file")
    )
    created_roles = []
    created_database = False
    try:
        for role in (migration_role, app_role):
            await admin.execute(f'CREATE ROLE "{role}" LOGIN PASSWORD {literal}')
            created_roles.append(role)
        await admin.execute(f'CREATE DATABASE "{name}" OWNER "{migration_role}"')
        created_database = True
        await admin.execute(f'REVOKE CONNECT ON DATABASE "{name}" FROM PUBLIC')
        await admin.execute(f'GRANT CONNECT ON DATABASE "{name}" TO "{app_role}"')
        database_admin = await connect(name, "postgres")
        try:
            await database_admin.execute("REVOKE ALL ON SCHEMA public FROM PUBLIC")
            await database_admin.execute(f'GRANT USAGE ON SCHEMA public TO "{app_role}"')
        finally:
            await database_admin.close()
        env = os.environ.copy()
        env.update(
            GL_API_DATABASE_NAME=name,
            GL_API_MIGRATION_DATABASE_USER=migration_role,
            GL_API_MIGRATION_APP_ROLE=app_role,
            GL_API_MIGRATION_DATABASE_PASSWORD_FILE=str(password_file),
        )
        for _ in range(2):
            result = await asyncio.to_thread(
                subprocess.run,
                [str(ROOT / ".venv/bin/alembic"), "upgrade", "head"],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                timeout=45,
                check=False,
            )
            assert result.returncode == 0, "configured-role migration failed"
            assert_no_passwords_in_output(result)
        app = await asyncpg.connect(
            Settings(
                database_host=os.environ["GL_API_DATABASE_HOST"],
                database_name=name,
                database_user=app_role,
                database_password_file=password_file,
            ).database_url()
        )
        try:
            assert await app.fetchval(
                "SELECT has_table_privilege(current_user, 'users', 'INSERT')"
            )
            assert await app.fetchval(
                "SELECT has_table_privilege(current_user, 'payment_events', 'INSERT')"
            )
            assert not await app.fetchval(
                "SELECT has_table_privilege('gl_api_app', 'users', 'INSERT')"
            )
            assert not await app.fetchval(
                "SELECT has_schema_privilege(current_user, 'public', 'CREATE')"
            )
            async with app.transaction():
                await app.execute("INSERT INTO users(telegram_id) VALUES ($1)", -9_109_777_001)
                await app.execute("DELETE FROM processed_updates WHERE update_id=$1", -900)
                await rejects(
                    app,
                    "CREATE TABLE forbidden(id integer)",
                    error=asyncpg.InsufficientPrivilegeError,
                )
        finally:
            await app.close()
    finally:
        if created_database:
            await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        for role in created_roles:
            await admin.execute(f'DROP ROLE "{role}"')
        await admin.close()


async def test_managed_acceptance_checks_payment_permissions_and_rejects_excess():
    import runpy

    verifier = runpy.run_path(str(ROOT / "infra/timeweb/verify-managed.py"))[
        "verify_payment_permissions"
    ]
    async with isolated_database() as name:
        assert_upgrade(name, "upgrade", "0008_video_sessions")
        app = await connect(name, "gl_api_app")
        try:
            await verifier(app)
        finally:
            await app.close()
        assert_upgrade(name, "upgrade", "head")
        app = await connect(name, "gl_api_app")
        migrator = await connect(name)
        try:
            await verifier(app)
            await migrator.execute("GRANT UPDATE(amount_kopeks) ON purchase_orders TO gl_api_app")
            with pytest.raises(AssertionError, match="unexpected order update permission"):
                await verifier(app)
            await migrator.execute(
                "REVOKE UPDATE(amount_kopeks) ON purchase_orders FROM gl_api_app"
            )
            await verifier(app)
            await migrator.execute("GRANT DELETE ON receipt_observations TO gl_api_app")
            with pytest.raises(AssertionError):
                await verifier(app)
        finally:
            await migrator.close()
            await app.close()


@pytest.mark.asyncio
async def test_public_recording_codes_preserve_rows_and_restrict_writer():
    async with isolated_database() as name:
        assert_upgrade(name, "upgrade", "0010_receipt_ledger")
        connection = await connect(name)
        try:
            wid = await connection.fetchval(
                "INSERT INTO webinars(title,price_kopeks) VALUES ('Synthetic preserved',1000) RETURNING id"
            )
            assert_upgrade(name, "upgrade", "head")
            assert_upgrade(name, "upgrade", "head")
            assert (
                await connection.fetchval("SELECT title FROM webinars WHERE id=$1", wid)
                == "Synthetic preserved"
            )
            assert (
                await connection.fetchval("SELECT public_code FROM webinars WHERE id=$1", wid)
                is None
            )
            await connection.execute(
                "UPDATE webinars SET public_code='synthetic_code' WHERE id=$1", wid
            )
            with pytest.raises(asyncpg.UniqueViolationError):
                await connection.execute(
                    "INSERT INTO webinars(title,public_code) VALUES ('Duplicate','synthetic_code')"
                )
            with pytest.raises(asyncpg.CheckViolationError):
                await connection.execute(
                    "UPDATE webinars SET public_code='../bad' WHERE id=$1", wid
                )
            application = await connect(name, "gl_api_app")
            try:
                assert (
                    await application.fetchval("SELECT public_code FROM webinars WHERE id=$1", wid)
                    == "synthetic_code"
                )
                with pytest.raises(asyncpg.InsufficientPrivilegeError):
                    await application.execute(
                        "UPDATE webinars SET public_code='other_code' WHERE id=$1", wid
                    )
            finally:
                await application.close()
        finally:
            await connection.close()
