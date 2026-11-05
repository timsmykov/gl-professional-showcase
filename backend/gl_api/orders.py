"""Dormant backend-owned order intent; no payment or entitlement operation."""

import asyncpg

ORDER_FIELDS = (
    "id",
    "user_id",
    "webinar_id",
    "amount_kopeks",
    "currency",
    "status",
    "source_update_id",
    "created_at",
)


class OrderUnavailable(Exception):
    """The buyer or a payable webinar is unavailable, or access is already owned."""


class OrderRequestConflict(Exception):
    """A Telegram update was already used for another order request."""


class OrderRepository:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def status(self, telegram_id: int, order_id: int) -> dict[str, object]:
        """Read the persisted authoritative state; never contact the provider."""
        if (
            type(telegram_id) is not int
            or not -(2**63) <= telegram_id < 2**63
            or telegram_id == 0
            or type(order_id) is not int
            or not 1 <= order_id < 2**63
        ):
            raise ValueError("invalid order identifiers")
        async with self.pool.acquire() as connection:
            row = await connection.fetchrow(
                """SELECT o.id,o.webinar_id,o.amount_kopeks,o.currency,o.status
                   FROM purchase_orders o JOIN users u ON u.id=o.user_id
                   WHERE o.id=$1 AND u.telegram_id=$2""",
                order_id,
                telegram_id,
            )
        if row is None:
            raise OrderUnavailable()
        return dict(row)

    async def create_pending_order(
        self, telegram_id: int, webinar_id: int, source_update_id: int
    ) -> dict[str, object]:
        if (
            type(telegram_id) is not int
            or type(webinar_id) is not int
            or type(source_update_id) is not int
            or not (-(2**63) <= telegram_id < 2**63)
            or telegram_id == 0
            or not (1 <= webinar_id < 2**63)
            or not (1 <= source_update_id < 2**63)
        ):
            raise ValueError("invalid order identifiers")

        async with self.pool.acquire() as connection, connection.transaction():
            # Serialize identical update IDs before any order write. The unique
            # table key remains the final guard if another writer ignores this lock.
            await connection.fetchval("SELECT pg_advisory_xact_lock($1::bigint)", source_update_id)
            existing = await self._by_source_update(connection, source_update_id)
            if existing:
                return self._replay(existing, telegram_id, webinar_id)

            pending = await self._pending_for_buyer(connection, telegram_id, webinar_id)
            if pending:
                await self._remember_request(connection, source_update_id, pending["id"])
                return self._snapshot(pending)

            created = await connection.fetchrow(
                """INSERT INTO purchase_orders(
                    user_id, webinar_id, amount_kopeks, source_update_id
                )
                SELECT u.id, w.id, w.price_kopeks, $3
                FROM users u CROSS JOIN webinars w
                WHERE u.telegram_id = $1 AND w.id = $2 AND w.active
                  AND w.price_kopeks > 0
                  AND NOT EXISTS (
                      SELECT 1 FROM webinar_entitlements e
                      WHERE e.user_id = u.id AND e.webinar_id = w.id
                  )
                ON CONFLICT DO NOTHING
                RETURNING id, user_id, webinar_id, amount_kopeks, currency, status,
                          source_update_id, created_at""",
                telegram_id,
                webinar_id,
                source_update_id,
            )
            if created:
                await self._remember_request(connection, source_update_id, created["id"])
                return self._snapshot(created)
            pending = await self._pending_for_buyer(connection, telegram_id, webinar_id)
            if pending:
                await self._remember_request(connection, source_update_id, pending["id"])
                return self._snapshot(pending)
            raise OrderUnavailable()

    @staticmethod
    async def _by_source_update(
        connection: asyncpg.Connection, source_update_id: int
    ) -> asyncpg.Record | None:
        return await connection.fetchrow(
            """SELECT o.id, o.user_id, o.webinar_id, o.amount_kopeks, o.currency,
                      o.status, o.source_update_id, o.created_at, u.telegram_id
               FROM order_intent_requests r
               JOIN purchase_orders o ON o.id = r.order_id
               JOIN users u ON u.id = o.user_id
               WHERE r.source_update_id = $1""",
            source_update_id,
        )

    @staticmethod
    async def _pending_for_buyer(
        connection: asyncpg.Connection, telegram_id: int, webinar_id: int
    ) -> asyncpg.Record | None:
        return await connection.fetchrow(
            """SELECT o.id, o.user_id, o.webinar_id, o.amount_kopeks, o.currency,
                      o.status, o.source_update_id, o.created_at
               FROM purchase_orders o JOIN users u ON u.id = o.user_id
               WHERE u.telegram_id = $1 AND o.webinar_id = $2 AND o.status = 'pending'
                 AND NOT EXISTS (
                     SELECT 1 FROM webinar_entitlements e
                     WHERE e.user_id = o.user_id AND e.webinar_id = o.webinar_id
                 )""",
            telegram_id,
            webinar_id,
        )

    @staticmethod
    async def _remember_request(
        connection: asyncpg.Connection, source_update_id: int, order_id: int
    ) -> None:
        await connection.execute(
            "INSERT INTO order_intent_requests(source_update_id, order_id) VALUES ($1, $2)",
            source_update_id,
            order_id,
        )

    @staticmethod
    def _snapshot(row: asyncpg.Record) -> dict[str, object]:
        return {field: row[field] for field in ORDER_FIELDS}

    @classmethod
    def _replay(
        cls, existing: asyncpg.Record, telegram_id: int, webinar_id: int
    ) -> dict[str, object]:
        if existing["telegram_id"] != telegram_id or existing["webinar_id"] != webinar_id:
            raise OrderRequestConflict()
        return cls._snapshot(existing)
