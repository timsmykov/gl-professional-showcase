"""Hosted checkout orchestration; no bot/browser payment confirmation."""

from dataclasses import dataclass, field

from gl_api.atol_pay import AtolPayClient, AtolPayError, ReceiptPolicy, RegistrationUncertain
from gl_api.fiscal_notifications import NotificationCredentials
from gl_api.payments import CheckoutUnavailable, PaymentRepository


class CheckoutPending(RuntimeError):
    """Another registration is running or its result needs reconciliation."""


@dataclass(frozen=True)
class CheckoutLink:
    order_id: int
    payment_url: str = field(repr=False)


class CheckoutService:
    def __init__(
        self,
        repository: PaymentRepository,
        client: AtolPayClient,
        environment: str,
        receipt: ReceiptPolicy,
        return_url: str,
        notifications: NotificationCredentials | None = None,
    ):
        self.repository = repository
        self.client = client
        self.environment = environment
        self.receipt = receipt
        self.return_url = return_url
        if notifications is not None and notifications.environment != environment:
            raise ValueError("notification environment mismatch")
        self.notifications = notifications

    async def checkout(self, telegram_id: int, order_id: int) -> CheckoutLink:
        attempt, created = await self.repository.reserve(
            telegram_id, order_id, self.environment, self.receipt
        )
        if attempt["review_required"]:
            raise CheckoutUnavailable("checkout unavailable")
        if not created:
            if attempt["registration_state"] == "registered":
                return CheckoutLink(order_id, attempt["payment_url"])
            raise CheckoutPending("checkout is being reconciled")
        try:
            notification_options = {}
            if self.notifications is not None:
                notification_options["notification_url"] = self.notifications.notification_url(
                    str(attempt["merchant_order_id"])
                )
            payment = await self.client.register(
                str(attempt["merchant_order_id"]),
                attempt["amount_kopeks"],
                attempt["recording_name"],
                self.return_url,
                self.receipt,
                **notification_options,
            )
        except RegistrationUncertain:
            await self.repository.uncertain(order_id)
            raise CheckoutPending("checkout is being reconciled") from None
        # If SQL commit fails after HTTP succeeds, the reserved attempt persists.
        # A repeated request cannot call POST again and does not leak its URL.
        await self.repository.registered(order_id, payment)
        return CheckoutLink(order_id, payment.payment_url)

    async def reconcile(self, order_id: int) -> bool:
        attempt = await self.repository.claim_poll(order_id)
        if attempt is None:
            return False
        if attempt["environment"] != self.environment:
            await self.repository.poll_failed(order_id, attempt["poll_lease"])
            raise CheckoutUnavailable("payment environment mismatch")
        try:
            status = await self.client.status(str(attempt["merchant_order_id"]))
        except AtolPayError:
            await self.repository.poll_failed(order_id, attempt["poll_lease"])
            return False
        return await self.repository.observe(order_id, attempt["poll_lease"], status)
