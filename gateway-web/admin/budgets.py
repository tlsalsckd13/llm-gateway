from decimal import Decimal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from admin.queries import encode, records
from auth.csrf import CSRF_COOKIE, create_csrf_token, verify_csrf_token
from common.audit import write_audit
from common.flash import set_flash
from common.validation import parse_alert_threshold, parse_money

page_router = APIRouter(prefix="/admin")
api_router = APIRouter(prefix="/api/admin")


def ctx(request: Request, page: str, **kwargs):
    base = {"request": request, "page": page, "user": request.state.user}
    base.update(kwargs)
    return base


def csrf_response(request: Request, template: str, context: dict, status_code: int = 200):
    token = create_csrf_token(request.app.state.session_secret)
    context["csrf_token"] = token
    response = request.app.state.templates.TemplateResponse(template, context, status_code=status_code)
    response.set_cookie(CSRF_COOKIE, token, httponly=True, secure=True, samesite="strict", max_age=3600, path="/admin")
    return response


def pct(used, limit) -> float:
    return round((float(used or 0) / float(limit)) * 100, 1) if limit else 0.0


def remaining(used, limit):
    return max(float(limit) - float(used or 0), 0.0) if limit is not None else None


def alert_level(*values: float, threshold: int = 80) -> str:
    high = max(values or [0])
    if high >= 95:
        return "critical"
    if high >= threshold:
        return "warning"
    return "ok"


def changed_numeric(before, after) -> bool:
    if before is None and after is None:
        return False
    return Decimal(str(before or "0")) != Decimal(str(after or "0"))


def changed_optional_numeric(before, after) -> bool:
    if (before is None) != (after is None):
        return True
    return changed_numeric(before, after)


def parse_team_budget_payload(data: dict) -> tuple[dict | None, dict[str, str]]:
    errors: dict[str, str] = {}
    monthly, err = parse_money(str(data.get("monthly_limit_usd", "0")), "월 예산")
    if err:
        errors["monthly_limit_usd"] = err
    daily, err = parse_money(str(data.get("daily_limit_usd", "0")), "일 예산")
    if err:
        errors["daily_limit_usd"] = err
    threshold, err = parse_alert_threshold(str(data.get("alert_threshold_pct", "80")))
    if err:
        errors["alert_threshold_pct"] = err
    reason = str(data.get("reason") or data.get("change_reason") or "").strip()
    if not reason:
        errors["reason"] = "변경 사유는 필수입니다."
    payload = {
        "monthly_limit_usd": monthly or Decimal("0"),
        "daily_limit_usd": daily or Decimal("0"),
        "alert_threshold_pct": threshold or 80,
        "reason": reason,
    }
    return (None, errors) if errors else (payload, {})


def parse_user_budget_payload(data: dict) -> tuple[dict | None, dict[str, str]]:
    errors: dict[str, str] = {}
    raw_monthly = str(data.get("monthly_limit_usd") or "").strip()
    monthly = None
    if raw_monthly:
        monthly, err = parse_money(raw_monthly, "사용자 월 예산")
        if err:
            errors["monthly_limit_usd"] = err
    reason = str(data.get("reason") or data.get("change_reason") or "").strip()
    if not reason:
        errors["reason"] = "변경 사유는 필수입니다."
    payload = {"monthly_limit_usd": monthly, "reason": reason}
    return (None, errors) if errors else (payload, {})


def decorate_team_budget(row: dict) -> dict:
    row["daily_pct"] = pct(row.get("today_used_usd"), row.get("daily_limit_usd"))
    row["monthly_pct"] = pct(row.get("month_used_usd"), row.get("monthly_limit_usd"))
    row["daily_remaining_usd"] = remaining(row.get("today_used_usd"), row.get("daily_limit_usd"))
    row["monthly_remaining_usd"] = remaining(row.get("month_used_usd"), row.get("monthly_limit_usd"))
    row["alert_level"] = alert_level(row["daily_pct"], row["monthly_pct"], threshold=row.get("alert_threshold_pct") or 80)
    return row


def decorate_user_budget(row: dict) -> dict:
    row["user_monthly_pct"] = pct(row.get("user_month_used_usd"), row.get("user_monthly_limit_usd"))
    row["team_monthly_pct"] = pct(row.get("team_month_used_usd"), row.get("team_monthly_limit_usd"))
    row["user_monthly_remaining_usd"] = remaining(row.get("user_month_used_usd"), row.get("user_monthly_limit_usd"))
    row["team_monthly_remaining_usd"] = remaining(row.get("team_month_used_usd"), row.get("team_monthly_limit_usd"))
    row["effective_monthly_limit_usd"] = row.get("user_monthly_limit_usd") if row.get("has_user_override") else row.get("team_monthly_limit_usd")
    row["effective_month_used_usd"] = row.get("user_month_used_usd") if row.get("has_user_override") else row.get("team_month_used_usd")
    row["effective_monthly_pct"] = pct(row["effective_month_used_usd"], row["effective_monthly_limit_usd"])
    row["alert_level"] = alert_level(row["effective_monthly_pct"], threshold=row.get("team_alert_threshold_pct") or 80)
    return row


async def team_budget_rows(conn):
    rows = await conn.fetch(
        """
        WITH usage AS (
            SELECT team_id,
                   COALESCE(sum(cost_usd) FILTER (WHERE ts >= date_trunc('day', now()) AND blocked_reason IS NULL), 0)::float8 AS today_used_usd,
                   COALESCE(sum(cost_usd) FILTER (WHERE ts >= date_trunc('month', now()) AND blocked_reason IS NULL), 0)::float8 AS month_used_usd
            FROM llm_usage
            GROUP BY team_id
        )
        SELECT t.id,
               t.team_key,
               t.name,
               t.monthly_limit_usd::float8 AS monthly_limit_usd,
               t.daily_limit_usd::float8 AS daily_limit_usd,
               t.alert_threshold_pct,
               COALESCE(u.today_used_usd, 0)::float8 AS today_used_usd,
               COALESCE(u.month_used_usd, 0)::float8 AS month_used_usd,
               t.is_active
        FROM teams t
        LEFT JOIN usage u ON u.team_id = t.team_key
        WHERE t.archived_at IS NULL
        ORDER BY t.name, t.team_key
        """
    )
    return [decorate_team_budget(row) for row in records(rows)]


async def user_budget_rows(conn):
    rows = await conn.fetch(
        """
        WITH user_usage AS (
            SELECT user_id,
                   COALESCE(sum(cost_usd) FILTER (WHERE ts >= date_trunc('month', now()) AND blocked_reason IS NULL), 0)::float8 AS user_month_used_usd
            FROM llm_usage
            GROUP BY user_id
        ),
        team_usage AS (
            SELECT team_id,
                   COALESCE(sum(cost_usd) FILTER (WHERE ts >= date_trunc('month', now()) AND blocked_reason IS NULL), 0)::float8 AS team_month_used_usd
            FROM llm_usage
            GROUP BY team_id
        )
        SELECT wu.id,
               wu.email,
               wu.display_name,
               wu.is_active,
               COALESCE(t.team_key, wu.team_id) AS team_id,
               COALESCE(t.name, wu.team_id) AS team_name,
               COALESCE(t.monthly_limit_usd, tb.monthly_limit_usd)::float8 AS team_monthly_limit_usd,
               COALESCE(t.alert_threshold_pct, 80)::int AS team_alert_threshold_pct,
               ub.monthly_limit_usd::float8 AS user_monthly_limit_usd,
               (ub.user_id IS NOT NULL) AS has_user_override,
               COALESCE(uu.user_month_used_usd, 0)::float8 AS user_month_used_usd,
               COALESCE(tu.team_month_used_usd, 0)::float8 AS team_month_used_usd
        FROM web_users wu
        LEFT JOIN teams t ON t.id = wu.team_id_fk
        LEFT JOIN team_budget tb ON tb.team_id = COALESCE(t.team_key, wu.team_id)
        LEFT JOIN user_budget ub ON ub.user_id = wu.email
        LEFT JOIN user_usage uu ON uu.user_id = wu.email
        LEFT JOIN team_usage tu ON tu.team_id = COALESCE(t.team_key, wu.team_id)
        WHERE wu.archived_at IS NULL
        ORDER BY COALESCE(t.name, wu.team_id), wu.display_name, wu.email
        """
    )
    return [decorate_user_budget(row) for row in records(rows)]


async def budget_history_rows(conn, scope: str | None = None, scope_id: str | None = None, limit: int = 200):
    params = []
    where = []
    if scope:
        params.append(scope)
        where.append(f"bh.scope = ${len(params)}")
    if scope_id:
        params.append(scope_id)
        where.append(f"bh.scope_id = ${len(params)}")
    params.append(limit)
    limit_ref = f"${len(params)}"
    where_sql = "WHERE " + " AND ".join(where) if where else ""
    rows = await conn.fetch(
        f"""
        SELECT bh.id,
               bh.scope,
               bh.scope_id,
               bh.field,
               bh.old_value::float8 AS old_value,
               bh.new_value::float8 AS new_value,
               bh.reason,
               bh.changed_at,
               wu.email AS changed_by_email,
               wu.display_name AS changed_by_name
        FROM budget_history bh
        LEFT JOIN web_users wu ON wu.id = bh.changed_by_user_id
        {where_sql}
        ORDER BY bh.changed_at DESC
        LIMIT {limit_ref}
        """,
        *params,
    )
    return records(rows)


async def update_team_budget(conn, team_id: int, payload: dict, actor_id: int):
    before = await conn.fetchrow(
        """
        SELECT id, team_key, monthly_limit_usd, daily_limit_usd, alert_threshold_pct
        FROM teams
        WHERE id = $1 AND archived_at IS NULL
        """,
        team_id,
    )
    if not before:
        return None, []
    async with conn.transaction():
        row = await conn.fetchrow(
            """
            UPDATE teams
            SET monthly_limit_usd = $2,
                daily_limit_usd = $3,
                alert_threshold_pct = $4,
                updated_by_user_id = $5,
                updated_at = now()
            WHERE id = $1
            RETURNING *
            """,
            team_id,
            payload["monthly_limit_usd"],
            payload["daily_limit_usd"],
            payload["alert_threshold_pct"],
            actor_id,
        )
        await conn.execute(
            """
            INSERT INTO team_budget (team_id, monthly_limit_usd, daily_limit_usd, updated_at)
            VALUES ($1, $2, $3, now())
            ON CONFLICT (team_id) DO UPDATE
            SET monthly_limit_usd = EXCLUDED.monthly_limit_usd,
                daily_limit_usd = EXCLUDED.daily_limit_usd,
                updated_at = now()
            """,
            before["team_key"],
            payload["monthly_limit_usd"],
            payload["daily_limit_usd"],
        )
        changed = []
        for field in ("monthly_limit_usd", "daily_limit_usd", "alert_threshold_pct"):
            if changed_numeric(before[field], row[field]):
                changed.append(field)
                await conn.execute(
                    """
                    INSERT INTO budget_history (scope, scope_id, field, old_value, new_value, changed_by_user_id, reason)
                    VALUES ('team', $1, $2, $3, $4, $5, $6)
                    """,
                    before["team_key"],
                    field,
                    before[field],
                    row[field],
                    actor_id,
                    payload["reason"],
                )
    return dict(row), changed


async def update_user_budget(conn, web_user_id: int, payload: dict, actor_id: int):
    user = await conn.fetchrow(
        """
        SELECT wu.id, wu.email, COALESCE(t.team_key, wu.team_id, 'default') AS team_id
        FROM web_users wu
        LEFT JOIN teams t ON t.id = wu.team_id_fk
        WHERE wu.id = $1 AND wu.archived_at IS NULL
        """,
        web_user_id,
    )
    if not user:
        return None, []
    before = await conn.fetchrow("SELECT monthly_limit_usd FROM user_budget WHERE user_id = $1", user["email"])
    old_value = before["monthly_limit_usd"] if before else None
    new_value = payload["monthly_limit_usd"]
    async with conn.transaction():
        await conn.execute(
            """
            INSERT INTO user_budget (user_id, team_id, monthly_limit_usd, updated_at)
            VALUES ($1, $2, $3, now())
            ON CONFLICT (user_id) DO UPDATE
            SET team_id = EXCLUDED.team_id,
                monthly_limit_usd = EXCLUDED.monthly_limit_usd,
                updated_at = now()
            """,
            user["email"],
            user["team_id"],
            new_value,
        )
        changed = []
        if changed_optional_numeric(old_value, new_value):
            changed.append("monthly_limit_usd")
            await conn.execute(
                """
                INSERT INTO budget_history (scope, scope_id, field, old_value, new_value, changed_by_user_id, reason)
                VALUES ('user', $1, 'monthly_limit_usd', $2, $3, $4, $5)
                """,
                user["email"],
                old_value,
                new_value,
                actor_id,
                payload["reason"],
            )
    updated = await conn.fetchrow(
        """
        SELECT wu.id, wu.email, ub.monthly_limit_usd
        FROM web_users wu
        LEFT JOIN user_budget ub ON ub.user_id = wu.email
        WHERE wu.id = $1
        """,
        web_user_id,
    )
    return dict(updated), changed


async def render_team_budgets(request: Request, *, status_code: int = 200, errors: dict | None = None):
    async with request.app.state.db.acquire() as conn:
        rows = await team_budget_rows(conn)
    return csrf_response(
        request,
        "admin/budgets.html",
        ctx(request, "budgets", active_tab="teams", budgets=rows, errors=errors or {}),
        status_code=status_code,
    )


async def render_user_budgets(request: Request, *, status_code: int = 200, errors: dict | None = None):
    async with request.app.state.db.acquire() as conn:
        rows = await user_budget_rows(conn)
    return csrf_response(
        request,
        "admin/budgets_users.html",
        ctx(request, "budgets", active_tab="users", users=rows, errors=errors or {}),
        status_code=status_code,
    )


@page_router.get("/budgets")
async def budgets_page(request: Request):
    return await render_team_budgets(request)


@page_router.post("/budgets/teams/{team_id:int}")
async def team_budget_update_post(request: Request, team_id: int):
    form = await request.form()
    csrf_ok = verify_csrf_token(
        request.app.state.session_secret,
        form.get("csrf_token"),
        request.cookies.get(CSRF_COOKIE),
    )
    payload, errors = parse_team_budget_payload(form)
    if not csrf_ok:
        errors = {"form": "요청이 만료되었습니다. 다시 시도해주세요."}
    if errors:
        return await render_team_budgets(request, status_code=400, errors={team_id: errors})
    async with request.app.state.db.acquire() as conn:
        row, changed = await update_team_budget(conn, team_id, payload, request.state.user["user_id"])
    if not row:
        raise HTTPException(status_code=404, detail="팀을 찾을 수 없습니다.")
    await write_audit(
        request,
        "budget.team.update",
        target_type="team",
        target_id=row["team_key"],
        metadata={"changed_fields": changed, "reason": payload["reason"]},
    )
    response = RedirectResponse("/admin/budgets", status_code=302)
    return set_flash(response, request.app.state.session_secret, "팀 예산이 수정되었습니다.")


@page_router.get("/budgets/users")
async def user_budgets_page(request: Request):
    return await render_user_budgets(request)


@page_router.post("/budgets/users/{user_id:int}")
async def user_budget_update_post(request: Request, user_id: int):
    form = await request.form()
    csrf_ok = verify_csrf_token(
        request.app.state.session_secret,
        form.get("csrf_token"),
        request.cookies.get(CSRF_COOKIE),
    )
    payload, errors = parse_user_budget_payload(form)
    if not csrf_ok:
        errors = {"form": "요청이 만료되었습니다. 다시 시도해주세요."}
    if errors:
        return await render_user_budgets(request, status_code=400, errors={user_id: errors})
    async with request.app.state.db.acquire() as conn:
        row, changed = await update_user_budget(conn, user_id, payload, request.state.user["user_id"])
    if not row:
        raise HTTPException(status_code=404, detail="사용자를 찾을 수 없습니다.")
    await write_audit(
        request,
        "budget.user.update",
        target_type="web_user",
        target_id=row["email"],
        metadata={"changed_fields": changed, "reason": payload["reason"]},
    )
    response = RedirectResponse("/admin/budgets/users", status_code=302)
    return set_flash(response, request.app.state.session_secret, "사용자 예산이 수정되었습니다.")


@page_router.get("/budgets/history")
async def budget_history_page(request: Request):
    scope = request.query_params.get("scope") or None
    scope_id = request.query_params.get("scope_id") or None
    if scope not in (None, "team", "user"):
        scope = None
    async with request.app.state.db.acquire() as conn:
        rows = await budget_history_rows(conn, scope=scope, scope_id=scope_id)
    return request.app.state.templates.TemplateResponse(
        "admin/budgets_history.html",
        ctx(request, "budgets", active_tab="history", history=rows, scope=scope or "", scope_id=scope_id or ""),
    )


@api_router.get("/budgets")
@api_router.get("/budgets/teams")
async def api_team_budgets(request: Request):
    async with request.app.state.db.acquire() as conn:
        return {"items": await team_budget_rows(conn)}


@api_router.put("/budgets/teams/{team_id:int}")
async def api_team_budget_update(request: Request, team_id: int):
    body = await request.json()
    payload, errors = parse_team_budget_payload(body)
    if errors:
        raise HTTPException(status_code=422, detail=errors)
    async with request.app.state.db.acquire() as conn:
        row, changed = await update_team_budget(conn, team_id, payload, request.state.user["user_id"])
    if not row:
        raise HTTPException(status_code=404, detail="팀을 찾을 수 없습니다.")
    await write_audit(
        request,
        "budget.team.update",
        target_type="team",
        target_id=row["team_key"],
        metadata={"changed_fields": changed, "reason": payload["reason"]},
    )
    return JSONResponse({"item": encode(row), "changed_fields": changed})


@api_router.get("/budgets/users")
async def api_user_budgets(request: Request):
    async with request.app.state.db.acquire() as conn:
        return {"items": await user_budget_rows(conn)}


@api_router.put("/budgets/users/{user_id:int}")
async def api_user_budget_update(request: Request, user_id: int):
    body = await request.json()
    payload, errors = parse_user_budget_payload(body)
    if errors:
        raise HTTPException(status_code=422, detail=errors)
    async with request.app.state.db.acquire() as conn:
        row, changed = await update_user_budget(conn, user_id, payload, request.state.user["user_id"])
    if not row:
        raise HTTPException(status_code=404, detail="사용자를 찾을 수 없습니다.")
    await write_audit(
        request,
        "budget.user.update",
        target_type="web_user",
        target_id=row["email"],
        metadata={"changed_fields": changed, "reason": payload["reason"]},
    )
    return JSONResponse({"item": encode(row), "changed_fields": changed})


@api_router.get("/budgets/history")
async def api_budget_history(request: Request):
    scope = request.query_params.get("scope") or None
    scope_id = request.query_params.get("scope_id") or None
    if scope not in (None, "team", "user"):
        raise HTTPException(status_code=422, detail="scope은 team 또는 user여야 합니다.")
    async with request.app.state.db.acquire() as conn:
        return {"items": await budget_history_rows(conn, scope=scope, scope_id=scope_id)}
