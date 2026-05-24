import hashlib
import secrets
from datetime import datetime, timezone


API_KEY_PREFIX = "kcs-poc-"


def generate_api_key() -> str:
    return f"{API_KEY_PREFIX}{secrets.token_hex(32)}"


def hash_api_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def display_prefix(api_key: str) -> str:
    return api_key[:18]


def parse_expires_at(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


async def issue_api_key(conn, *, user_email: str, label: str, expires_at, issued_by_user_id: int | None, issued_via: str):
    user = await conn.fetchrow(
        """
        SELECT u.id, u.email, u.team_id, u.team_id_fk, t.team_key
        FROM web_users u
        LEFT JOIN teams t ON t.id = u.team_id_fk
        WHERE lower(u.email) = lower($1)
          AND u.is_active = TRUE
          AND u.archived_at IS NULL
        """,
        user_email,
    )
    if not user:
        raise ValueError("user_not_found")
    plain_key = generate_api_key()
    key_hash = hash_api_key(plain_key)
    key_prefix = display_prefix(plain_key)
    team_key = user["team_key"] or user["team_id"] or "default"
    row = await conn.fetchrow(
        """
        INSERT INTO api_keys (
          key_hash, user_id, team_id, label, expires_at,
          key_prefix, issued_by_user_id, issued_via
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        RETURNING key_hash, key_prefix, user_id, team_id, label, created_at, expires_at, revoked_at, last_used_at, issued_via
        """,
        key_hash,
        user["email"],
        team_key,
        label,
        expires_at,
        key_prefix,
        issued_by_user_id,
        issued_via,
    )
    return plain_key, dict(row)


async def revoke_api_key(conn, *, key_hash: str, actor_user_id: int | None, owner_email: str | None = None):
    if owner_email:
        row = await conn.fetchrow(
            """
            UPDATE api_keys
            SET revoked_at = COALESCE(revoked_at, now()),
                revoked_by_user_id = COALESCE(revoked_by_user_id, $2)
            WHERE key_hash = $1
              AND lower(user_id) = lower($3)
            RETURNING key_hash, key_prefix, user_id, team_id, label, created_at, expires_at, revoked_at, last_used_at, issued_via
            """,
            key_hash,
            actor_user_id,
            owner_email,
        )
    else:
        row = await conn.fetchrow(
            """
            UPDATE api_keys
            SET revoked_at = COALESCE(revoked_at, now()),
                revoked_by_user_id = COALESCE(revoked_by_user_id, $2)
            WHERE key_hash = $1
            RETURNING key_hash, key_prefix, user_id, team_id, label, created_at, expires_at, revoked_at, last_used_at, issued_via
            """,
            key_hash,
            actor_user_id,
        )
    return dict(row) if row else None


async def selectable_users(conn):
    rows = await conn.fetch(
        """
        SELECT u.id, u.email, u.display_name, COALESCE(t.team_key, u.team_id) AS team_id
        FROM web_users u
        LEFT JOIN teams t ON t.id = u.team_id_fk
        WHERE u.is_active = TRUE
          AND u.archived_at IS NULL
        ORDER BY u.display_name, u.email
        LIMIT 500
        """
    )
    return [dict(row) for row in rows]
