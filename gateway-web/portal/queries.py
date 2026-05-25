from admin.queries import records


def _pct(used, limit) -> float:
    return round((float(used or 0) / float(limit)) * 100, 1) if limit else 0.0


def _remaining(used, limit):
    return max(float(limit) - float(used or 0), 0.0) if limit is not None else None


def _budget_alert_level(budget: dict) -> str:
    threshold = (budget.get("team_alert_threshold_pct") or 80)
    high = max(
        budget.get("team_monthly_pct") or 0,
        budget.get("team_daily_pct") or 0,
        budget.get("user_monthly_pct") or 0,
    )
    if high >= 95:
        return "critical"
    if high >= threshold:
        return "warning"
    return "ok"


def _decorate_budget(row) -> dict:
    budget = dict(row) if row else {
        "team_monthly_limit_usd": None,
        "team_daily_limit_usd": None,
        "team_alert_threshold_pct": 80,
        "team_month_used_usd": 0,
        "team_day_used_usd": 0,
        "user_monthly_limit_usd": None,
        "has_user_override": False,
        "user_month_used_usd": 0,
    }
    budget["team_monthly_pct"] = _pct(budget.get("team_month_used_usd"), budget.get("team_monthly_limit_usd"))
    budget["team_daily_pct"] = _pct(budget.get("team_day_used_usd"), budget.get("team_daily_limit_usd"))
    budget["team_monthly_remaining_usd"] = _remaining(budget.get("team_month_used_usd"), budget.get("team_monthly_limit_usd"))
    budget["team_daily_remaining_usd"] = _remaining(budget.get("team_day_used_usd"), budget.get("team_daily_limit_usd"))
    budget["user_monthly_pct"] = _pct(budget.get("user_month_used_usd"), budget.get("user_monthly_limit_usd"))
    budget["user_monthly_remaining_usd"] = _remaining(budget.get("user_month_used_usd"), budget.get("user_monthly_limit_usd"))
    if budget.get("has_user_override") and budget.get("user_monthly_limit_usd") is not None:
        budget["monthly_limit_usd"] = budget.get("user_monthly_limit_usd")
        budget["month_used"] = budget.get("user_month_used_usd")
        budget["monthly_remaining_usd"] = budget.get("user_monthly_remaining_usd")
        budget["monthly_pct"] = budget.get("user_monthly_pct")
    else:
        budget["monthly_limit_usd"] = budget.get("team_monthly_limit_usd")
        budget["month_used"] = budget.get("team_month_used_usd")
        budget["monthly_remaining_usd"] = budget.get("team_monthly_remaining_usd")
        budget["monthly_pct"] = budget.get("team_monthly_pct")
    budget["alert_level"] = _budget_alert_level(budget)
    return budget


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
        WITH team_usage AS (
            SELECT COALESCE(sum(cost_usd) FILTER (WHERE ts >= date_trunc('month', now()) AND blocked_reason IS NULL), 0)::float8 AS month_used,
                   COALESCE(sum(cost_usd) FILTER (WHERE ts >= date_trunc('day', now()) AND blocked_reason IS NULL), 0)::float8 AS day_used
            FROM llm_usage
            WHERE team_id = $1
        ),
        user_usage AS (
            SELECT COALESCE(sum(cost_usd) FILTER (WHERE ts >= date_trunc('month', now()) AND blocked_reason IS NULL), 0)::float8 AS month_used
            FROM llm_usage
            WHERE user_id = $2
        )
        SELECT COALESCE(t.monthly_limit_usd, b.monthly_limit_usd)::float8 AS team_monthly_limit_usd,
               COALESCE(t.daily_limit_usd, b.daily_limit_usd)::float8 AS team_daily_limit_usd,
               COALESCE(t.alert_threshold_pct, 80)::int AS team_alert_threshold_pct,
               tu.month_used AS team_month_used_usd,
               tu.day_used AS team_day_used_usd,
               ub.monthly_limit_usd::float8 AS user_monthly_limit_usd,
               (ub.user_id IS NOT NULL) AS has_user_override,
               uu.month_used AS user_month_used_usd
        FROM team_usage tu
        CROSS JOIN user_usage uu
        LEFT JOIN teams t ON t.team_key = $1
        LEFT JOIN team_budget b ON b.team_id = $1
        LEFT JOIN user_budget ub ON ub.user_id = $2
        LIMIT 1
        """,
        team_id,
        user_id,
    )
    return {
        "usage": dict(usage) if usage else {},
        "keys": dict(keys) if keys else {"active_keys": 0, "total_keys": 0},
        "budget": _decorate_budget(budget),
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
