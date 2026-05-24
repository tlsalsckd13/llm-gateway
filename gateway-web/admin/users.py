from datetime import date, timedelta

import asyncpg
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from admin.queries import encode, records
from auth.csrf import CSRF_COOKIE, create_csrf_token, verify_csrf_token
from auth.magic_link import issue_magic_link
from common.audit import write_audit
from common.flash import set_flash
from common.pagination import envelope, page_from_query

page_router = APIRouter(prefix="/admin")
api_router = APIRouter(prefix="/api/admin")

ROLES = ("admin", "user")


class UserInvite(BaseModel):
    email: str
    display_name: str = Field(min_length=1, max_length=160)
    role: str = "user"
    team_id_fk: int
    department: str | None = None
    hire_date: date | None = None
    manager_user_id: int | None = None


class UserUpdate(BaseModel):
    display_name: str = Field(min_length=1, max_length=160)
    role: str = "user"
    team_id_fk: int
    department: str | None = None
    hire_date: date | None = None
    manager_user_id: int | None = None
    is_active: bool = True


def ctx(request: Request, page: str, **kwargs):
    base = {"request": request, "page": page, "user": request.state.user, "roles": ROLES}
    base.update(kwargs)
    return base


def csrf_response(request: Request, template: str, context: dict, status_code: int = 200):
    token = create_csrf_token(request.app.state.session_secret)
    context["csrf_token"] = token
    response = request.app.state.templates.TemplateResponse(template, context, status_code=status_code)
    response.set_cookie(CSRF_COOKIE, token, httponly=True, secure=True, samesite="strict", max_age=3600, path="/admin")
    return response


def external_base_url(request: Request) -> str:
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}".rstrip("/")


def invite_url(request: Request, token: str, purpose: str = "invite") -> str:
    path = "/auth/accept-invite" if purpose == "invite" else "/auth/reset-password"
    return f"{external_base_url(request)}{path}?token={token}"


def parse_optional_int(value) -> int | None:
    if value in (None, ""):
        return None
    return int(value)


def parse_optional_date(value) -> date | None:
    if not value:
        return None
    return date.fromisoformat(str(value))


def validate_user_payload(data: dict, *, creating: bool) -> tuple[dict | None, dict[str, str]]:
    errors: dict[str, str] = {}
    email = str(data.get("email", "")).strip().lower()
    display_name = str(data.get("display_name", "")).strip()
    role = str(data.get("role") or "user")
    department = str(data.get("department", "")).strip() or None
    team_raw = data.get("team_id_fk")

    if creating and ("@" not in email or "." not in email):
        errors["email"] = "올바른 이메일을 입력해주세요."
    if not display_name:
        errors["display_name"] = "표시 이름은 필수입니다."
    if role not in ROLES:
        errors["role"] = "권한은 admin 또는 user만 선택할 수 있습니다."
    try:
        team_id_fk = int(team_raw)
    except (TypeError, ValueError):
        errors["team_id_fk"] = "팀을 선택해주세요."
        team_id_fk = None
    try:
        manager_user_id = parse_optional_int(data.get("manager_user_id"))
    except ValueError:
        errors["manager_user_id"] = "매니저를 다시 선택해주세요."
        manager_user_id = None
    try:
        hire_date = parse_optional_date(data.get("hire_date"))
    except ValueError:
        errors["hire_date"] = "입사일 형식을 확인해주세요."
        hire_date = None

    payload = {
        "email": email,
        "display_name": display_name,
        "role": role,
        "team_id_fk": team_id_fk,
        "department": department,
        "hire_date": hire_date,
        "manager_user_id": manager_user_id,
        "is_active": str(data.get("is_active", "true")).lower() not in ("false", "0", "off"),
    }
    return (None, errors) if errors else (payload, {})


async def active_teams(conn):
    rows = await conn.fetch(
        """
        SELECT id, team_key, name
        FROM teams
        WHERE archived_at IS NULL AND is_active = TRUE
        ORDER BY name
        """
    )
    return records(rows)


async def manager_options(conn, exclude_user_id: int | None = None):
    if exclude_user_id:
        rows = await conn.fetch(
            """
            SELECT id, email, display_name
            FROM web_users
            WHERE archived_at IS NULL AND id <> $1
            ORDER BY display_name, email
            LIMIT 200
            """,
            exclude_user_id,
        )
    else:
        rows = await conn.fetch(
            """
            SELECT id, email, display_name
            FROM web_users
            WHERE archived_at IS NULL
            ORDER BY display_name, email
            LIMIT 200
            """
        )
    return records(rows)


async def list_users(conn, q: str | None, role: str | None, team: str | None, status: str | None, page):
    params = []
    where = []
    if status != "archived":
        where.append("u.archived_at IS NULL")
    elif status == "archived":
        where.append("u.archived_at IS NOT NULL")
    if q:
        params.append(f"%{q.lower()}%")
        where.append(f"(lower(u.email) LIKE ${len(params)} OR lower(u.display_name) LIKE ${len(params)})")
    if role:
        params.append(role)
        where.append(f"u.role = ${len(params)}")
    if team:
        params.append(team)
        where.append(f"t.team_key = ${len(params)}")
    if status == "active":
        where.append("u.is_active = TRUE AND u.locked_until IS NULL")
    elif status == "inactive":
        where.append("u.is_active = FALSE")
    elif status == "locked":
        where.append("u.locked_until IS NOT NULL AND u.locked_until > now()")
    where_sql = "WHERE " + " AND ".join(where) if where else ""
    total = await conn.fetchval(
        f"""
        SELECT count(*)
        FROM web_users u
        LEFT JOIN teams t ON t.id = u.team_id_fk
        {where_sql}
        """,
        *params,
    )
    params.extend([page.per_page, page.offset])
    rows = await conn.fetch(
        f"""
        SELECT u.id,
               u.email,
               u.display_name,
               u.role,
               u.team_id,
               u.department,
               u.is_active,
               u.locked_until,
               u.archived_at,
               u.invited_at,
               u.last_login_at,
               t.team_key,
               t.name AS team_name
        FROM web_users u
        LEFT JOIN teams t ON t.id = u.team_id_fk
        {where_sql}
        ORDER BY u.created_at DESC, u.id DESC
        LIMIT ${len(params)-1} OFFSET ${len(params)}
        """,
        *params,
    )
    items = records(rows)
    for item in items:
        if item["archived_at"]:
            item["status"] = "archived"
        elif item["locked_until"]:
            item["status"] = "locked"
        elif item["is_active"]:
            item["status"] = "active"
        else:
            item["status"] = "inactive"
    return envelope(items, int(total or 0), page)


async def get_user(conn, user_id: int):
    row = await conn.fetchrow(
        """
        SELECT u.id,
               u.email,
               u.display_name,
               u.role,
               u.team_id,
               u.team_id_fk,
               u.department,
               u.hire_date,
               u.manager_user_id,
               u.is_active,
               u.failed_login_count,
               u.locked_until,
               u.archived_at,
               u.invited_at,
               u.last_password_changed_at,
               u.created_at,
               u.last_login_at,
               t.team_key,
               t.name AS team_name,
               m.email AS manager_email,
               m.display_name AS manager_name
        FROM web_users u
        LEFT JOIN teams t ON t.id = u.team_id_fk
        LEFT JOIN web_users m ON m.id = u.manager_user_id
        WHERE u.id = $1
        """,
        user_id,
    )
    return encode(dict(row)) if row else None


async def user_keys(conn, user):
    rows = await conn.fetch(
        """
        SELECT COALESCE(key_prefix, substring(key_hash from 1 for 12)) AS key_prefix,
               label, created_at, expires_at, last_used_at, revoked_at
        FROM api_keys
        WHERE lower(user_id) = lower($1)
        ORDER BY created_at DESC
        LIMIT 100
        """,
        user["email"],
    )
    return records(rows)


async def user_usage(conn, user):
    row = await conn.fetchrow(
        """
        SELECT count(*)::int AS calls,
               COALESCE(sum(input_tokens), 0)::int AS input_tokens,
               COALESCE(sum(output_tokens), 0)::int AS output_tokens,
               COALESCE(sum(cost_usd), 0)::float8 AS cost_usd
        FROM llm_usage
        WHERE lower(user_id) = lower($1)
          AND ts >= now() - interval '30 days'
          AND blocked_reason IS NULL
        """,
        user["email"],
    )
    return encode(dict(row)) if row else {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0}


async def user_audit(conn, user):
    rows = await conn.fetch(
        """
        SELECT id, actor_user_id, actor_role, action, target_type, target_id, metadata, created_at
        FROM audit_log
        WHERE actor_user_id = $1 OR target_id IN ($2, $3)
        ORDER BY created_at DESC
        LIMIT 100
        """,
        user["id"],
        str(user["id"]),
        user["email"],
    )
    return records(rows)


async def create_invited_user(conn, payload: dict, actor_id: int):
    team = await conn.fetchrow("SELECT id, team_key FROM teams WHERE id = $1 AND archived_at IS NULL", payload["team_id_fk"])
    if not team:
        raise ValueError("team_not_found")
    row = await conn.fetchrow(
        """
        INSERT INTO web_users (
          email, display_name, role, team_id, team_id_fk,
          department, hire_date, manager_user_id, invited_at, is_active
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, now(), TRUE)
        RETURNING *
        """,
        payload["email"],
        payload["display_name"],
        payload["role"],
        team["team_key"],
        team["id"],
        payload["department"],
        payload["hire_date"],
        payload["manager_user_id"],
    )
    token = await issue_magic_link(conn, row["id"], "invite", actor_id, timedelta(days=7))
    return dict(row), token


async def update_user_record(conn, user_id: int, payload: dict):
    team = await conn.fetchrow("SELECT id, team_key FROM teams WHERE id = $1 AND archived_at IS NULL", payload["team_id_fk"])
    if not team:
        raise ValueError("team_not_found")
    before = await conn.fetchrow("SELECT * FROM web_users WHERE id = $1", user_id)
    if not before:
        return None, None
    row = await conn.fetchrow(
        """
        UPDATE web_users
        SET display_name = $2,
            role = $3,
            team_id = $4,
            team_id_fk = $5,
            department = $6,
            hire_date = $7,
            manager_user_id = $8,
            is_active = $9
        WHERE id = $1
        RETURNING *
        """,
        user_id,
        payload["display_name"],
        payload["role"],
        team["team_key"],
        team["id"],
        payload["department"],
        payload["hire_date"],
        payload["manager_user_id"],
        payload["is_active"],
    )
    return dict(row), dict(before)


async def deactivate_user(conn, user_id: int, actor_id: int, *, archive: bool = False):
    row = await conn.fetchrow(
        """
        UPDATE web_users
        SET is_active = FALSE,
            archived_at = CASE WHEN $2 THEN COALESCE(archived_at, now()) ELSE archived_at END
        WHERE id = $1
        RETURNING *
        """,
        user_id,
        archive,
    )
    if not row:
        return None
    await conn.execute("UPDATE web_sessions SET revoked_at = now() WHERE user_id = $1 AND revoked_at IS NULL", user_id)
    await conn.execute(
        """
        UPDATE api_keys
        SET revoked_at = COALESCE(revoked_at, now()),
            revoked_by_user_id = COALESCE(revoked_by_user_id, $2)
        WHERE lower(user_id) = lower($1)
        """,
        row["email"],
        actor_id,
    )
    return dict(row)


async def unlock_user(conn, user_id: int):
    row = await conn.fetchrow(
        """
        UPDATE web_users
        SET failed_login_count = 0, locked_until = NULL
        WHERE id = $1
        RETURNING *
        """,
        user_id,
    )
    return dict(row) if row else None


@page_router.get("/users")
async def users_page(request: Request):
    page = page_from_query(request.query_params)
    q = request.query_params.get("q") or None
    role = request.query_params.get("role") or None
    team = request.query_params.get("team") or None
    status = request.query_params.get("status") or None
    async with request.app.state.db.acquire() as conn:
        data = await list_users(conn, q, role, team, status, page)
        teams = await active_teams(conn)
    return request.app.state.templates.TemplateResponse(
        "admin/users.html",
        ctx(request, "users", users=data, teams=teams, q=q or "", selected_role=role or "", selected_team=team or "", selected_status=status or ""),
    )


@page_router.get("/users/new")
async def user_new_page(request: Request):
    async with request.app.state.db.acquire() as conn:
        teams = await active_teams(conn)
        managers = await manager_options(conn)
    values = {"role": "user", "team_id_fk": teams[0]["id"] if teams else ""}
    return csrf_response(request, "admin/user_form.html", ctx(request, "users", values=values, errors={}, teams=teams, managers=managers))


@page_router.post("/users")
async def user_create_post(request: Request):
    form = await request.form()
    if not verify_csrf_token(request.app.state.session_secret, form.get("csrf_token"), request.cookies.get(CSRF_COOKIE)):
        errors = {"form": "요청이 만료되었습니다. 다시 시도해주세요."}
    else:
        payload, errors = validate_user_payload(dict(form), creating=True)
    async with request.app.state.db.acquire() as conn:
        teams = await active_teams(conn)
        managers = await manager_options(conn)
        if errors:
            return csrf_response(request, "admin/user_form.html", ctx(request, "users", values=dict(form), errors=errors, teams=teams, managers=managers), status_code=400)
        try:
            user, token = await create_invited_user(conn, payload, request.state.user["user_id"])
        except asyncpg.UniqueViolationError:
            return csrf_response(request, "admin/user_form.html", ctx(request, "users", values=dict(form), errors={"email": "이미 존재하는 이메일입니다."}, teams=teams, managers=managers), status_code=409)
        except ValueError:
            return csrf_response(request, "admin/user_form.html", ctx(request, "users", values=dict(form), errors={"team_id_fk": "팀을 찾을 수 없습니다."}, teams=teams, managers=managers), status_code=400)
    await write_audit(request, "user.invite", target_type="web_user", target_id=str(user["id"]), metadata={"email": user["email"], "role": user["role"]})
    return request.app.state.templates.TemplateResponse(
        "admin/user_link.html",
        ctx(request, "users", title="사용자 초대 링크", user_record=encode(user), link_url=invite_url(request, token, "invite")),
    )


@page_router.get("/users/{user_id:int}")
async def user_detail_page(request: Request, user_id: int):
    async with request.app.state.db.acquire() as conn:
        user_record = await get_user(conn, user_id)
        if not user_record:
            raise HTTPException(status_code=404, detail="사용자를 찾을 수 없습니다")
        teams = await active_teams(conn)
        managers = await manager_options(conn, exclude_user_id=user_id)
        keys = await user_keys(conn, user_record)
        usage = await user_usage(conn, user_record)
        audit = await user_audit(conn, user_record)
    return csrf_response(
        request,
        "admin/user_detail.html",
        ctx(request, "users", user_record=user_record, teams=teams, managers=managers, keys=keys, usage=usage, audit=audit, errors={}),
    )


@page_router.post("/users/{user_id:int}")
async def user_update_post(request: Request, user_id: int):
    form = await request.form()
    if not verify_csrf_token(request.app.state.session_secret, form.get("csrf_token"), request.cookies.get(CSRF_COOKIE)):
        errors = {"form": "요청이 만료되었습니다. 다시 시도해주세요."}
        payload = None
    else:
        payload, errors = validate_user_payload(dict(form), creating=False)
    async with request.app.state.db.acquire() as conn:
        user_record = await get_user(conn, user_id)
        if not user_record:
            raise HTTPException(status_code=404, detail="사용자를 찾을 수 없습니다")
        if errors:
            teams = await active_teams(conn)
            managers = await manager_options(conn, exclude_user_id=user_id)
            keys = await user_keys(conn, user_record)
            usage = await user_usage(conn, user_record)
            audit = await user_audit(conn, user_record)
            return csrf_response(request, "admin/user_detail.html", ctx(request, "users", user_record=user_record, teams=teams, managers=managers, keys=keys, usage=usage, audit=audit, errors=errors), status_code=400)
        try:
            updated, before = await update_user_record(conn, user_id, payload)
        except ValueError:
            raise HTTPException(status_code=400, detail="팀을 찾을 수 없습니다")
    await write_audit(request, "user.update", target_type="web_user", target_id=str(user_id), metadata={"before": encode(before), "after": encode(updated)})
    response = RedirectResponse(f"/admin/users/{user_id}", status_code=302)
    response.delete_cookie(CSRF_COOKIE, path="/admin")
    return set_flash(response, request.app.state.session_secret, "사용자 정보가 수정되었습니다.")


@page_router.post("/users/{user_id:int}/deactivate")
async def user_deactivate_post(request: Request, user_id: int):
    form = await request.form()
    if not verify_csrf_token(request.app.state.session_secret, form.get("csrf_token"), request.cookies.get(CSRF_COOKIE)):
        raise HTTPException(status_code=400, detail="요청이 만료되었습니다")
    async with request.app.state.db.acquire() as conn:
        user = await deactivate_user(conn, user_id, request.state.user["user_id"])
    if not user:
        raise HTTPException(status_code=404, detail="사용자를 찾을 수 없습니다")
    await write_audit(request, "user.deactivate", target_type="web_user", target_id=str(user_id), metadata={"email": user["email"]})
    response = RedirectResponse(f"/admin/users/{user_id}", status_code=302)
    return set_flash(response, request.app.state.session_secret, "사용자가 비활성화되고 세션/API Key가 폐기되었습니다.")


@page_router.post("/users/{user_id:int}/archive")
async def user_archive_post(request: Request, user_id: int):
    form = await request.form()
    if not verify_csrf_token(request.app.state.session_secret, form.get("csrf_token"), request.cookies.get(CSRF_COOKIE)):
        raise HTTPException(status_code=400, detail="요청이 만료되었습니다")
    async with request.app.state.db.acquire() as conn:
        user = await deactivate_user(conn, user_id, request.state.user["user_id"], archive=True)
    if not user:
        raise HTTPException(status_code=404, detail="사용자를 찾을 수 없습니다")
    await write_audit(request, "user.archive", target_type="web_user", target_id=str(user_id), metadata={"email": user["email"]})
    response = RedirectResponse("/admin/users", status_code=302)
    return set_flash(response, request.app.state.session_secret, "사용자가 archive되었습니다.")


@page_router.post("/users/{user_id:int}/unlock")
async def user_unlock_post(request: Request, user_id: int):
    form = await request.form()
    if not verify_csrf_token(request.app.state.session_secret, form.get("csrf_token"), request.cookies.get(CSRF_COOKIE)):
        raise HTTPException(status_code=400, detail="요청이 만료되었습니다")
    async with request.app.state.db.acquire() as conn:
        user = await unlock_user(conn, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="사용자를 찾을 수 없습니다")
    await write_audit(request, "user.unlock", target_type="web_user", target_id=str(user_id), metadata={"email": user["email"]})
    response = RedirectResponse(f"/admin/users/{user_id}", status_code=302)
    return set_flash(response, request.app.state.session_secret, "사용자 잠금이 해제되었습니다.")


@page_router.post("/users/{user_id:int}/reset-password-link")
async def user_reset_link_post(request: Request, user_id: int):
    form = await request.form()
    if not verify_csrf_token(request.app.state.session_secret, form.get("csrf_token"), request.cookies.get(CSRF_COOKIE)):
        raise HTTPException(status_code=400, detail="요청이 만료되었습니다")
    async with request.app.state.db.acquire() as conn:
        user = await get_user(conn, user_id)
        if not user:
            raise HTTPException(status_code=404, detail="사용자를 찾을 수 없습니다")
        token = await issue_magic_link(conn, user_id, "password_reset", request.state.user["user_id"], timedelta(hours=24))
    await write_audit(request, "user.password_reset_link", target_type="web_user", target_id=str(user_id), metadata={"email": user["email"]})
    return request.app.state.templates.TemplateResponse(
        "admin/user_link.html",
        ctx(request, "users", title="비밀번호 재설정 링크", user_record=user, link_url=invite_url(request, token, "password_reset")),
    )


@api_router.get("/users")
async def api_list_users(request: Request):
    page = page_from_query(request.query_params)
    async with request.app.state.db.acquire() as conn:
        return await list_users(
            conn,
            request.query_params.get("q") or None,
            request.query_params.get("role") or None,
            request.query_params.get("team") or None,
            request.query_params.get("status") or None,
            page,
        )


@api_router.post("/users")
async def api_invite_user(request: Request, payload: UserInvite):
    data, errors = validate_user_payload(payload.model_dump(), creating=True)
    if errors:
        raise HTTPException(status_code=422, detail=errors)
    async with request.app.state.db.acquire() as conn:
        try:
            user, token = await create_invited_user(conn, data, request.state.user["user_id"])
        except asyncpg.UniqueViolationError as exc:
            raise HTTPException(status_code=409, detail="이미 존재하는 이메일입니다.") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="팀을 찾을 수 없습니다.") from exc
    await write_audit(request, "user.invite", target_type="web_user", target_id=str(user["id"]), metadata={"email": user["email"], "role": user["role"]})
    result = encode(user)
    result["invite_url"] = invite_url(request, token, "invite")
    return JSONResponse(status_code=201, content=result)


@api_router.get("/users/{user_id}")
async def api_get_user(request: Request, user_id: int):
    async with request.app.state.db.acquire() as conn:
        user = await get_user(conn, user_id)
        if not user:
            raise HTTPException(status_code=404, detail="사용자를 찾을 수 없습니다")
        user["api_keys"] = await user_keys(conn, user)
        user["usage"] = await user_usage(conn, user)
        user["audit"] = await user_audit(conn, user)
    return user


@api_router.put("/users/{user_id}")
async def api_update_user(request: Request, user_id: int, payload: UserUpdate):
    data, errors = validate_user_payload(payload.model_dump(), creating=False)
    if errors:
        raise HTTPException(status_code=422, detail=errors)
    async with request.app.state.db.acquire() as conn:
        try:
            user, before = await update_user_record(conn, user_id, data)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="팀을 찾을 수 없습니다.") from exc
    if not user:
        raise HTTPException(status_code=404, detail="사용자를 찾을 수 없습니다")
    await write_audit(request, "user.update", target_type="web_user", target_id=str(user_id), metadata={"before": encode(before), "after": encode(user)})
    return encode(user)


@api_router.post("/users/{user_id}/deactivate")
async def api_deactivate_user(request: Request, user_id: int):
    async with request.app.state.db.acquire() as conn:
        user = await deactivate_user(conn, user_id, request.state.user["user_id"])
    if not user:
        raise HTTPException(status_code=404, detail="사용자를 찾을 수 없습니다")
    await write_audit(request, "user.deactivate", target_type="web_user", target_id=str(user_id), metadata={"email": user["email"]})
    return encode(user)


@api_router.post("/users/{user_id}/archive")
async def api_archive_user(request: Request, user_id: int):
    async with request.app.state.db.acquire() as conn:
        user = await deactivate_user(conn, user_id, request.state.user["user_id"], archive=True)
    if not user:
        raise HTTPException(status_code=404, detail="사용자를 찾을 수 없습니다")
    await write_audit(request, "user.archive", target_type="web_user", target_id=str(user_id), metadata={"email": user["email"]})
    return encode(user)


@api_router.post("/users/{user_id}/unlock")
async def api_unlock_user(request: Request, user_id: int):
    async with request.app.state.db.acquire() as conn:
        user = await unlock_user(conn, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="사용자를 찾을 수 없습니다")
    await write_audit(request, "user.unlock", target_type="web_user", target_id=str(user_id), metadata={"email": user["email"]})
    return encode(user)


@api_router.post("/users/{user_id}/reset-password-link")
async def api_reset_link(request: Request, user_id: int):
    async with request.app.state.db.acquire() as conn:
        user = await get_user(conn, user_id)
        if not user:
            raise HTTPException(status_code=404, detail="사용자를 찾을 수 없습니다")
        token = await issue_magic_link(conn, user_id, "password_reset", request.state.user["user_id"], timedelta(hours=24))
    await write_audit(request, "user.password_reset_link", target_type="web_user", target_id=str(user_id), metadata={"email": user["email"]})
    return {"reset_url": invite_url(request, token, "password_reset")}
