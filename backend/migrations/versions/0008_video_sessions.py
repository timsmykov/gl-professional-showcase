"""Protected recording bindings and bounded watch sessions; no entitlement grants."""

from migrations.roles import execute_role_sql

revision = "0008_video_sessions"
down_revision = "0007_reuse_pending_orders"
branch_labels = None
depends_on = None


def upgrade() -> None:
    execute_role_sql("""
        CREATE TABLE webinar_videos (
            webinar_id BIGINT PRIMARY KEY REFERENCES webinars(id),
            project_id TEXT NOT NULL CHECK (project_id ~ '^[A-Za-z0-9-]{1,64}$'),
            video_id TEXT NOT NULL UNIQUE CHECK (video_id ~ '^[A-Za-z0-9-]{1,64}$'),
            active BOOLEAN NOT NULL DEFAULT false,
            protection_verified_at TIMESTAMPTZ,
            CHECK (NOT active OR protection_verified_at IS NOT NULL)
        );
        CREATE TABLE video_sessions (
            ticket_hash TEXT PRIMARY KEY CHECK (ticket_hash ~ '^[0-9a-f]{64}$'),
            playback_hash TEXT UNIQUE CHECK (playback_hash ~ '^[0-9a-f]{64}$'),
            user_id BIGINT NOT NULL REFERENCES users(id),
            webinar_id BIGINT NOT NULL REFERENCES webinar_videos(webinar_id),
            video_id TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            ticket_expires_at TIMESTAMPTZ NOT NULL,
            consumed_at TIMESTAMPTZ,
            playback_expires_at TIMESTAMPTZ,
            revoked_at TIMESTAMPTZ,
            CHECK (ticket_expires_at > created_at),
            CHECK ((playback_hash IS NULL AND consumed_at IS NULL
                    AND playback_expires_at IS NULL) OR
                   (playback_hash IS NOT NULL AND consumed_at IS NOT NULL
                    AND playback_expires_at > consumed_at))
        );
        CREATE INDEX video_sessions_expiry_idx ON video_sessions(ticket_expires_at);
    """)
    execute_role_sql("GRANT SELECT ON webinar_videos TO gl_api_app")
    execute_role_sql("GRANT SELECT, INSERT ON video_sessions TO gl_api_app")
    execute_role_sql("""
        GRANT UPDATE(playback_hash, consumed_at, playback_expires_at)
        ON video_sessions TO gl_api_app
    """)


def downgrade() -> None:
    raise RuntimeError("Destructive downgrade is disabled; restore a verified backup")
