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
