"""Watch tickets are opaque bearer capabilities, never Telegram IDs or usernames."""

import hashlib
import re
import secrets

from gl_api.kinescope import resource_id

TICKET_SECONDS = 300
PLAYBACK_SECONDS = 4 * 60 * 60


class VideoDenied(Exception):
    pass


def token_hash(token: str) -> str:
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
        raise VideoDenied("video unavailable")
    return hashlib.sha256(token.encode("ascii")).hexdigest()


class VideoRepository:
    def __init__(self, pool):
        self.pool = pool

    async def issue(self, telegram_id: int, webinar_id: int) -> str:
        ticket = secrets.token_urlsafe(32)
        async with self.pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                "SELECT e.user_id, v.video_id FROM webinar_entitlements e "
                "JOIN users u ON u.id=e.user_id JOIN webinars w ON w.id=e.webinar_id "
                "JOIN webinar_videos v ON v.webinar_id=e.webinar_id "
                "WHERE u.telegram_id=$1 AND w.id=$2 AND w.active AND v.active "
                "AND v.protection_verified_at IS NOT NULL",
                telegram_id,
                webinar_id,
            )
            if row is None:
                raise VideoDenied("video unavailable")
            await connection.execute(
                "INSERT INTO video_sessions(ticket_hash,user_id,webinar_id,video_id,"
                "ticket_expires_at) VALUES ($1,$2,$3,$4,"
                "clock_timestamp()+$5*interval '1 second')",
                token_hash(ticket),
                row["user_id"],
                webinar_id,
                row["video_id"],
                TICKET_SECONDS,
            )
        return ticket

    async def exchange(self, ticket: str) -> dict[str, str]:
        digest = token_hash(ticket)
        playback = secrets.token_urlsafe(32)
        async with self.pool.acquire() as connection, connection.transaction():
            # UPDATE serializes concurrent exchanges. Exactly one consumes the ticket.
            row = await connection.fetchrow(
                "UPDATE video_sessions s SET playback_hash=$2,consumed_at=clock_timestamp(),"
                "playback_expires_at=clock_timestamp()+$3*interval '1 second' "
                "WHERE s.ticket_hash=$1 AND s.consumed_at IS NULL AND s.revoked_at IS NULL "
                "AND s.ticket_expires_at>clock_timestamp() "
                "AND EXISTS(SELECT 1 FROM webinar_entitlements e "
                "JOIN webinars w ON w.id=e.webinar_id "
                "JOIN webinar_videos v ON v.webinar_id=e.webinar_id "
                "WHERE e.user_id=s.user_id AND e.webinar_id=s.webinar_id "
                "AND w.active AND v.active AND v.protection_verified_at IS NOT NULL "
                "AND v.video_id=s.video_id) RETURNING s.video_id",
                digest,
                token_hash(playback),
                PLAYBACK_SECONDS,
            )
            if row is None:
                raise VideoDenied("video unavailable")
        return {"video_id": row["video_id"], "token": playback}

    async def authorize(self, playback: str, video_id: str) -> bool:
        digest = token_hash(playback)
        try:
            resource_id(video_id)
        except ValueError:
            return False
        return bool(
            await self.pool.fetchval(
                "SELECT EXISTS(SELECT 1 FROM video_sessions s "
                "JOIN webinar_entitlements e ON e.user_id=s.user_id AND e.webinar_id=s.webinar_id "
                "JOIN webinars w ON w.id=s.webinar_id "
                "JOIN webinar_videos v ON v.webinar_id=s.webinar_id "
                "WHERE s.playback_hash=$1 AND s.video_id=$2 AND v.video_id=s.video_id "
                "AND s.revoked_at IS NULL AND s.playback_expires_at>clock_timestamp() "
                "AND w.active AND v.active AND v.protection_verified_at IS NOT NULL)",
                digest,
                video_id,
            )
        )
