"""Add dormant purchase records; no purchase or grant API is exposed."""

from alembic import op

revision = "0002_purchase_records"
down_revision = "0001_legacy_bot_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE purchase_orders (
            id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            user_id BIGINT NOT NULL REFERENCES users(id),
            webinar_id BIGINT NOT NULL REFERENCES webinars(id),
            amount_kopeks INTEGER NOT NULL CHECK (amount_kopeks >= 0),
            currency TEXT NOT NULL DEFAULT 'RUB' CHECK (currency = 'RUB'),
            status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'paid', 'cancelled', 'refunded')),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            paid_at TIMESTAMPTZ
        )
    """)
    op.execute(
        "CREATE INDEX purchase_orders_user_idx ON purchase_orders(user_id, created_at DESC)"
    )
    op.execute("""
        CREATE TABLE payment_events (
            id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            provider TEXT NOT NULL,
            provider_event_id TEXT NOT NULL,
            order_id BIGINT NOT NULL REFERENCES purchase_orders(id),
            event_type TEXT NOT NULL,
            received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (provider, provider_event_id)
        )
    """)


def downgrade() -> None:
    raise RuntimeError("Destructive downgrade is disabled; restore a verified backup")
