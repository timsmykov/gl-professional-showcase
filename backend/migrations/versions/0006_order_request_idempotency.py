"""Allow price-snapshotted pending orders without enabling payment writes."""

from migrations.roles import execute_role_sql

revision = "0006_order_request_idempotency"
down_revision = "0005_booking_notification_outbox"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing dormant rows remain untouched. Only new order intents use this key.
    execute_role_sql("""
        ALTER TABLE purchase_orders
        ADD COLUMN source_update_id BIGINT UNIQUE CHECK (source_update_id > 0)
    """)
    execute_role_sql("GRANT SELECT ON purchase_orders TO gl_api_app")
    execute_role_sql(
        "GRANT INSERT (user_id, webinar_id, amount_kopeks, source_update_id) "
        "ON purchase_orders TO gl_api_app"
    )
    execute_role_sql("GRANT USAGE ON SEQUENCE purchase_orders_id_seq TO gl_api_app")


def downgrade() -> None:
    raise RuntimeError("Destructive downgrade is disabled; restore a verified backup")
