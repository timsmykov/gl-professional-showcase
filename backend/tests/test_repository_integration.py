import os
import time

import asyncpg
import pytest

from gl_bot.repository import Repository
from gl_bot.settings import database_url_from_env


@pytest.mark.asyncio
@pytest.mark.skipif(
    not (os.getenv("TEST_DATABASE_URL") or os.getenv("TEST_DATABASE_PASSWORD_FILE")),
    reason="test database connection not set",
)
async def test_postgres_dual_roles_deduplication_and_double_booking():
    dsn = os.getenv("TEST_DATABASE_URL") or database_url_from_env(
        os.environ, password_file_key="TEST_DATABASE_PASSWORD_FILE"
    )
    pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=4)
    repo = Repository(pool)
    base = int(time.time_ns() // 1_000_000)
    try:
        await repo.health()
        user = await repo.get_user(base)
        await repo.select_section(user["id"], "pro")
        await repo.select_section(user["id"], "client")
        same_user = await repo.get_user(base)
        assert same_user["id"] == user["id"]
        assert same_user["last_section"] == "client"
        assert await repo.has_section(user["id"], "pro")
        assert await repo.has_section(user["id"], "client")

        first_claim = await repo.claim_update(base + 1)
        assert first_claim["status"] == "claimed"
        assert await repo.claim_update(base + 1) == "busy"
        assert await repo.complete_update(base + 1, first_claim["lease"])
        assert await repo.claim_update(base + 1) == "done"
        second_claim = await repo.claim_update(base + 2)
        assert second_claim["status"] == "claimed"
        await repo.release_update(base + 2, second_claim["lease"])
        old_claim = await repo.claim_update(base + 2)
        assert old_claim["status"] == "claimed"
        await pool.execute(
            "UPDATE processed_updates SET claimed_at = now() - interval '3 minutes' "
            "WHERE update_id = $1",
            base + 2,
        )
        new_claim = await repo.claim_update(base + 2)
        assert new_claim["status"] == "claimed"
        await repo.release_update(base + 2, old_claim["lease"])
        assert await repo.complete_update(base + 2, old_claim["lease"]) is False
        assert await repo.claim_update(base + 2) == "busy"
        assert await repo.complete_update(base + 2, new_claim["lease"])
        assert await repo.claim_update(base + 2) == "done"

        service_id = await pool.fetchval(
            "INSERT INTO services(title, active) VALUES ('Тестовая услуга', true) RETURNING id"
        )
        slot_id = await pool.fetchval(
            """INSERT INTO appointment_slots(service_id, starts_at, ends_at)
            VALUES ($1, now() + interval '5 days', now() + interval '5 days 1 hour') RETURNING id""",
            service_id,
        )
        booking = await repo.create_booking(user["id"], slot_id, base + 3)
        assert booking["id"]
        assert (await repo.create_booking(user["id"], slot_id, base + 3))["id"] == booking["id"]
        assert await repo.create_booking(user["id"], slot_id, base + 4) is None
        assert await repo.list_slots(service_id) == []
        await repo.decide_booking(booking["id"], "confirmed")
        assert (await repo.list_bookings(user["id"]))[0]["status"] == "confirmed"
        assert await repo.decide_booking(booking["id"], "declined") is None
    finally:
        await pool.close()
