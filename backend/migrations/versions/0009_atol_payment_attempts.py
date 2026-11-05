"""Persist bounded payment attempts and tie grants to their purchase order."""

from migrations.roles import execute_role_sql

revision = "0009_atol_payment_attempts"
down_revision = "0008_video_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    execute_role_sql("""
        CREATE TABLE atol_payment_attempts (
            order_id BIGINT PRIMARY KEY REFERENCES purchase_orders(id),
            merchant_order_id UUID NOT NULL UNIQUE,
            environment TEXT NOT NULL CHECK (environment IN ('sandbox', 'production')),
            registration_state TEXT NOT NULL DEFAULT 'reserved'
                CHECK (registration_state IN ('reserved', 'registered', 'uncertain')),
            recording_name TEXT NOT NULL CHECK (length(recording_name) BETWEEN 1 AND 128),
            receipt_policy JSONB NOT NULL CHECK (jsonb_typeof(receipt_policy) = 'object'),
            payment_url TEXT,
            last_status SMALLINT CHECK (last_status IN (0,1,2,3,4,5,6,7,8,9,10,11,12,255)),
            review_required BOOLEAN NOT NULL DEFAULT false,
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            poll_lease UUID,
            poll_expires_at TIMESTAMPTZ,
            next_poll_at TIMESTAMPTZ NOT NULL DEFAULT (clock_timestamp()+interval '1 minute'),
            CHECK ((registration_state = 'registered' AND payment_url IS NOT NULL)
                OR (registration_state <> 'registered' AND payment_url IS NULL)),
            CHECK ((poll_lease IS NULL) = (poll_expires_at IS NULL))
        );
        CREATE INDEX atol_payment_due_idx ON atol_payment_attempts(next_poll_at);
        ALTER TABLE webinar_entitlements ADD COLUMN payment_order_id BIGINT
            REFERENCES purchase_orders(id);
    """)
    execute_role_sql("GRANT SELECT, INSERT, UPDATE ON atol_payment_attempts TO gl_api_app")
    execute_role_sql("GRANT UPDATE(status, paid_at) ON purchase_orders TO gl_api_app")
    execute_role_sql("GRANT SELECT, INSERT ON payment_events TO gl_api_app")
    execute_role_sql("GRANT USAGE ON SEQUENCE payment_events_id_seq TO gl_api_app")
    execute_role_sql(
        "GRANT INSERT(user_id, webinar_id, source, payment_order_id) ON webinar_entitlements TO gl_api_app"
    )
    execute_role_sql("GRANT DELETE ON webinar_entitlements TO gl_api_app")


def downgrade() -> None:
    raise RuntimeError("Destructive downgrade is disabled; restore a verified backup")
