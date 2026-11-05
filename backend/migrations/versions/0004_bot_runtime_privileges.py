"""Allow the API runtime, but not the bot, to execute legacy bot operations."""

from migrations.roles import execute_role_sql

revision = "0004_bot_runtime_privileges"
down_revision = "0003_runtime_read_privileges"
branch_labels = None
depends_on = None


def upgrade() -> None:
    execute_role_sql("""
        GRANT SELECT ON users, user_sections, services, appointment_slots,
            booking_requests, products, webinars, webinar_entitlements,
            processed_updates TO gl_api_app
    """)
    execute_role_sql("""
        GRANT INSERT, UPDATE ON users, user_sections, booking_requests,
            processed_updates TO gl_api_app
    """)
    execute_role_sql("GRANT DELETE ON processed_updates TO gl_api_app")
    execute_role_sql("GRANT USAGE ON SEQUENCE users_id_seq, booking_requests_id_seq TO gl_api_app")


def downgrade() -> None:
    raise RuntimeError("Privilege downgrade is disabled; restore a verified backup")
