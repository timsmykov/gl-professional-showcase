"""Limit the current read-only runtime to the webinar catalog."""

from migrations.roles import execute_role_sql

revision = "0003_runtime_read_privileges"
down_revision = "0002_purchase_records"
branch_labels = None
depends_on = None


def upgrade() -> None:
    execute_role_sql(
        "REVOKE SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public FROM gl_api_app"
    )
    execute_role_sql("REVOKE USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public FROM gl_api_app")
    execute_role_sql(
        "ALTER DEFAULT PRIVILEGES FOR ROLE gl_api_migrator IN SCHEMA public "
        "REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM gl_api_app"
    )
    execute_role_sql(
        "ALTER DEFAULT PRIVILEGES FOR ROLE gl_api_migrator IN SCHEMA public "
        "REVOKE USAGE, SELECT ON SEQUENCES FROM gl_api_app"
    )
    execute_role_sql("GRANT SELECT ON TABLE public.webinars TO gl_api_app")


def downgrade() -> None:
    raise RuntimeError("Privilege broadening by downgrade is disabled")
