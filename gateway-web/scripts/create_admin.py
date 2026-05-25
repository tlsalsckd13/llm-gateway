import argparse
import asyncio
import getpass
import os
import sys

import asyncpg

sys.path.append(os.path.dirname(os.path.dirname(__file__)))

from auth.password import hash_password, validate_password_policy  # noqa: E402


def read_password(args):
    if args.password_stdin:
        password = sys.stdin.readline().rstrip("\n")
        confirm = password
    else:
        password = getpass.getpass("Admin password: ")
        confirm = getpass.getpass("Confirm password: ")
    if password != confirm:
        raise SystemExit("password confirmation does not match")
    ok, message = validate_password_policy(password)
    if not ok:
        raise SystemExit(message)
    return password


async def upsert_admin(args):
    password = read_password(args)
    conn = await asyncpg.connect(
        host=os.environ["DB_HOST"],
        port=int(os.environ.get("DB_PORT", "5432")),
        database=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        ssl="require",
    )
    try:
        password_hash = hash_password(password)
        row = await conn.fetchrow(
            """
            INSERT INTO web_users (email, display_name, role, team_id, password_hash, is_active)
            VALUES ($1, $2, 'admin', $3, $4, TRUE)
            ON CONFLICT (email) DO UPDATE
            SET display_name = EXCLUDED.display_name,
                role = 'admin',
                team_id = EXCLUDED.team_id,
                password_hash = EXCLUDED.password_hash,
                is_active = TRUE,
                failed_login_count = 0,
                locked_until = NULL
            RETURNING id, email
            """,
            args.email.lower(),
            args.display_name,
            args.team_id,
            password_hash,
        )
        await conn.execute(
            """
            INSERT INTO audit_log (actor_user_id, actor_role, action, target_type, target_id, metadata)
            VALUES ($1, 'system', 'web_user.admin_upsert', 'web_user', $2, '{}'::jsonb)
            """,
            row["id"],
            str(row["id"]),
        )
        print(f"admin user ready: {row['email']} id={row['id']}")
    finally:
        await conn.close()


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--email", default="admin@example.com")
    parser.add_argument("--display-name", default="Admin User")
    parser.add_argument("--team-id", default="infra")
    parser.add_argument("--password-stdin", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(upsert_admin(parse_args()))
