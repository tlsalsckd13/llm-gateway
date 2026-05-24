from datetime import datetime, timedelta, timezone
from decimal import Decimal


def encode(value):
    if isinstance(value, list):
        return [encode(v) for v in value]
    if isinstance(value, dict):
        return {k: encode(v) for k, v in value.items()}
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def records(rows):
    return [encode(dict(row)) for row in rows]


async def overview(conn):
    today = await conn.fetchrow(
        """
        SELECT count(*)::int AS calls,
               COALESCE(sum(input_tokens), 0)::int AS input_tokens,
               COALESCE(sum(output_tokens), 0)::int AS output_tokens,
               COALESCE(sum(cost_usd), 0)::float8 AS cost_usd
        FROM llm_usage
        WHERE ts >= date_trunc('day', now())
          AND blocked_reason IS NULL
        """
    )
    yesterday = await conn.fetchrow(
        """
        SELECT count(*)::int AS calls,
               COALESCE(sum(cost_usd), 0)::float8 AS cost_usd
        FROM llm_usage
        WHERE ts >= date_trunc('day', now()) - interval '1 day'
          AND ts < date_trunc('day', now())
          AND blocked_reason IS NULL
        """
    )
    budgets = await budget_rows(conn)
    dlp = await dlp_rows(conn, limit=5)
    return {
        "today": encode(dict(today)),
        "yesterday": encode(dict(yesterday)),
        "budgets": budgets,
        "recent_dlp": dlp,
    }


async def budget_rows(conn):
    rows = await conn.fetch(
        """
        SELECT b.team_id,
               b.monthly_limit_usd::float8 AS monthly_limit_usd,
               b.daily_limit_usd::float8 AS daily_limit_usd,
               COALESCE(sum(u.cost_usd) FILTER (WHERE u.ts >= date_trunc('day', now()) AND u.blocked_reason IS NULL), 0)::float8 AS today_used_usd,
               COALESCE(sum(u.cost_usd) FILTER (WHERE u.ts >= date_trunc('month', now()) AND u.blocked_reason IS NULL), 0)::float8 AS month_used_usd
        FROM team_budget b
        LEFT JOIN llm_usage u ON u.team_id = b.team_id
        GROUP BY b.team_id, b.monthly_limit_usd, b.daily_limit_usd
        ORDER BY b.team_id
        """
    )
    items = records(rows)
    for item in items:
        daily = item.get("daily_limit_usd") or 0
        monthly = item.get("monthly_limit_usd") or 0
        item["daily_pct"] = round((item["today_used_usd"] / daily) * 100, 1) if daily else 0
        item["monthly_pct"] = round((item["month_used_usd"] / monthly) * 100, 1) if monthly else 0
        item["daily_remaining_usd"] = max(daily - item["today_used_usd"], 0) if daily else None
        item["monthly_remaining_usd"] = max(monthly - item["month_used_usd"], 0) if monthly else None
    return items


async def key_rows(conn):
    rows = await conn.fetch(
        """
        SELECT substring(k.key_hash from 1 for 8) AS key_prefix,
               k.user_id, k.team_id, k.label, k.created_at, k.revoked_at,
               k.issued_via,
               (SELECT max(u.ts) FROM llm_usage u WHERE u.user_id = k.user_id) AS last_used_at
        FROM api_keys k
        ORDER BY k.created_at DESC
        LIMIT 200
        """
    )
    return records(rows)


async def dlp_rows(conn, limit=100, since=None):
    if since:
        rows = await conn.fetch(
            """
            SELECT created_at, action, target_id, metadata
            FROM audit_log
            WHERE action IN ('dlp.block', 'dlp.mask')
              AND created_at >= $1
            ORDER BY created_at DESC
            LIMIT $2
            """,
            since,
            limit,
        )
    else:
        rows = await conn.fetch(
            """
            SELECT created_at, action, target_id, metadata
            FROM audit_log
            WHERE action IN ('dlp.block', 'dlp.mask')
            ORDER BY created_at DESC
            LIMIT $1
            """,
            limit,
        )
    return records(rows)


async def audit_rows(conn, action=None, since=None, limit=100):
    params = []
    where = []
    if action:
        params.append(action)
        where.append(f"action = ${len(params)}")
    if since:
        params.append(since)
        where.append(f"created_at >= ${len(params)}")
    params.append(limit)
    limit_ref = f"${len(params)}"
    where_sql = "WHERE " + " AND ".join(where) if where else ""
    rows = await conn.fetch(
        f"""
        SELECT id, actor_user_id, actor_role, action, target_type, target_id,
               metadata, ip_address::text AS ip_address, created_at
        FROM audit_log
        {where_sql}
        ORDER BY created_at DESC
        LIMIT {limit_ref}
        """,
        *params,
    )
    return records(rows)


def default_range(days=7):
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    return start, end


async def usage_summary(conn, start, end, team=None, user=None, model=None):
    params = [start, end]
    where = ["ts >= $1", "ts < $2", "blocked_reason IS NULL"]
    if team:
        params.append(team)
        where.append(f"team_id = ${len(params)}")
    if user:
        params.append(user)
        where.append(f"user_id = ${len(params)}")
    if model:
        params.append(model)
        where.append(f"model = ${len(params)}")
    where_sql = " AND ".join(where)
    series = await conn.fetch(
        f"""
        SELECT date_trunc('day', ts)::date AS day,
               count(*)::int AS calls,
               COALESCE(sum(cost_usd), 0)::float8 AS cost_usd
        FROM llm_usage
        WHERE {where_sql}
        GROUP BY 1
        ORDER BY 1
        """,
        *params,
    )
    top_users = await conn.fetch(
        f"""
        SELECT user_id, team_id,
               count(*)::int AS calls,
               COALESCE(sum(input_tokens), 0)::int AS input_tokens,
               COALESCE(sum(output_tokens), 0)::int AS output_tokens,
               COALESCE(sum(cost_usd), 0)::float8 AS cost_usd
        FROM llm_usage
        WHERE {where_sql}
        GROUP BY user_id, team_id
        ORDER BY cost_usd DESC, calls DESC
        LIMIT 20
        """,
        *params,
    )
    return {"series": records(series), "top_users": records(top_users)}


async def usage_csv_rows(conn, start, end, team=None, user=None, model=None):
    params = [start, end]
    where = ["ts >= $1", "ts < $2"]
    if team:
        params.append(team)
        where.append(f"team_id = ${len(params)}")
    if user:
        params.append(user)
        where.append(f"user_id = ${len(params)}")
    if model:
        params.append(model)
        where.append(f"model = ${len(params)}")
    rows = await conn.fetch(
        f"""
        SELECT ts, user_id, team_id, model, input_tokens, output_tokens,
               cost_usd::float8 AS cost_usd, latency_ms, blocked_reason
        FROM llm_usage
        WHERE {" AND ".join(where)}
        ORDER BY ts DESC
        LIMIT 5000
        """,
        *params,
    )
    return records(rows)
