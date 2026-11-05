"""Baseline the current bot schema, with no destructive operations."""

from pathlib import Path

from alembic import op

revision = "0001_legacy_bot_schema"
down_revision = None
branch_labels = None
depends_on = None


TABLES = (
    "users",
    "user_sections",
    "services",
    "appointment_slots",
    "booking_requests",
    "products",
    "webinars",
    "webinar_entitlements",
    "processed_updates",
)


def upgrade() -> None:
    connection = op.get_bind()
    existing = connection.exec_driver_sql(
        "SELECT tablename FROM pg_tables WHERE schemaname = current_schema() "
        "AND tablename = ANY(%s)",
        (list(TABLES),),
    ).fetchall()
    if existing:
        raise RuntimeError(
            "Existing bot tables detected. Verify sql/001_init.sql against the live schema, "
            "then explicitly stamp 0001_legacy_bot_schema before upgrading."
        )
    sql = (Path(__file__).resolve().parents[1] / "legacy_001_init.sql").read_text().strip()
    sql = sql.removeprefix("BEGIN;").removesuffix("COMMIT;").strip()
    connection.exec_driver_sql(sql)


def downgrade() -> None:
    raise RuntimeError("Destructive downgrade is disabled; restore a verified backup")
