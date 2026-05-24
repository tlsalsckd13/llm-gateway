import hashlib
import secrets
from datetime import datetime, timedelta, timezone


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def issue_magic_link(conn, user_id: int, purpose: str, issued_by_user_id: int | None, ttl: timedelta) -> str:
    token = new_token()
    expires_at = datetime.now(timezone.utc) + ttl
    await conn.execute(
        """
        INSERT INTO magic_link_tokens (token_hash, user_id, purpose, expires_at, issued_by_user_id)
        VALUES ($1, $2, $3, $4, $5)
        """,
        token_hash(token),
        user_id,
        purpose,
        expires_at,
        issued_by_user_id,
    )
    return token


async def consume_magic_link(conn, token: str, purpose: str):
    row = await conn.fetchrow(
        """
        UPDATE magic_link_tokens
        SET consumed_at = now()
        WHERE token_hash = $1
          AND purpose = $2
          AND consumed_at IS NULL
          AND expires_at > now()
        RETURNING user_id
        """,
        token_hash(token),
        purpose,
    )
    return row["user_id"] if row else None


async def get_magic_link(conn, token: str, purpose: str):
    row = await conn.fetchrow(
        """
        SELECT m.token_hash,
               m.user_id,
               m.purpose,
               m.issued_at,
               m.expires_at,
               m.consumed_at,
               u.email,
               u.display_name
        FROM magic_link_tokens m
        JOIN web_users u ON u.id = m.user_id
        WHERE m.token_hash = $1
          AND m.purpose = $2
        """,
        token_hash(token),
        purpose,
    )
    return dict(row) if row else None


def magic_link_is_usable(row) -> bool:
    if not row:
        return False
    now = datetime.now(timezone.utc)
    return row["consumed_at"] is None and row["expires_at"] > now
