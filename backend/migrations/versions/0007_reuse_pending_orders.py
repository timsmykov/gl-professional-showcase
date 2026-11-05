"""Reuse one pending order per buyer/webinar and remember every request ID."""

from alembic import op
from sqlalchemy import text

from migrations.roles import execute_role_sql

revision = "0007_reuse_pending_orders"
down_revision = "0006_order_request_idempotency"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # An existing duplicate makes the upgrade fail atomically; never pick or
    # delete an order silently because a provider may already reference it.
    duplicates = (
        op.get_bind()
        .execute(
            text("""
            SELECT EXISTS (
                SELECT 1 FROM purchase_orders WHERE status = 'pending'
                GROUP BY user_id, webinar_id HAVING count(*) > 1
            )
        """)
        )
        .scalar_one()
    )
    if duplicates:
        raise RuntimeError("Duplicate pending orders require manual reconciliation")
    execute_role_sql("""
        CREATE UNIQUE INDEX purchase_orders_one_pending_per_webinar_idx
        ON purchase_orders(user_id, webinar_id) WHERE status = 'pending'
    """)
    execute_role_sql("""
        CREATE TABLE order_intent_requests (
            source_update_id BIGINT PRIMARY KEY CHECK (source_update_id > 0),
            order_id BIGINT NOT NULL REFERENCES purchase_orders(id),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    execute_role_sql("""
        INSERT INTO order_intent_requests(source_update_id, order_id)
        SELECT source_update_id, id FROM purchase_orders
        WHERE source_update_id IS NOT NULL
    """)
    execute_role_sql("GRANT SELECT, INSERT ON order_intent_requests TO gl_api_app")


def downgrade() -> None:
    raise RuntimeError("Destructive downgrade is disabled; restore a verified backup")
