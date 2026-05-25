from decimal import Decimal
import logging

import asyncpg
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from admin.queries import encode, records
from auth.csrf import CSRF_COOKIE, create_csrf_token, verify_csrf_token
from common.audit import write_audit
from common.flash import set_flash
from common.pagination import envelope, page_from_query
from common.validation import parse_alert_threshold, parse_money, validate_team_key

page_router = APIRouter(prefix="/admin")
api_router = APIRouter(prefix="/api/admin")
log = logging.getLogger(__name__)

BEDROCK_MODELS = [
    "global.anthropic.claude-opus-4-7",
    "global.anthropic.claude-haiku-4-5-20251001-v1:0",
]


class TeamCreate(BaseModel):
    team_key: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=160)
    description: str | None = None
    default_model: str = BEDROCK_MODELS[0]
    monthly_limit_usd: Decimal = Decimal("0")
    daily_limit_usd: Decimal = Decimal("0")
    alert_threshold_pct: int = Field(default=80, ge=1, le=100)


class TeamUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    description: str | None = None
    default_model: str = BEDROCK_MODELS[0]
    monthly_limit_usd: Decimal = Decimal("0")
    daily_limit_usd: Decimal = Decimal("0")
    alert_threshold_pct: int = Field(default=80, ge=1, le=100)
    change_reason: str | None = None
    is_active: bool = True


def ctx(request: Request, page: str, **kwargs):
    base = {"request": request, "page": page, "user": request.state.user, "models": BEDROCK_MODELS}
    base.update(kwargs)
    return base


def csrf_response(request: Request, template: str, context: dict, status_code: int = 200):
    token = create_csrf_token(request.app.state.session_secret)
    context["csrf_token"] = token
    response = request.app.state.templates.TemplateResponse(template, context, status_code=status_code)
    response.set_cookie(CSRF_COOKIE, token, httponly=True, secure=True, samesite="strict", max_age=3600, path="/admin")
    return response


def validate_team_payload(data: dict, *, creating: bool) -> tuple[dict | None, dict[str, str]]:
    errors: dict[str, str] = {}
    payload: dict = {}

    if creating:
        team_key = str(data.get("team_key", "")).strip()
        ok, message = validate_team_key(team_key)
        if not ok:
            errors["team_key"] = message or "team_key를 확인해주세요."
        payload["team_key"] = team_key

    name = str(data.get("name", "")).strip()
    if not name:
        errors["name"] = "팀 이름은 필수입니다."
    payload["name"] = name
    payload["description"] = str(data.get("description", "")).strip() or None

    default_model = str(data.get("default_model") or BEDROCK_MODELS[0])
    if default_model not in BEDROCK_MODELS:
        errors["default_model"] = "지원하는 Bedrock 모델을 선택해주세요."
    payload["default_model"] = default_model

    monthly, err = parse_money(str(data.get("monthly_limit_usd", "0")), "월 예산")
    if err:
        errors["monthly_limit_usd"] = err
    daily, err = parse_money(str(data.get("daily_limit_usd", "0")), "일 예산")
    if err:
        errors["daily_limit_usd"] = err
    threshold, err = parse_alert_threshold(str(data.get("alert_threshold_pct", "80")))
    if err:
        errors["alert_threshold_pct"] = err

    payload["monthly_limit_usd"] = monthly or Decimal("0")
    payload["daily_limit_usd"] = daily or Decimal("0")
    payload["alert_threshold_pct"] = threshold or 80
    payload["is_active"] = str(data.get("is_active", "true")).lower() not in ("false", "0", "off")
    payload["change_reason"] = str(data.get("change_reason", "")).strip() or None

    return (None, errors) if errors else (payload, {})


def budget_changed(before: dict, after: dict) -> bool:
    return (
        Decimal(str(before.get("monthly_limit_usd") or "0")) != Decimal(str(after.get("monthly_limit_usd") or "0"))
        or Decimal(str(before.get("daily_limit_usd") or "0")) != Decimal(str(after.get("daily_limit_usd") or "0"))
        or int(before.get("alert_threshold_pct") or 80) != int(after.get("alert_threshold_pct") or 80)
    )


def budget_field_changed(field: str, before, after) -> bool:
    if field == "alert_threshold_pct":
        return int(before or 80) != int(after or 80)
    return Decimal(str(before or "0")) != Decimal(str(after or "0"))


async def list_teams(conn, q: str | None, page, sort: str):
    params = []
    where = ["t.archived_at IS NULL"]
    if q:
        params.append(f"%{q.lower()}%")
        where.append(f"(lower(t.team_key) LIKE ${len(params)} OR lower(t.name) LIKE ${len(params)})")
    where_sql = " AND ".join(where)
    order_by = {
        "name": "t.name ASC",
        "created_at": "t.created_at DESC",
        "user_count": "user_count DESC, t.name ASC",
    }.get(sort, "t.name ASC")
    total = await conn.fetchval(f"SELECT count(*) FROM teams t WHERE {where_sql}", *params)
    params.extend([page.per_page, page.offset])
    rows = await conn.fetch(
        f"""
        WITH usage AS (
            SELECT team_id,
                   COALESCE(sum(cost_usd) FILTER (WHERE ts >= date_trunc('month', now()) AND blocked_reason IS NULL), 0)::float8 AS month_used_usd
            FROM llm_usage
            GROUP BY team_id
        )
        SELECT t.id,
               t.team_key,
               t.name,
               t.description,
               t.monthly_limit_usd::float8 AS monthly_limit_usd,
               t.daily_limit_usd::float8 AS daily_limit_usd,
               t.alert_threshold_pct,
               t.is_active,
               t.archived_at,
               t.created_at,
               count(DISTINCT wu.id)::int AS user_count,
               COALESCE(u.month_used_usd, 0)::float8 AS month_used_usd
        FROM teams t
        LEFT JOIN web_users wu ON wu.archived_at IS NULL AND (wu.team_id_fk = t.id OR wu.team_id = t.team_key)
        LEFT JOIN usage u ON u.team_id = t.team_key
        WHERE {where_sql}
        GROUP BY t.id, u.month_used_usd
        ORDER BY {order_by}
        LIMIT ${len(params)-1} OFFSET ${len(params)}
        """,
        *params,
    )
    items = records(rows)
    for item in items:
        limit = item.get("monthly_limit_usd") or 0
        used = item.get("month_used_usd") or 0
        item["monthly_pct"] = round((used / limit) * 100, 1) if limit else 0
    return envelope(items, int(total or 0), page)


async def get_team(conn, team_id: int):
    row = await conn.fetchrow(
        """
        WITH user_counts AS (
            SELECT t.id AS team_id, count(wu.id)::int AS user_count
            FROM teams t
            LEFT JOIN web_users wu ON wu.archived_at IS NULL AND (wu.team_id_fk = t.id OR wu.team_id = t.team_key)
            WHERE t.id = $1
            GROUP BY t.id
        ),
        usage AS (
            SELECT t.id AS team_id,
                   COALESCE(sum(u.cost_usd) FILTER (WHERE u.ts >= date_trunc('month', now()) AND u.blocked_reason IS NULL), 0)::float8 AS month_used_usd,
                   count(u.id) FILTER (WHERE u.ts >= date_trunc('month', now()) AND u.blocked_reason IS NULL)::int AS month_calls
            FROM teams t
            LEFT JOIN llm_usage u ON u.team_id = t.team_key
            WHERE t.id = $1
            GROUP BY t.id
        )
        SELECT t.id,
               t.team_key,
               t.name,
               t.description,
               t.default_model,
               t.monthly_limit_usd::float8 AS monthly_limit_usd,
               t.daily_limit_usd::float8 AS daily_limit_usd,
               t.alert_threshold_pct,
               t.is_active,
               t.archived_at,
               t.created_at,
               t.updated_at,
               COALESCE(uc.user_count, 0)::int AS user_count,
               COALESCE(us.month_used_usd, 0)::float8 AS month_used_usd,
               COALESCE(us.month_calls, 0)::int AS month_calls
        FROM teams t
        LEFT JOIN user_counts uc ON uc.team_id = t.id
        LEFT JOIN usage us ON us.team_id = t.id
        WHERE t.id = $1
        """,
        team_id,
    )
    return encode(dict(row)) if row else None


async def team_users(conn, team_id: int):
    rows = await conn.fetch(
        """
        SELECT id, email, display_name, role, department, is_active, last_login_at
        FROM web_users
        WHERE archived_at IS NULL
          AND (team_id_fk = $1 OR team_id = (SELECT team_key FROM teams WHERE id = $1))
        ORDER BY display_name, email
        LIMIT 100
        """,
        team_id,
    )
    return records(rows)


async def team_keys(conn, team_id: int):
    rows = await conn.fetch(
        """
        SELECT COALESCE(k.key_prefix, substring(k.key_hash from 1 for 12)) AS key_prefix,
               k.user_id, k.label, k.created_at, k.expires_at, k.last_used_at, k.revoked_at
        FROM api_keys k
        JOIN teams t ON t.id = $1
        WHERE (k.team_id = t.team_key OR k.user_id IN (
            SELECT email FROM web_users WHERE team_id_fk = t.id
        ))
          AND k.revoked_at IS NULL
        ORDER BY k.created_at DESC
        LIMIT 100
        """,
        team_id,
    )
    return records(rows)


async def team_budget_history(conn, team):
    rows = await conn.fetch(
        """
        SELECT field, old_value::float8 AS old_value, new_value::float8 AS new_value,
               changed_by_user_id, changed_at, reason
        FROM budget_history
        WHERE scope = 'team' AND scope_id = $1
        ORDER BY changed_at DESC
        LIMIT 50
        """,
        team["team_key"],
    )
    return records(rows)


async def create_team_record(conn, payload: dict, actor_id: int):
    row = await conn.fetchrow(
        """
        INSERT INTO teams (
          team_key, name, description, default_model,
          monthly_limit_usd, daily_limit_usd, alert_threshold_pct,
          created_by_user_id, updated_by_user_id
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $8)
        RETURNING *
        """,
        payload["team_key"],
        payload["name"],
        payload["description"],
        payload["default_model"],
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
        payload["team_key"],
        payload["monthly_limit_usd"],
        payload["daily_limit_usd"],
    )
    return dict(row)


async def update_team_record(conn, team_id: int, payload: dict, actor_id: int):
    before = await conn.fetchrow("SELECT * FROM teams WHERE id = $1", team_id)
    if not before:
        return None, None
    row = await conn.fetchrow(
        """
        UPDATE teams
        SET name = $2,
            description = $3,
            default_model = $4,
            monthly_limit_usd = $5,
            daily_limit_usd = $6,
            alert_threshold_pct = $7,
            is_active = $8,
            updated_by_user_id = $9,
            updated_at = now()
        WHERE id = $1
        RETURNING *
        """,
        team_id,
        payload["name"],
        payload["description"],
        payload["default_model"],
        payload["monthly_limit_usd"],
        payload["daily_limit_usd"],
        payload["alert_threshold_pct"],
        payload["is_active"],
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
    fields = ("monthly_limit_usd", "daily_limit_usd", "alert_threshold_pct")
    for field in fields:
        if budget_field_changed(field, before[field], row[field]):
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
                payload["change_reason"],
            )
    return dict(row), dict(before)


async def archive_team_record(conn, team_id: int, actor_id: int):
    users = await conn.fetchval(
        """
        SELECT count(*)
        FROM web_users wu
        JOIN teams t ON t.id = $1
        WHERE wu.archived_at IS NULL
          AND (wu.team_id_fk = t.id OR wu.team_id = t.team_key)
        """,
        team_id,
    )
    if users:
        return None, int(users)
    row = await conn.fetchrow(
        """
        UPDATE teams
        SET is_active = FALSE,
            archived_at = COALESCE(archived_at, now()),
            updated_by_user_id = $2,
            updated_at = now()
        WHERE id = $1
        RETURNING *
        """,
        team_id,
        actor_id,
    )
    return dict(row) if row else None, 0


@page_router.get("/teams")
async def teams_page(request: Request):
    page = page_from_query(request.query_params)
    q = request.query_params.get("q") or None
    sort = request.query_params.get("sort") or "name"
    async with request.app.state.db.acquire() as conn:
        data = await list_teams(conn, q, page, sort)
    return request.app.state.templates.TemplateResponse("admin/teams.html", ctx(request, "teams", teams=data, q=q or "", sort=sort))


@page_router.get("/teams/new")
async def team_new_page(request: Request):
    values = {"default_model": BEDROCK_MODELS[0], "monthly_limit_usd": "0", "daily_limit_usd": "0", "alert_threshold_pct": "80"}
    return csrf_response(request, "admin/team_form.html", ctx(request, "teams", mode="new", values=values, errors={}))


@page_router.post("/teams")
async def team_create_post(request: Request):
    form = await request.form()
    if not verify_csrf_token(request.app.state.session_secret, form.get("csrf_token"), request.cookies.get(CSRF_COOKIE)):
        return csrf_response(request, "admin/team_form.html", ctx(request, "teams", mode="new", values=dict(form), errors={"form": "요청이 만료되었습니다. 다시 시도해주세요."}), status_code=400)
    payload, errors = validate_team_payload(dict(form), creating=True)
    if errors:
        return csrf_response(request, "admin/team_form.html", ctx(request, "teams", mode="new", values=dict(form), errors=errors), status_code=400)
    actor_id = request.state.user["user_id"]
    async with request.app.state.db.acquire() as conn:
        try:
            team = await create_team_record(conn, payload, actor_id)
        except asyncpg.UniqueViolationError:
            return csrf_response(request, "admin/team_form.html", ctx(request, "teams", mode="new", values=dict(form), errors={"team_key": "이미 존재하는 team_key입니다."}), status_code=409)
    try:
        await write_audit(request, "team.create", target_type="team", target_id=team["team_key"], metadata={"after": encode(team)})
    except Exception:
        log.exception("team create audit failed: team=%s", team["team_key"])
    response = RedirectResponse("/admin/teams", status_code=302)
    response.delete_cookie(CSRF_COOKIE, path="/admin")
    return set_flash(response, request.app.state.session_secret, "팀이 생성되었습니다.")


@page_router.get("/teams/{team_id:int}")
async def team_detail_page(request: Request, team_id: int):
    async with request.app.state.db.acquire() as conn:
        team = await get_team(conn, team_id)
        if not team:
            raise HTTPException(status_code=404, detail="팀을 찾을 수 없습니다")
        users = await team_users(conn, team_id)
        keys = await team_keys(conn, team_id)
        history = await team_budget_history(conn, team)
    return csrf_response(
        request,
        "admin/team_detail.html",
        ctx(request, "teams", team=team, users=users, keys=keys, history=history, errors={}),
    )


@page_router.post("/teams/{team_id:int}")
async def team_update_post(request: Request, team_id: int):
    form = await request.form()
    if not verify_csrf_token(request.app.state.session_secret, form.get("csrf_token"), request.cookies.get(CSRF_COOKIE)):
        async with request.app.state.db.acquire() as conn:
            team = await get_team(conn, team_id)
        return csrf_response(request, "admin/team_detail.html", ctx(request, "teams", team=team, users=[], keys=[], history=[], errors={"form": "요청이 만료되었습니다. 다시 시도해주세요."}), status_code=400)
    payload, errors = validate_team_payload(dict(form), creating=False)
    actor_id = request.state.user["user_id"]
    async with request.app.state.db.acquire() as conn:
        before = await conn.fetchrow("SELECT * FROM teams WHERE id = $1", team_id)
        if not before:
            raise HTTPException(status_code=404, detail="팀을 찾을 수 없습니다")
        if budget_changed(dict(before), payload) and not payload["change_reason"]:
            errors["change_reason"] = "예산 변경 사유를 입력해주세요."
        if errors:
            team = await get_team(conn, team_id)
            users = await team_users(conn, team_id)
            keys = await team_keys(conn, team_id)
            history = await team_budget_history(conn, team)
            return csrf_response(request, "admin/team_detail.html", ctx(request, "teams", team=team, users=users, keys=keys, history=history, errors=errors), status_code=400)
        team, before_dict = await update_team_record(conn, team_id, payload, actor_id)
    await write_audit(request, "team.update", target_type="team", target_id=team["team_key"], metadata={"before": encode(before_dict), "after": encode(team)})
    response = RedirectResponse(f"/admin/teams/{team_id}", status_code=302)
    response.delete_cookie(CSRF_COOKIE, path="/admin")
    return set_flash(response, request.app.state.session_secret, "팀 정보가 수정되었습니다.")


@page_router.post("/teams/{team_id:int}/archive")
async def team_archive_post(request: Request, team_id: int):
    form = await request.form()
    if not verify_csrf_token(request.app.state.session_secret, form.get("csrf_token"), request.cookies.get(CSRF_COOKIE)):
        raise HTTPException(status_code=400, detail="요청이 만료되었습니다")
    actor_id = request.state.user["user_id"]
    async with request.app.state.db.acquire() as conn:
        team, users = await archive_team_record(conn, team_id, actor_id)
    if users:
        response = RedirectResponse(f"/admin/teams/{team_id}", status_code=302)
        return set_flash(response, request.app.state.session_secret, "사용자가 있는 팀은 archive할 수 없습니다.", "error")
    if not team:
        raise HTTPException(status_code=404, detail="팀을 찾을 수 없습니다")
    await write_audit(request, "team.archive", target_type="team", target_id=team["team_key"], metadata={"after": encode(team)})
    response = RedirectResponse("/admin/teams", status_code=302)
    response.delete_cookie(CSRF_COOKIE, path="/admin")
    return set_flash(response, request.app.state.session_secret, "팀이 archive되었습니다.")


@api_router.get("/teams")
async def api_list_teams(request: Request):
    page = page_from_query(request.query_params)
    q = request.query_params.get("q") or None
    sort = request.query_params.get("sort") or "name"
    async with request.app.state.db.acquire() as conn:
        return await list_teams(conn, q, page, sort)


@api_router.post("/teams")
async def api_create_team(request: Request, payload: TeamCreate):
    ok, message = validate_team_key(payload.team_key)
    if not ok:
        raise HTTPException(status_code=422, detail=message)
    if payload.default_model not in BEDROCK_MODELS:
        raise HTTPException(status_code=422, detail="지원하는 Bedrock 모델을 선택해주세요.")
    data = payload.model_dump()
    actor_id = request.state.user["user_id"]
    async with request.app.state.db.acquire() as conn:
        try:
            team = await create_team_record(conn, data, actor_id)
        except asyncpg.UniqueViolationError as exc:
            raise HTTPException(status_code=409, detail="이미 존재하는 team_key입니다.") from exc
    await write_audit(request, "team.create", target_type="team", target_id=team["team_key"], metadata={"after": encode(team)})
    return JSONResponse(status_code=201, content=encode(team))


@api_router.get("/teams/{team_id}")
async def api_get_team(request: Request, team_id: int):
    async with request.app.state.db.acquire() as conn:
        team = await get_team(conn, team_id)
        if not team:
            raise HTTPException(status_code=404, detail="팀을 찾을 수 없습니다")
        team["users"] = await team_users(conn, team_id)
        team["api_keys"] = await team_keys(conn, team_id)
        team["budget_history"] = await team_budget_history(conn, team)
    return team


@api_router.put("/teams/{team_id}")
async def api_update_team(request: Request, team_id: int, payload: TeamUpdate):
    data = payload.model_dump()
    if data["default_model"] not in BEDROCK_MODELS:
        raise HTTPException(status_code=422, detail="지원하는 Bedrock 모델을 선택해주세요.")
    actor_id = request.state.user["user_id"]
    async with request.app.state.db.acquire() as conn:
        before = await conn.fetchrow("SELECT * FROM teams WHERE id = $1", team_id)
        if not before:
            raise HTTPException(status_code=404, detail="팀을 찾을 수 없습니다")
        if budget_changed(dict(before), data) and not data.get("change_reason"):
            raise HTTPException(status_code=422, detail="예산 변경 사유를 입력해주세요.")
        team, before_dict = await update_team_record(conn, team_id, data, actor_id)
    await write_audit(request, "team.update", target_type="team", target_id=team["team_key"], metadata={"before": encode(before_dict), "after": encode(team)})
    return encode(team)


@api_router.post("/teams/{team_id}/archive")
async def api_archive_team(request: Request, team_id: int):
    actor_id = request.state.user["user_id"]
    async with request.app.state.db.acquire() as conn:
        team, users = await archive_team_record(conn, team_id, actor_id)
    if users:
        raise HTTPException(status_code=409, detail="사용자가 있는 팀은 archive할 수 없습니다.")
    if not team:
        raise HTTPException(status_code=404, detail="팀을 찾을 수 없습니다")
    await write_audit(request, "team.archive", target_type="team", target_id=team["team_key"], metadata={"after": encode(team)})
    return encode(team)
