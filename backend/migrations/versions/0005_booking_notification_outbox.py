"""Persist booking decisions and their client notifications together."""

from migrations.roles import execute_role_sql

revision = "0005_booking_notification_outbox"
down_revision = "0004_bot_runtime_privileges"
branch_labels = None
depends_on = None


def upgrade() -> None:
    execute_role_sql("""
        CREATE TABLE booking_notification_outbox (
            id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            booking_id BIGINT NOT NULL UNIQUE REFERENCES booking_requests(id) ON DELETE CASCADE,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'sending', 'sent', 'needs_review')),
            attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
            available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            lease_until TIMESTAMPTZ,
            telegram_message_id BIGINT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            sent_at TIMESTAMPTZ
        )
    """)
    execute_role_sql("""
        CREATE INDEX booking_notification_due_idx
        ON booking_notification_outbox(available_at, id)
        WHERE status IN ('pending', 'sending')
    """)
    execute_role_sql("""
        GRANT SELECT, INSERT, UPDATE ON booking_notification_outbox TO gl_api_app
    """)
    execute_role_sql("""
        GRANT USAGE ON SEQUENCE booking_notification_outbox_id_seq TO gl_api_app
    """)


def downgrade() -> None:
    raise RuntimeError("Destructive downgrade is disabled; restore a verified backup")
