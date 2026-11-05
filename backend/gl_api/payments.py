"""ATOL checkout persistence. Provider HTTP never runs inside SQL transactions."""

import json
from dataclasses import asdict
from uuid import UUID, uuid4

import asyncpg

from gl_api.atol_pay import PaymentStatus, ReceiptPolicy, RegisteredPayment


class CheckoutUnavailable(RuntimeError):
    """Unknown buyer/order, unpayable order or an environment mismatch."""


class PaymentRepository:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def reserve(
        self, telegram_id: int, order_id: int, environment: str, policy: ReceiptPolicy
    ) -> tuple[dict, bool]:
        """Only the transaction that inserts the attempt may call POST registration."""
        if (
            type(order_id) is not int
            or order_id <= 0
            or environment not in {"sandbox", "production"}
        ):
            raise ValueError("invalid checkout configuration")
        async with self.pool.acquire() as c, c.transaction():
            order = await c.fetchrow(
                """SELECT o.id, o.amount_kopeks, o.status, w.title
                   FROM purchase_orders o JOIN users u ON u.id=o.user_id
                   JOIN webinars w ON w.id=o.webinar_id
                   WHERE o.id=$1 AND u.telegram_id=$2 AND o.status='pending'
                     AND w.active AND o.amount_kopeks>0 AND NOT EXISTS (
                         SELECT 1 FROM webinar_entitlements e
                         WHERE e.user_id=o.user_id AND e.webinar_id=o.webinar_id)
                   FOR UPDATE OF o""",
                order_id,
                telegram_id,
            )
            if order is None:
                raise CheckoutUnavailable("checkout unavailable")
            if (
                not 1 <= order["amount_kopeks"] <= 2_000_000_000
                or not order["title"].strip()
                or len(order["title"]) > 128
                or any(ord(c) < 32 for c in order["title"])
            ):
                raise CheckoutUnavailable("checkout unavailable")
            inserted = await c.fetchrow(
                """INSERT INTO atol_payment_attempts
                       (order_id, merchant_order_id, environment, recording_name, receipt_policy)
                   VALUES ($1,$2,$3,$4,$5::jsonb) ON CONFLICT DO NOTHING RETURNING *""",
                order_id,
                uuid4(),
                environment,
                order["title"],
                json.dumps(asdict(policy)),
            )
            row = inserted or await c.fetchrow(
                "SELECT * FROM atol_payment_attempts WHERE order_id=$1", order_id
            )
            if row["environment"] != environment:
                raise CheckoutUnavailable("checkout unavailable")
            return dict(row) | {"amount_kopeks": order["amount_kopeks"]}, inserted is not None

    async def uncertain(self, order_id: int) -> None:
        async with self.pool.acquire() as c:
            await c.execute(
                """UPDATE atol_payment_attempts SET registration_state='uncertain',
                   updated_at=clock_timestamp() WHERE order_id=$1 AND registration_state='reserved'""",
                order_id,
            )

    async def registered(self, order_id: int, payment: RegisteredPayment) -> None:
        async with self.pool.acquire() as c, c.transaction():
            row = await c.fetchrow(
                """SELECT a.*,o.amount_kopeks FROM purchase_orders o
                   JOIN atol_payment_attempts a ON a.order_id=o.id
                   WHERE o.id=$1 FOR UPDATE OF o,a""",
                order_id,
            )
            if (
                row is None
                or str(row["merchant_order_id"]) != payment.order_id
                or row["amount_kopeks"] != payment.amount_kopeks
            ):
                raise CheckoutUnavailable("registration mismatch")
            if row["registration_state"] == "registered":
                if row["payment_url"] != payment.payment_url:
                    raise CheckoutUnavailable("registration mismatch")
                return
            await c.execute(
                """UPDATE atol_payment_attempts SET registration_state='registered',payment_url=$2,
                   updated_at=clock_timestamp() WHERE order_id=$1""",
                order_id,
                payment.payment_url,
            )

    async def due(self, environment: str, *, limit: int = 20) -> list[int]:
        if (
            environment not in {"sandbox", "production"}
            or type(limit) is not int
            or not 1 <= limit <= 20
        ):
            raise ValueError("invalid poll configuration")
        async with self.pool.acquire() as c:
            rows = await c.fetch(
                """SELECT order_id FROM atol_payment_attempts
                   WHERE environment=$1 AND next_poll_at<=clock_timestamp()
                   AND (poll_expires_at IS NULL OR poll_expires_at<clock_timestamp())
                   ORDER BY next_poll_at,order_id LIMIT $2""",
                environment,
                limit,
            )
            return [row["order_id"] for row in rows]

    async def claim_poll(self, order_id: int) -> dict | None:
        lease = uuid4()
        async with self.pool.acquire() as c:
            row = await c.fetchrow(
                """UPDATE atol_payment_attempts a SET poll_lease=$2,
                   poll_expires_at=clock_timestamp()+interval '30 seconds'
                   FROM purchase_orders o WHERE a.order_id=$1 AND o.id=a.order_id
                   AND a.next_poll_at<=clock_timestamp()
                   AND (a.poll_expires_at IS NULL OR a.poll_expires_at<clock_timestamp())
                   RETURNING a.*,o.amount_kopeks""",
                order_id,
                lease,
            )
            return dict(row) if row else None

    async def poll_failed(self, order_id: int, lease: UUID) -> None:
        async with self.pool.acquire() as c:
            await c.execute(
                """UPDATE atol_payment_attempts SET poll_lease=NULL,poll_expires_at=NULL,
                   next_poll_at=clock_timestamp()+interval '1 minute'
                   WHERE order_id=$1 AND poll_lease=$2""",
                order_id,
                lease,
            )

    async def observe(self, order_id: int, lease: UUID, status: PaymentStatus) -> bool:
        """Fenced authenticated status observation and grant commit atomically.

        Call only with the configured provider client's authenticated GET result.
        A callback payload or a user's status is not an observation.
        """
        if not isinstance(status, PaymentStatus):
            raise TypeError("invalid payment observation")
        async with self.pool.acquire() as c, c.transaction():
            row = await c.fetchrow(
                """SELECT a.*,o.status AS order_status,o.user_id,o.webinar_id,
                   (a.poll_expires_at>clock_timestamp()) AS lease_valid
                   FROM purchase_orders o JOIN atol_payment_attempts a ON a.order_id=o.id
                   WHERE o.id=$1 FOR UPDATE OF o,a""",
                order_id,
            )
            if row is None or row["poll_lease"] != lease or not row["lease_valid"]:
                return False
            review = status in {
                PaymentStatus.PARTIALLY_CANCELLED,
                PaymentStatus.PARTIALLY_REFUNDED,
            }
            if status is PaymentStatus.PAID:
                if (
                    row["registration_state"] != "registered"
                    or row["review_required"]
                    or row["order_status"] not in {"pending", "paid"}
                ):
                    review = True
                else:
                    await c.execute(
                        "UPDATE purchase_orders SET status='paid',paid_at=coalesce(paid_at,clock_timestamp()) WHERE id=$1",
                        order_id,
                    )
                    await c.execute(
                        """INSERT INTO webinar_entitlements(user_id,webinar_id,source,payment_order_id)
                           VALUES($1,$2,'paid',$3) ON CONFLICT DO NOTHING""",
                        row["user_id"],
                        row["webinar_id"],
                        order_id,
                    )
            elif status in {PaymentStatus.CANCELLED, PaymentStatus.REFUNDED}:
                await c.execute(
                    "UPDATE purchase_orders SET status=$2 WHERE id=$1 AND status IN ('pending','paid')",
                    order_id,
                    "refunded" if status is PaymentStatus.REFUNDED else "cancelled",
                )
                # Never revoke imports or access granted by another purchase.
                await c.execute(
                    "DELETE FROM webinar_entitlements WHERE payment_order_id=$1 AND source='paid'",
                    order_id,
                )
            elif status in {PaymentStatus.FAILED, PaymentStatus.EXPIRED, PaymentStatus.FRAUD}:
                await c.execute(
                    "UPDATE purchase_orders SET status='cancelled' WHERE id=$1 AND status='pending'",
                    order_id,
                )
                review = row["order_status"] == "paid"
            await c.execute(
                """INSERT INTO payment_events(provider,provider_event_id,order_id,event_type)
                   VALUES('atol_pay',$1,$2,$3) ON CONFLICT DO NOTHING""",
                f"{row['merchant_order_id']}:{int(status)}",
                order_id,
                status.name.lower(),
            )
            await c.execute(
                """UPDATE atol_payment_attempts SET last_status=$3,
                   review_required=review_required OR $4,poll_lease=NULL,poll_expires_at=NULL,
                   updated_at=clock_timestamp(),next_poll_at=clock_timestamp()+
                   CASE WHEN $3::smallint=1 THEN interval '1 hour' ELSE interval '1 minute' END
                   WHERE order_id=$1 AND poll_lease=$2""",
                order_id,
                lease,
                int(status),
                review,
            )
            return True
