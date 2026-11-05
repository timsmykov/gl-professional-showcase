"""Internal receipt ledger, independent from payment/access transitions.

Inputs are backend-normalized observations, not raw callbacks. A future provider
adapter must authenticate the sender and verify units/IDs before calling record.
No HTTP route currently accepts these observations.
"""

from dataclasses import dataclass, field
from enum import StrEnum
from uuid import UUID

import asyncpg


class FiscalState(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"
    REVIEW = "review"


class ReceiptUnavailable(RuntimeError):
    """Unregistered order, namespace/amount mismatch or reused receipt identity."""


@dataclass(frozen=True)
class ReceiptObservation:
    merchant_order_id: UUID = field(repr=False)
    receipt_id: UUID = field(repr=False)
    environment: str
    receipt_type: str
    amount_kopeks: int = field(repr=False)
    state: FiscalState

    def __post_init__(self):
        if (
            any(
                not isinstance(value, UUID) or value.version != 4
                for value in (self.merchant_order_id, self.receipt_id)
            )
            or type(self.environment) is not str
            or self.environment not in {"sandbox", "production"}
            or type(self.receipt_type) is not str
            or self.receipt_type not in {"sell", "sell_refund"}
            or type(self.amount_kopeks) is not int
            or not 1 <= self.amount_kopeks <= 2_000_000_000
            or not isinstance(self.state, FiscalState)
        ):
            raise ValueError("invalid normalized receipt observation")


class ReceiptRepository:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def record(
        self, observation: ReceiptObservation, *, authenticated_notification: bool = False
    ) -> FiscalState:
        if not isinstance(observation, ReceiptObservation):
            raise TypeError("normalized receipt observation required")
        async with self.pool.acquire() as connection, connection.transaction():
            # A provider notification can arrive before the registration HTTP response.
            # The callback adapter authenticates the order token before opting in.
            # Frozen identity/amount plus receipt uniqueness allow a SELECT-only role;
            # the ordinary internal ledger path retains its existing row locks.
            lock = "" if authenticated_notification else " FOR UPDATE OF a,o"
            order = await connection.fetchrow(
                """SELECT a.order_id,o.amount_kopeks FROM atol_payment_attempts a
                   JOIN purchase_orders o ON o.id=a.order_id
                   WHERE a.merchant_order_id=$1 AND a.environment=$2
                     AND (a.registration_state='registered' OR $3)"""
                + lock,
                observation.merchant_order_id,
                observation.environment,
                authenticated_notification,
            )
            if order is None or (
                observation.amount_kopeks > order["amount_kopeks"]
                or observation.receipt_type == "sell"
                and observation.amount_kopeks != order["amount_kopeks"]
            ):
                raise ReceiptUnavailable("receipt unavailable")
            await connection.execute(
                """INSERT INTO payment_receipts(receipt_id,order_id,receipt_type,amount_kopeks)
                   VALUES($1,$2,$3,$4) ON CONFLICT DO NOTHING""",
                observation.receipt_id,
                order["order_id"],
                observation.receipt_type,
                observation.amount_kopeks,
            )
            receipt = await connection.fetchrow(
                "SELECT order_id,receipt_type,amount_kopeks FROM payment_receipts WHERE receipt_id=$1",
                observation.receipt_id,
            )
            if (receipt["order_id"], receipt["receipt_type"], receipt["amount_kopeks"]) != (
                order["order_id"],
                observation.receipt_type,
                observation.amount_kopeks,
            ):
                raise ReceiptUnavailable("receipt unavailable")
            await connection.execute(
                """INSERT INTO receipt_observations(receipt_id,state) VALUES($1,$2)
                   ON CONFLICT DO NOTHING""",
                observation.receipt_id,
                observation.state.value,
            )
            states = {
                row["state"]
                for row in await connection.fetch(
                    "SELECT state FROM receipt_observations WHERE receipt_id=$1",
                    observation.receipt_id,
                )
            }
            # Arrival order does not prove provider event order. Conflicts require review.
            return FiscalState(next(iter(states))) if len(states) == 1 else FiscalState.REVIEW
