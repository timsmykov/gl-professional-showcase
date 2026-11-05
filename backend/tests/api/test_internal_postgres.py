"""Internal bot HTTP contract against the isolated PostgreSQL 16 database."""

import asyncio
import os
from pathlib import Path

import asyncpg
import httpx
import pytest

from gl_api.app import create_app
from gl_api.config import Settings

pytestmark = pytest.mark.skipif(
    os.getenv("GL_API_TEST_DATABASE") != "1", reason="isolated PostgreSQL test container required"
)

TOKEN = "synthetic-internal-token-1234567890"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


def app_settings(tmp_path: Path) -> Settings:
    token_file = tmp_path / "internal-token"
    token_file.write_text(TOKEN)
    return Settings(bot_token_file=token_file)


async def migrator_connection() -> asyncpg.Connection:
    settings = Settings(
        database_user="gl_api_migrator",
        database_password_file=Path(os.environ["GL_API_TEST_MIGRATOR_PASSWORD_FILE"]),
    )
    return await asyncpg.connect(settings.database_url())


async def call(client: httpx.AsyncClient, operation: str, *args) -> httpx.Response:
    return await client.post(
        f"/internal/bot/{operation}", json={"args": list(args)}, headers=HEADERS
    )


async def result(client: httpx.AsyncClient, operation: str, *args):
    response = await call(client, operation, *args)
    assert response.status_code == 200, (operation, response.text)
    return response.json()["result"]


@pytest.mark.asyncio
async def test_profile_sections_catalog_and_entitlements_are_scoped(tmp_path: Path):
    migrator = await migrator_connection()
    user_ids: list[int] = []
    service_ids: list[int] = []
    product_ids: list[int] = []
    webinar_ids: list[int] = []
    app = create_app(app_settings(tmp_path))
    try:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client,
        ):
            assert (await client.get("/readyz")).status_code == 200
            assert await result(client, "health") is None
            first = await result(client, "get_user", -9_300_000_000_001)
            second = await result(client, "get_user", -9_300_000_000_002)
            user_ids.extend((first["id"], second["id"]))
            assert (await result(client, "get_user", -9_300_000_000_001))["id"] == first["id"]
            assert await result(client, "has_section", first["id"], "pro") is False
            assert await result(client, "select_section", first["id"], "pro") is None
            assert await result(client, "select_section", first["id"], "pro") is None
            assert await result(client, "select_section", first["id"], "client") is None
            assert await result(client, "has_section", first["id"], "pro") is True
            assert await result(client, "has_section", first["id"], "client") is True
            assert await result(client, "has_section", second["id"], "pro") is False
            assert (await result(client, "get_user", -9_300_000_000_001))[
                "last_section"
            ] == "client"
            assert (
                await migrator.fetchval(
                    "SELECT count(*) FROM user_sections WHERE user_id = $1", first["id"]
                )
                == 2
            )

            active_service = await migrator.fetchval(
                "INSERT INTO services(title, description, active) "
                "VALUES ('Synthetic active service', 'Synthetic service detail', true) RETURNING id"
            )
            inactive_service = await migrator.fetchval(
                "INSERT INTO services(title, active) "
                "VALUES ('Synthetic hidden service', false) RETURNING id"
            )
            service_ids.extend((active_service, inactive_service))
            active_product = await migrator.fetchval(
                "INSERT INTO products(title, description, active) "
                "VALUES ('Synthetic active product', 'Synthetic product detail', true) RETURNING id"
            )
            inactive_product = await migrator.fetchval(
                "INSERT INTO products(title, active) "
                "VALUES ('Synthetic hidden product', false) RETURNING id"
            )
            product_ids.extend((active_product, inactive_product))
            active_webinar = await migrator.fetchval(
                "INSERT INTO webinars(title, description, price_kopeks, active) "
                "VALUES ('Synthetic active webinar', 'Synthetic webinar detail', 1500, true) "
                "RETURNING id"
            )
            inactive_webinar = await migrator.fetchval(
                "INSERT INTO webinars(title, active) "
                "VALUES ('Synthetic hidden webinar', false) RETURNING id"
            )
            webinar_ids.extend((active_webinar, inactive_webinar))
            await migrator.execute(
                "INSERT INTO webinar_entitlements(user_id, webinar_id, source) "
                "VALUES ($1, $2, 'imported')",
                first["id"],
                active_webinar,
            )

            assert active_service in {item["id"] for item in await result(client, "list_services")}
            assert inactive_service not in {
                item["id"] for item in await result(client, "list_services")
            }
            assert (await result(client, "get_service", active_service))["description"] == (
                "Synthetic service detail"
            )
            assert await result(client, "get_service", inactive_service) is None
            assert active_product in {item["id"] for item in await result(client, "list_products")}
            assert inactive_product not in {
                item["id"] for item in await result(client, "list_products")
            }
            assert (await result(client, "get_product", active_product))["description"] == (
                "Synthetic product detail"
            )
            assert await result(client, "get_product", inactive_product) is None
            assert active_webinar in {item["id"] for item in await result(client, "list_webinars")}
            assert inactive_webinar not in {
                item["id"] for item in await result(client, "list_webinars")
            }
            assert (await result(client, "get_webinar", active_webinar))["price_kopeks"] == 1500
            assert await result(client, "get_webinar", inactive_webinar) is None
            assert await result(client, "list_entitlements", first["id"]) == [
                {"title": "Synthetic active webinar", "source": "imported"}
            ]
            assert await result(client, "list_entitlements", second["id"]) == []
            assert (
                await call(client, "grant_entitlement", first["id"], active_webinar)
            ).status_code == 404
            assert (await call(client, "mark_paid", active_webinar)).status_code == 404
    finally:
        if user_ids:
            await migrator.execute(
                "DELETE FROM webinar_entitlements WHERE user_id = ANY($1::bigint[])", user_ids
            )
            await migrator.execute("DELETE FROM users WHERE id = ANY($1::bigint[])", user_ids)
        if webinar_ids:
            await migrator.execute(
                "DELETE FROM webinars WHERE id = ANY($1::bigint[])", webinar_ids
            )
        if product_ids:
            await migrator.execute(
                "DELETE FROM products WHERE id = ANY($1::bigint[])", product_ids
            )
        if service_ids:
            await migrator.execute(
                "DELETE FROM services WHERE id = ANY($1::bigint[])", service_ids
            )
        await migrator.close()


@pytest.mark.asyncio
async def test_bad_user_reference_is_not_reported_as_database_outage(tmp_path: Path, caplog):
    migrator = await migrator_connection()
    service_id = slot_id = None
    try:
        service_id = await migrator.fetchval(
            "INSERT INTO services(title, active) VALUES ('Synthetic reference service', true) "
            "RETURNING id"
        )
        slot_id = await migrator.fetchval(
            "INSERT INTO appointment_slots(service_id, starts_at, ends_at) "
            "VALUES ($1, now() + interval '1 day', now() + interval '1 day 1 hour') "
            "RETURNING id",
            service_id,
        )
        app = create_app(app_settings(tmp_path))
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client,
        ):
            missing_user = 2**63 - 1
            for operation, args in (
                ("select_section", (missing_user, "pro")),
                ("create_booking", (missing_user, slot_id, 9_300_000_000_020)),
            ):
                response = await call(client, operation, *args)
                assert response.status_code == 422
                assert response.json() == {"detail": "invalid reference"}
        assert (
            await migrator.fetchval(
                "SELECT count(*) FROM booking_requests WHERE slot_id = $1", slot_id
            )
            == 0
        )
        assert (
            await migrator.fetchval(
                "SELECT count(*) FROM user_sections WHERE user_id = $1", 2**63 - 1
            )
            == 0
        )
        assert TOKEN not in caplog.text
        assert str(2**63 - 1) not in caplog.text
    finally:
        if slot_id is not None:
            await migrator.execute("DELETE FROM appointment_slots WHERE id = $1", slot_id)
        if service_id is not None:
            await migrator.execute("DELETE FROM services WHERE id = $1", service_id)
        await migrator.close()


@pytest.mark.asyncio
async def test_update_deduplication_and_booking_lifecycle(tmp_path: Path):
    migrator = await migrator_connection()
    user_ids: list[int] = []
    service_ids: list[int] = []
    slot_ids: list[int] = []
    update_ids = [9_300_000_000_031, 9_300_000_000_032, 9_300_000_000_033]
    app = create_app(app_settings(tmp_path))
    try:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client,
        ):
            first = await result(client, "get_user", -9_300_000_000_031)
            second = await result(client, "get_user", -9_300_000_000_032)
            user_ids.extend((first["id"], second["id"]))

            outcomes = await asyncio.gather(
                result(client, "claim_update", update_ids[0]),
                result(client, "claim_update", update_ids[0]),
            )
            assert "busy" in outcomes
            first_lease = next(item["lease"] for item in outcomes if isinstance(item, dict))
            assert await result(client, "complete_update", update_ids[0], first_lease) is True
            assert await result(client, "claim_update", update_ids[0]) == "done"
            await result(client, "release_update", update_ids[0], first_lease)
            assert await result(client, "claim_update", update_ids[0]) == "done"
            first_update_claim = await result(client, "claim_update", update_ids[1])
            assert first_update_claim["status"] == "claimed"
            await result(client, "release_update", update_ids[1], first_update_claim["lease"])
            second_update_claim = await result(client, "claim_update", update_ids[1])
            assert second_update_claim["status"] == "claimed"
            await migrator.execute(
                "UPDATE processed_updates SET claimed_at = now() - interval '3 minutes' "
                "WHERE update_id = $1",
                update_ids[1],
            )
            replacement = await result(client, "claim_update", update_ids[1])
            assert replacement["status"] == "claimed"
            assert replacement["lease"] != second_update_claim["lease"]
            await result(client, "release_update", update_ids[1], second_update_claim["lease"])
            assert (
                await result(
                    client, "complete_update", update_ids[1], second_update_claim["lease"]
                )
                is False
            )
            assert await result(client, "claim_update", update_ids[1]) == "busy"
            assert await result(client, "complete_update", update_ids[1], replacement["lease"])
            assert await result(client, "claim_update", update_ids[1]) == "done"
            assert (
                await migrator.fetchval(
                    "SELECT count(*) FROM processed_updates WHERE update_id = $1", update_ids[1]
                )
                == 1
            )

            active_service = await migrator.fetchval(
                "INSERT INTO services(title, active) "
                "VALUES ('Synthetic booking service', true) RETURNING id"
            )
            inactive_service = await migrator.fetchval(
                "INSERT INTO services(title, active) "
                "VALUES ('Synthetic inactive service', false) RETURNING id"
            )
            service_ids.extend((active_service, inactive_service))
            primary_slot = await migrator.fetchval(
                "INSERT INTO appointment_slots(service_id, starts_at, ends_at) "
                "VALUES ($1, now() + interval '2 days', now() + interval '2 days 1 hour') "
                "RETURNING id",
                active_service,
            )
            declined_slot = await migrator.fetchval(
                "INSERT INTO appointment_slots(service_id, starts_at, ends_at) "
                "VALUES ($1, now() + interval '3 days', now() + interval '3 days 1 hour') "
                "RETURNING id",
                active_service,
            )
            race_slot = await migrator.fetchval(
                "INSERT INTO appointment_slots(service_id, starts_at, ends_at) "
                "VALUES ($1, now() + interval '5 days', now() + interval '5 days 1 hour') "
                "RETURNING id",
                active_service,
            )
            hidden_slot = await migrator.fetchval(
                "INSERT INTO appointment_slots(service_id, starts_at, ends_at) "
                "VALUES ($1, now() + interval '4 days', now() + interval '4 days 1 hour') "
                "RETURNING id",
                inactive_service,
            )
            slot_ids.extend((primary_slot, declined_slot, race_slot, hidden_slot))
            assert primary_slot in {
                item["id"] for item in await result(client, "list_slots", active_service)
            }
            assert await result(client, "list_slots", inactive_service) == []
            assert (
                await result(client, "create_booking", second["id"], hidden_slot, update_ids[2])
                is None
            )

            booking = await result(
                client, "create_booking", first["id"], primary_slot, update_ids[2]
            )
            assert booking["id"]
            assert (
                await result(client, "create_booking", first["id"], primary_slot, update_ids[2])
            )["id"] == booking["id"]
            assert (
                await result(
                    client, "create_booking", second["id"], primary_slot, update_ids[2] + 1
                )
                is None
            )
            assert primary_slot not in {
                item["id"] for item in await result(client, "list_slots", active_service)
            }
            assert (await result(client, "get_booking", booking["id"]))["telegram_id"] == (
                -9_300_000_000_031
            )
            assert (await result(client, "list_bookings", first["id"]))[0]["id"] == booking["id"]
            assert await result(client, "list_bookings", second["id"]) == []
            assert await result(client, "decide_booking", booking["id"], "confirmed") == {
                "id": booking["id"]
            }
            assert await result(client, "decide_booking", booking["id"], "declined") is None
            notice = await result(client, "claim_notification", booking["id"])
            assert notice["telegram_id"] == -9_300_000_000_031
            assert await result(client, "claim_notification", booking["id"]) is None
            assert (
                await result(
                    client, "complete_notification", notice["id"], notice["attempts"], 123
                )
                is True
            )
            assert (
                await result(
                    client, "complete_notification", notice["id"], notice["attempts"], 123
                )
                is False
            )
            assert (await result(client, "get_booking", booking["id"]))[
                "client_notified_at"
            ] is not None

            declined = await result(
                client, "create_booking", first["id"], declined_slot, update_ids[2] + 2
            )
            assert await result(client, "decide_booking", declined["id"], "declined") == {
                "id": declined["id"]
            }
            assert declined_slot in {
                item["id"] for item in await result(client, "list_slots", active_service)
            }
            races = await asyncio.gather(
                result(client, "create_booking", first["id"], race_slot, update_ids[2] + 3),
                result(client, "create_booking", second["id"], race_slot, update_ids[2] + 4),
            )
            assert sum(item is not None for item in races) == 1
            assert (
                await migrator.fetchval(
                    "SELECT count(*) FROM booking_requests WHERE slot_id = $1", race_slot
                )
                == 1
            )
    finally:
        if slot_ids:
            await migrator.execute(
                "DELETE FROM booking_requests WHERE slot_id = ANY($1::bigint[])", slot_ids
            )
            await migrator.execute(
                "DELETE FROM appointment_slots WHERE id = ANY($1::bigint[])", slot_ids
            )
        if service_ids:
            await migrator.execute(
                "DELETE FROM services WHERE id = ANY($1::bigint[])", service_ids
            )
        if update_ids:
            await migrator.execute(
                "DELETE FROM processed_updates WHERE update_id = ANY($1::bigint[])", update_ids
            )
        if user_ids:
            await migrator.execute("DELETE FROM users WHERE id = ANY($1::bigint[])", user_ids)
        await migrator.close()
