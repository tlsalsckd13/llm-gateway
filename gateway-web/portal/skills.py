import asyncpg
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from admin.queries import encode, records
from admin.skills import create_skill, parse_bundle_upload
from auth.csrf import CSRF_COOKIE, create_csrf_token, verify_csrf_token
from common.audit import write_audit
from common.flash import set_flash


page_router = APIRouter(prefix="/portal")
api_router = APIRouter(prefix="/api/portal/me/skills")


def ctx(request: Request, page: str, **kwargs):
    base = {"request": request, "page": page, "section": "portal", "user": request.state.user}
    base.update(kwargs)
    return base


def csrf_response(request: Request, template: str, context: dict, status_code: int = 200):
    token = create_csrf_token(request.app.state.session_secret)
    context["csrf_token"] = token
    response = request.app.state.templates.TemplateResponse(template, context, status_code=status_code)
    response.set_cookie(CSRF_COOKIE, token, httponly=True, secure=True, samesite="strict", max_age=3600, path="/portal")
    return response


def can_manage_team_skills(user: dict) -> bool:
    return user.get("role") in ("admin", "team_owner") and bool(user.get("team_id_fk"))


async def list_visible_skills(conn, user: dict):
    rows = await conn.fetch(
        """
        WITH visible AS (
          SELECT s.id
          FROM skills s
          WHERE s.archived_at IS NULL
            AND s.is_active = TRUE
            AND (
              (s.owner_scope = 'org' AND s.status = 'approved')
              OR (s.owner_scope = 'team' AND s.status = 'approved' AND s.owner_team_id = $2)
              OR (s.owner_scope = 'user' AND s.owner_user_id = $1)
            )
        )
        SELECT s.id,
               s.slug,
               s.name,
               s.description,
               s.owner_scope,
               s.status,
               v.version AS latest_version,
               t.team_key AS owner_team_key,
               t.name AS owner_team_name,
               wu.email AS owner_user_email,
               wu.display_name AS owner_user_name,
               COALESCE(usa.enabled, FALSE) AS user_enabled,
               COALESCE(tsa.enabled, FALSE) AS team_enabled
        FROM visible
        JOIN skills s ON s.id = visible.id
        LEFT JOIN skill_versions v ON v.id = s.latest_version_id
        LEFT JOIN teams t ON t.id = s.owner_team_id
        LEFT JOIN web_users wu ON wu.id = s.owner_user_id
        LEFT JOIN skill_activations usa
          ON usa.skill_id = s.id AND usa.subject_scope = 'user' AND usa.subject_user_id = $1
        LEFT JOIN skill_activations tsa
          ON tsa.skill_id = s.id AND tsa.subject_scope = 'team' AND tsa.subject_team_id = $2
        ORDER BY s.owner_scope, s.slug
        """,
        user["user_id"],
        user.get("team_id_fk"),
    )
    return records(rows)


async def list_my_published_skills(conn, user: dict):
    rows = await conn.fetch(
        """
        SELECT s.id,
               s.slug,
               s.owner_scope,
               s.status,
               s.created_at,
               v.version AS latest_version,
               t.team_key AS owner_team_key
        FROM skills s
        LEFT JOIN skill_versions v ON v.id = s.latest_version_id
        LEFT JOIN teams t ON t.id = s.owner_team_id
        WHERE s.archived_at IS NULL
          AND (
            s.owner_user_id = $1
            OR (s.owner_scope = 'team' AND s.owner_team_id = $2 AND $3 = TRUE)
          )
        ORDER BY s.created_at DESC
        LIMIT 50
        """,
        user["user_id"],
        user.get("team_id_fk"),
        can_manage_team_skills(user),
    )
    return records(rows)


async def assert_skill_visible(conn, skill_id: int, user: dict) -> dict | None:
    row = await conn.fetchrow(
        """
        SELECT s.*
        FROM skills s
        WHERE s.id = $1
          AND s.archived_at IS NULL
          AND s.is_active = TRUE
          AND (
            (s.owner_scope = 'org' AND s.status = 'approved')
            OR (s.owner_scope = 'team' AND s.status = 'approved' AND s.owner_team_id = $3)
            OR (s.owner_scope = 'user' AND s.owner_user_id = $2)
          )
        """,
        skill_id,
        user["user_id"],
        user.get("team_id_fk"),
    )
    return dict(row) if row else None


async def set_activation(conn, *, skill_id: int, user: dict, subject_scope: str, enabled: bool) -> dict:
    if subject_scope == "team":
        if not can_manage_team_skills(user):
            raise PermissionError("팀 Skill 활성화 권한이 없습니다.")
        subject_team_id = user.get("team_id_fk")
        subject_user_id = None
    elif subject_scope == "user":
        subject_team_id = None
        subject_user_id = user["user_id"]
    else:
        raise ValueError("활성화 스코프를 확인해주세요.")

    skill = await assert_skill_visible(conn, skill_id, user)
    if not skill:
        raise LookupError("활성화 가능한 Skill을 찾을 수 없습니다.")

    row = await conn.fetchrow(
        """
        INSERT INTO skill_activations (
          skill_id, subject_scope, subject_team_id, subject_user_id, enabled, activated_at
        )
        VALUES ($1, $2, $3, $4, $5, now())
        ON CONFLICT (skill_id, subject_scope, (COALESCE(subject_team_id, 0)), (COALESCE(subject_user_id, 0)))
        DO UPDATE SET enabled = EXCLUDED.enabled, activated_at = now()
        RETURNING *
        """,
        skill_id,
        subject_scope,
        subject_team_id,
        subject_user_id,
        enabled,
    )
    return encode(dict(row))


async def create_portal_skill(conn, *, request: Request, owner_scope: str, form) -> dict:
    raw_bundle, bundle, bundle_error = await parse_bundle_upload(form.get("bundle"))
    if bundle_error:
        raise ValueError(bundle_error)
    if owner_scope == "user":
        owner_team_id = None
        owner_user_id = request.state.user["user_id"]
        status = "approved"
    elif owner_scope == "team":
        if not can_manage_team_skills(request.state.user):
            raise PermissionError("팀 Skill 발행 권한이 없습니다.")
        owner_team_id = request.state.user["team_id_fk"]
        owner_user_id = None
        status = "pending"
    else:
        raise ValueError("스코프를 확인해주세요.")
    return await create_skill(
        conn,
        actor_id=request.state.user["user_id"],
        owner_scope=owner_scope,
        owner_team_id=owner_team_id,
        owner_user_id=owner_user_id,
        raw_bundle=raw_bundle,
        bundle=bundle,
        status=status,
    )


@page_router.get("/skills")
async def portal_skills_page(request: Request):
    async with request.app.state.db.acquire() as conn:
        skills = await list_visible_skills(conn, request.state.user)
        published = await list_my_published_skills(conn, request.state.user)
    return csrf_response(
        request,
        "portal/skills.html",
        ctx(request, "portal_skills", skills=skills, published=published, can_manage_team=can_manage_team_skills(request.state.user)),
    )


@page_router.get("/skills/new")
async def portal_skill_new_page(request: Request):
    return csrf_response(
        request,
        "portal/skill_form.html",
        ctx(request, "portal_skills", scope="user", values={}, errors={}),
    )


@page_router.post("/skills")
async def portal_skill_create_post(request: Request):
    form = await request.form()
    csrf_ok = verify_csrf_token(request.app.state.session_secret, form.get("csrf_token"), request.cookies.get(CSRF_COOKIE))
    if not csrf_ok:
        return csrf_response(request, "portal/skill_form.html", ctx(request, "portal_skills", scope="user", values=dict(form), errors={"form": "요청이 만료되었습니다. 다시 시도해주세요."}), status_code=400)
    async with request.app.state.db.acquire() as conn:
        try:
            skill = await create_portal_skill(conn, request=request, owner_scope="user", form=form)
            await set_activation(conn, skill_id=skill["id"], user=request.state.user, subject_scope="user", enabled=True)
        except ValueError as exc:
            return csrf_response(request, "portal/skill_form.html", ctx(request, "portal_skills", scope="user", values=dict(form), errors={"bundle": str(exc)}), status_code=400)
        except asyncpg.UniqueViolationError:
            return csrf_response(request, "portal/skill_form.html", ctx(request, "portal_skills", scope="user", values=dict(form), errors={"bundle": "동일한 Skill name 또는 version이 이미 있습니다."}), status_code=409)
    await write_audit(request, "skill.create", target_type="skill", target_id=str(skill["id"]), metadata={"skill": skill, "origin": "portal-user"})
    await write_audit(request, "skill.activate", target_type="skill", target_id=str(skill["id"]), metadata={"subject_scope": "user", "auto": True})
    response = RedirectResponse("/portal/skills", status_code=302)
    response.delete_cookie(CSRF_COOKIE, path="/portal")
    return set_flash(response, request.app.state.session_secret, "내 Skill이 발행되고 활성화되었습니다.")


@page_router.get("/skills/team/new")
async def portal_team_skill_new_page(request: Request):
    if not can_manage_team_skills(request.state.user):
        raise HTTPException(status_code=403, detail="팀 Skill 발행 권한이 없습니다")
    return csrf_response(
        request,
        "portal/skill_form.html",
        ctx(request, "portal_skills", scope="team", values={}, errors={}),
    )


@page_router.post("/skills/team")
async def portal_team_skill_create_post(request: Request):
    form = await request.form()
    csrf_ok = verify_csrf_token(request.app.state.session_secret, form.get("csrf_token"), request.cookies.get(CSRF_COOKIE))
    if not can_manage_team_skills(request.state.user):
        raise HTTPException(status_code=403, detail="팀 Skill 발행 권한이 없습니다")
    if not csrf_ok:
        return csrf_response(request, "portal/skill_form.html", ctx(request, "portal_skills", scope="team", values=dict(form), errors={"form": "요청이 만료되었습니다. 다시 시도해주세요."}), status_code=400)
    async with request.app.state.db.acquire() as conn:
        try:
            skill = await create_portal_skill(conn, request=request, owner_scope="team", form=form)
        except ValueError as exc:
            return csrf_response(request, "portal/skill_form.html", ctx(request, "portal_skills", scope="team", values=dict(form), errors={"bundle": str(exc)}), status_code=400)
        except asyncpg.UniqueViolationError:
            return csrf_response(request, "portal/skill_form.html", ctx(request, "portal_skills", scope="team", values=dict(form), errors={"bundle": "동일한 Skill name 또는 version이 이미 있습니다."}), status_code=409)
    await write_audit(request, "skill.create", target_type="skill", target_id=str(skill["id"]), metadata={"skill": skill, "origin": "portal-team"})
    response = RedirectResponse("/portal/skills", status_code=302)
    response.delete_cookie(CSRF_COOKIE, path="/portal")
    return set_flash(response, request.app.state.session_secret, "팀 Skill이 승인 대기 상태로 등록되었습니다.")


@page_router.post("/skills/{skill_id:int}/activate")
async def portal_skill_activate_post(request: Request, skill_id: int):
    form = await request.form()
    if not verify_csrf_token(request.app.state.session_secret, form.get("csrf_token"), request.cookies.get(CSRF_COOKIE)):
        raise HTTPException(status_code=400, detail="요청이 만료되었습니다")
    subject_scope = str(form.get("subject_scope") or "user")
    async with request.app.state.db.acquire() as conn:
        try:
            activation = await set_activation(conn, skill_id=skill_id, user=request.state.user, subject_scope=subject_scope, enabled=True)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except (ValueError, LookupError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    await write_audit(request, "skill.activate", target_type="skill", target_id=str(skill_id), metadata={"activation": activation})
    response = RedirectResponse("/portal/skills", status_code=302)
    return set_flash(response, request.app.state.session_secret, "Skill이 활성화되었습니다.")


@page_router.post("/skills/{skill_id:int}/deactivate")
async def portal_skill_deactivate_post(request: Request, skill_id: int):
    form = await request.form()
    if not verify_csrf_token(request.app.state.session_secret, form.get("csrf_token"), request.cookies.get(CSRF_COOKIE)):
        raise HTTPException(status_code=400, detail="요청이 만료되었습니다")
    subject_scope = str(form.get("subject_scope") or "user")
    async with request.app.state.db.acquire() as conn:
        try:
            activation = await set_activation(conn, skill_id=skill_id, user=request.state.user, subject_scope=subject_scope, enabled=False)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except (ValueError, LookupError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    await write_audit(request, "skill.deactivate", target_type="skill", target_id=str(skill_id), metadata={"activation": activation})
    response = RedirectResponse("/portal/skills", status_code=302)
    return set_flash(response, request.app.state.session_secret, "Skill이 비활성화되었습니다.")


@api_router.get("")
async def api_my_skills(request: Request):
    async with request.app.state.db.acquire() as conn:
        return {"items": await list_visible_skills(conn, request.state.user)}


@api_router.post("/{skill_id}/activate")
async def api_activate_skill(request: Request, skill_id: int):
    body = await request.json()
    subject_scope = str(body.get("subject_scope") or "user")
    async with request.app.state.db.acquire() as conn:
        try:
            activation = await set_activation(conn, skill_id=skill_id, user=request.state.user, subject_scope=subject_scope, enabled=True)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except (ValueError, LookupError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    await write_audit(request, "skill.activate", target_type="skill", target_id=str(skill_id), metadata={"activation": activation})
    return JSONResponse(encode(activation))


@api_router.post("/{skill_id}/deactivate")
async def api_deactivate_skill(request: Request, skill_id: int):
    body = await request.json()
    subject_scope = str(body.get("subject_scope") or "user")
    async with request.app.state.db.acquire() as conn:
        try:
            activation = await set_activation(conn, skill_id=skill_id, user=request.state.user, subject_scope=subject_scope, enabled=False)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except (ValueError, LookupError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    await write_audit(request, "skill.deactivate", target_type="skill", target_id=str(skill_id), metadata={"activation": activation})
    return JSONResponse(encode(activation))


__all__ = ["page_router", "api_router", "list_visible_skills", "set_activation"]
