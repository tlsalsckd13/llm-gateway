from admin.queries import records


async def dashboard(conn, user):
    user_id = user["email"]
    team_id = user.get("team_key") or user.get("team_id") or "default"
    usage = await conn.fetchrow(
        """
        SELECT count(*)::int AS calls,
               COALESCE(sum(input_tokens), 0)::int AS input_tokens,
               COALESCE(sum(output_tokens), 0)::int AS output_tokens,
               COALESCE(sum(cost_usd), 0)::float8 AS cost_usd
        FROM llm_usage
        WHERE user_id = $1
          AND ts >= now() - interval '30 days'
          AND blocked_reason IS NULL
        """,
        user_id,
    )
    keys = await conn.fetchrow(
        """
        SELECT count(*) FILTER (WHERE revoked_at IS NULL AND (expires_at IS NULL OR expires_at > now()))::int AS active_keys,
               count(*)::int AS total_keys
        FROM api_keys
        WHERE user_id = $1
        """,
        user_id,
    )
    budget = await conn.fetchrow(
        """
        WITH usage AS (
            SELECT COALESCE(sum(cost_usd) FILTER (WHERE ts >= date_trunc('month', now()) AND blocked_reason IS NULL), 0)::float8 AS month_used
            FROM llm_usage
            WHERE team_id = $1
        )
        SELECT COALESCE(t.monthly_limit_usd, b.monthly_limit_usd)::float8 AS monthly_limit_usd,
               u.month_used
        FROM usage u
        LEFT JOIN teams t ON t.team_key = $1
        LEFT JOIN team_budget b ON b.team_id = $1
        LIMIT 1
        """,
        team_id,
    )
    return {
        "usage": dict(usage) if usage else {},
        "keys": dict(keys) if keys else {"active_keys": 0, "total_keys": 0},
        "budget": dict(budget) if budget else {"monthly_limit_usd": None, "month_used": 0},
    }


async def my_keys(conn, user):
    rows = await conn.fetch(
        """
        SELECT key_hash,
               key_prefix,
               label,
               created_at,
               expires_at,
               last_used_at,
               revoked_at,
               CASE
                 WHEN revoked_at IS NOT NULL THEN 'revoked'
                 WHEN expires_at IS NOT NULL AND expires_at <= now() THEN 'expired'
                 ELSE 'active'
               END AS status
        FROM api_keys
        WHERE user_id = $1
        ORDER BY created_at DESC
        LIMIT 100
        """,
        user["email"],
    )
    return records(rows)
