"""Bounded durable polling in the existing API process, without an external queue."""

import asyncio
import logging

from gl_api.checkout import CheckoutService

logger = logging.getLogger(__name__)


class PaymentReconciler:
    def __init__(self, service: CheckoutService, interval: int = 10):
        self.service = service
        self.interval = interval

    async def run_once(self) -> int:
        ids = await self.service.repository.due(self.service.environment, limit=20)
        completed = 0
        for order_id in ids:
            try:
                completed += bool(await self.service.reconcile(order_id))
            except Exception:  # noqa: BLE001 - no driver/provider secrets in background logs.
                logger.warning("payment_reconciliation_failed")
        return completed

    async def run(self):
        while True:
            try:
                await self.run_once()
            except Exception:  # noqa: BLE001 - DB outages must not kill the durable poller.
                logger.warning("payment_poll_unavailable")
            await asyncio.sleep(self.interval)
