"""Append-only normalized receipt observations; no payment/access mutation."""

from migrations.roles import execute_role_sql

revision = "0010_receipt_ledger"
down_revision = "0009_atol_payment_attempts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    execute_role_sql("""
        CREATE TABLE payment_receipts (
            receipt_id UUID PRIMARY KEY,
            order_id BIGINT NOT NULL REFERENCES atol_payment_attempts(order_id),
            receipt_type TEXT NOT NULL CHECK (receipt_type IN ('sell','sell_refund')),
            amount_kopeks BIGINT NOT NULL CHECK (amount_kopeks BETWEEN 1 AND 2000000000),
            received_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
        );
        CREATE INDEX payment_receipts_order_idx ON payment_receipts(order_id);
        CREATE TABLE receipt_observations (
            receipt_id UUID NOT NULL REFERENCES payment_receipts(receipt_id),
            state TEXT NOT NULL CHECK (state IN ('success','failed','review')),
            observed_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            PRIMARY KEY(receipt_id,state)
        );
        GRANT SELECT, INSERT ON payment_receipts,receipt_observations TO gl_api_app;
    """)


def downgrade() -> None:
    raise RuntimeError("Destructive downgrade is disabled; restore a verified backup")
