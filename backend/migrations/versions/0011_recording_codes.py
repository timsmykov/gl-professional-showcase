"""Explicit public recording codes; existing recordings remain unmapped."""

from migrations.roles import execute_role_sql

revision = "0011_recording_codes"
down_revision = "0010_receipt_ledger"
branch_labels = None
depends_on = None


def upgrade() -> None:
    execute_role_sql("""
        ALTER TABLE webinars ADD COLUMN public_code TEXT UNIQUE
            CHECK (public_code ~ '^[A-Za-z0-9_-]{1,64}$');
    """)


def downgrade() -> None:
    raise RuntimeError("Destructive downgrade is disabled; restore a verified backup")
