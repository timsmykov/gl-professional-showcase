from __future__ import annotations

from datetime import datetime

import asyncpg


class Repository:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def health(self) -> None:
        for sql in (
            "SELECT 1 FROM users LIMIT 1",
            "SELECT 1 FROM user_sections LIMIT 1",
            "SELECT 1 FROM services LIMIT 1",
            "SELECT 1 FROM appointment_slots LIMIT 1",
            "SELECT 1 FROM booking_requests LIMIT 1",
            "SELECT 1 FROM booking_notification_outbox LIMIT 1",
            "SELECT 1 FROM products LIMIT 1",
            "SELECT 1 FROM webinars LIMIT 1",
            "SELECT 1 FROM webinar_entitlements LIMIT 1",
            "SELECT 1 FROM processed_updates LIMIT 1",
        ):
            await self.pool.fetchval(sql)

    async def claim_update(self, update_id: int) -> str | dict[str, str]:
        claimed = await self.pool.fetchrow(
            """INSERT INTO processed_updates(update_id, status) VALUES ($1, 'processing')
            ON CONFLICT (update_id) DO UPDATE SET status = 'processing', claimed_at = now()
            WHERE processed_updates.status = 'processing'
              AND processed_updates.claimed_at < now() - interval '2 minutes'
            RETURNING claimed_at""",
            update_id,
        )
        if claimed:
            return {"status": "claimed", "lease": claimed["claimed_at"].isoformat()}
        current = await self.pool.fetchval(
            "SELECT status FROM processed_updates WHERE update_id = $1", update_id
        )
        return "done" if current == "done" else "busy"

    async def complete_update(self, update_id: int, lease: str) -> bool:
        result = await self.pool.execute(
            """UPDATE processed_updates SET status = 'done', processed_at = now()
            WHERE update_id = $1 AND status = 'processing' AND claimed_at = $2""",
            update_id,
            datetime.fromisoformat(lease),
        )
        return result == "UPDATE 1"

    async def release_update(self, update_id: int, lease: str) -> None:
        await self.pool.execute(
            """DELETE FROM processed_updates
            WHERE update_id = $1 AND status = 'processing' AND claimed_at = $2""",
            update_id,
            datetime.fromisoformat(lease),
        )

    async def get_user(self, telegram_id: int) -> asyncpg.Record:
        return await self.pool.fetchrow(
            """INSERT INTO users(telegram_id) VALUES ($1)
            ON CONFLICT (telegram_id) DO UPDATE SET updated_at = now() RETURNING *""",
            telegram_id,
        )

    async def select_section(self, user_id: int, section: str) -> None:
        async with self.pool.acquire() as connection, connection.transaction():
            await connection.execute(
                "INSERT INTO user_sections(user_id, section) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                user_id,
                section,
            )
            await connection.execute(
                "UPDATE users SET last_section = $2, updated_at = now() WHERE id = $1",
                user_id,
                section,
            )

    async def has_section(self, user_id: int, section: str) -> bool:
        return bool(
            await self.pool.fetchval(
                "SELECT 1 FROM user_sections WHERE user_id = $1 AND section = $2",
                user_id,
                section,
            )
        )

    async def list_services(self) -> list[asyncpg.Record]:
        return await self.pool.fetch(
            "SELECT id, title, description FROM services WHERE active ORDER BY title LIMIT 20"
        )

    async def get_service(self, service_id: int) -> asyncpg.Record | None:
        return await self.pool.fetchrow(
            "SELECT id, title, description FROM services WHERE id = $1 AND active", service_id
        )

    async def list_slots(self, service_id: int) -> list[asyncpg.Record]:
        return await self.pool.fetch(
            """SELECT s.id, s.starts_at, s.ends_at FROM appointment_slots s
            JOIN services v ON v.id = s.service_id
            WHERE s.service_id = $1 AND s.active AND v.active AND s.starts_at > now()
              AND NOT EXISTS (
                SELECT 1 FROM booking_requests b
                WHERE b.slot_id = s.id AND b.status IN ('pending', 'confirmed')
              )
            ORDER BY s.starts_at LIMIT 10""",
            service_id,
        )

    async def create_booking(
        self, user_id: int, slot_id: int, update_id: int
    ) -> asyncpg.Record | None:
        previous = await self.pool.fetchrow(
            """SELECT id FROM booking_requests
            WHERE source_update_id = $1 AND user_id = $2 AND slot_id = $3""",
            update_id,
            user_id,
            slot_id,
        )
        if previous:
            return previous
        try:
            return await self.pool.fetchrow(
                """INSERT INTO booking_requests(user_id, slot_id, source_update_id)
                SELECT $1, s.id, $3 FROM appointment_slots s
                JOIN services v ON v.id = s.service_id
                WHERE s.id = $2 AND s.active AND v.active AND s.starts_at > now()
                  AND NOT EXISTS (
                    SELECT 1 FROM booking_requests b
                    WHERE b.slot_id = s.id AND b.status IN ('pending', 'confirmed')
                  )
                RETURNING id""",
                user_id,
                slot_id,
                update_id,
            )
        except asyncpg.UniqueViolationError:
            return await self.pool.fetchrow(
                """SELECT id FROM booking_requests
                WHERE source_update_id = $1 AND user_id = $2 AND slot_id = $3""",
                update_id,
                user_id,
                slot_id,
            )

    async def get_booking(self, booking_id: int) -> asyncpg.Record | None:
        return await self.pool.fetchrow(
            """SELECT b.id, b.status, b.client_notified_at, u.telegram_id,
                    v.title, s.starts_at
            FROM booking_requests b JOIN users u ON u.id = b.user_id
            JOIN appointment_slots s ON s.id = b.slot_id
            JOIN services v ON v.id = s.service_id WHERE b.id = $1""",
            booking_id,
        )

    async def decide_booking(self, booking_id: int, status: str) -> asyncpg.Record | None:
        async with self.pool.acquire() as connection, connection.transaction():
            decided = await connection.fetchrow(
                """UPDATE booking_requests SET status = $2, decided_at = now()
                WHERE id = $1 AND status = 'pending' RETURNING id""",
                booking_id,
                status,
            )
            if decided:
                await connection.execute(
                    "INSERT INTO booking_notification_outbox(booking_id) VALUES ($1)",
                    booking_id,
                )
            return decided

    async def claim_notification(self, booking_id: int) -> asyncpg.Record | None:
        async with self.pool.acquire() as connection, connection.transaction():
            await connection.execute(
                """UPDATE booking_notification_outbox SET status = 'needs_review', lease_until = NULL
                WHERE status = 'sending' AND attempts >= 5 AND lease_until <= now()"""
            )
            return await connection.fetchrow(
                """WITH candidate AS (
                SELECT id FROM booking_notification_outbox
                WHERE ($1 = 0 OR booking_id = $1)
                  AND attempts < 5
                  AND ((status = 'pending' AND available_at <= now())
                       OR (status = 'sending' AND lease_until <= now()))
                ORDER BY available_at, id LIMIT 1 FOR UPDATE SKIP LOCKED
            ), claimed AS (
                UPDATE booking_notification_outbox n
                SET status = 'sending', attempts = n.attempts + 1,
                    lease_until = now() + interval '2 minutes'
                FROM candidate WHERE n.id = candidate.id
                RETURNING n.id, n.booking_id, n.attempts
            )
            SELECT c.id, c.booking_id, c.attempts, b.status, u.telegram_id, v.title, s.starts_at
            FROM claimed c JOIN booking_requests b ON b.id = c.booking_id
            JOIN users u ON u.id = b.user_id
            JOIN appointment_slots s ON s.id = b.slot_id
            JOIN services v ON v.id = s.service_id""",
                booking_id,
            )

    async def complete_notification(
        self, notification_id: int, attempts: int, message_id: int
    ) -> bool:
        async with self.pool.acquire() as connection, connection.transaction():
            booking_id = await connection.fetchval(
                """UPDATE booking_notification_outbox
                SET status = 'sent', sent_at = now(), lease_until = NULL,
                    telegram_message_id = NULLIF($3, 0)
                WHERE id = $1 AND attempts = $2 AND status = 'sending'
                RETURNING booking_id""",
                notification_id,
                attempts,
                message_id,
            )
            if booking_id is None:
                return False
            await connection.execute(
                "UPDATE booking_requests SET client_notified_at = now() WHERE id = $1",
                booking_id,
            )
            return True

    async def retry_notification(self, notification_id: int, attempts: int) -> bool:
        result = await self.pool.fetchval(
            """UPDATE booking_notification_outbox
            SET status = CASE WHEN attempts >= 5 THEN 'needs_review' ELSE 'pending' END,
                lease_until = NULL,
                available_at = now() + make_interval(
                    secs => LEAST(3600, (power(2, LEAST(attempts, 8)) * 5)::integer))
            WHERE id = $1 AND attempts = $2 AND status = 'sending'
            RETURNING id""",
            notification_id,
            attempts,
        )
        return result is not None

    async def list_bookings(self, user_id: int) -> list[asyncpg.Record]:
        return await self.pool.fetch(
            """SELECT b.id, b.status, v.title, s.starts_at FROM booking_requests b
            JOIN appointment_slots s ON s.id = b.slot_id
            JOIN services v ON v.id = s.service_id
            WHERE b.user_id = $1 ORDER BY b.created_at DESC LIMIT 10""",
            user_id,
        )

    async def list_products(self) -> list[asyncpg.Record]:
        return await self.pool.fetch(
            "SELECT id, title FROM products WHERE active ORDER BY title LIMIT 20"
        )

    async def get_product(self, product_id: int) -> asyncpg.Record | None:
        return await self.pool.fetchrow(
            "SELECT id, title, description FROM products WHERE id = $1 AND active", product_id
        )

    async def list_webinars(self) -> list[asyncpg.Record]:
        return await self.pool.fetch(
            "SELECT id, title, price_kopeks FROM webinars WHERE active ORDER BY id DESC LIMIT 20"
        )

    async def get_webinar(self, webinar_id: int) -> asyncpg.Record | None:
        return await self.pool.fetchrow(
            """SELECT id, title, description, price_kopeks
            FROM webinars WHERE id = $1 AND active""",
            webinar_id,
        )

    async def list_entitlements(self, user_id: int) -> list[asyncpg.Record]:
        return await self.pool.fetch(
            """SELECT w.title, e.source FROM webinar_entitlements e
            JOIN webinars w ON w.id = e.webinar_id
            WHERE e.user_id = $1 ORDER BY e.granted_at DESC LIMIT 20""",
            user_id,
        )

    async def resolve_recording(self, public_code: str) -> asyncpg.Record | None:
        return await self.pool.fetchrow(
            """SELECT id, title, description, price_kopeks FROM webinars
            WHERE public_code = $1 AND active""",
            public_code,
        )

    async def get_recording_context(self, webinar_id: int) -> asyncpg.Record | None:
        # Historical order/access presentation survives catalog withdrawal.
        # This read does not establish permission to buy or watch.
        return await self.pool.fetchrow(
            "SELECT id, title, description, price_kopeks FROM webinars WHERE id = $1",
            webinar_id,
        )

    async def list_owned_recordings(self, telegram_id: int) -> list[asyncpg.Record]:
        return await self.pool.fetch(
            """SELECT e.webinar_id, w.title FROM webinar_entitlements e
            JOIN users u ON u.id = e.user_id
            JOIN webinars w ON w.id = e.webinar_id
            WHERE u.telegram_id = $1 ORDER BY e.granted_at DESC, e.webinar_id DESC LIMIT 20""",
            telegram_id,
        )
