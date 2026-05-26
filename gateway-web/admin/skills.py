import json
import logging
import os
from urllib.parse import urlencode

import asyncpg
import boto3
from fastapi import APIRouter, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from admin.queries import encode, records
from auth.csrf import CSRF_COOKIE, create_csrf_token, verify_csrf_token
from common.audit import write_audit
from common.flash import set_flash
from common.pagination import envelope, page_from_query
from common.skill_bundle import SkillBundle, SkillBundleError, inspect_skill_bundle


page_router = APIRouter(prefix="/admin")
api_router = APIRouter(prefix="/api/admin")
log = logging.getLogger(__name__)

SKILLS_BUCKET = os.environ.get("SKILLS_BUCKET", "kcs-llm-gateway-skills-prod")
OWNER_SCOPES = ("org", "team", "user")
SKILL_STATUSES = ("pending", "approved", "rejected")


class SkillDecision(BaseModel):
    reason: str | None = None


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


def s3_key_for(skill_id: int, owner_scope: str, version: str, *, team_key: str | None = None, owner_user_id: int | None = None) -> str:
    version_part = f"v{version}.zip"
    if owner_scope == "org":
        return f"org/{skill_id}/{version_part}"
    if owner_scope == "team":
        if not team_key:
            raise ValueError("team_key is required for team-scope skills")
        return f"team/{team_key}/{skill_id}/{version_part}"
    if owner_scope == "user":
        if not owner_user_id:
            raise ValueError("owner_user_id is required for user-scope skills")
        return f"user/{owner_user_id}/{skill_id}/{version_part}"
    raise ValueError(f"unsupported owner_scope: {owner_scope}")


def _put_skill_bundle(bucket: str, key: str, body: bytes, *, owner_scope: str, skill_id: int, version: str) -> None:
    boto3.client("s3").put_object(
        Bucket=bucket,
        Key=key,
        Body=body,
        ContentType="application/zip",
        ServerSideEncryption="AES256",
        Tagging=urlencode({"owner_scope": owner_scope, "skill_id": str(skill_id), "version": version}),
    )


async def upload_skill_bundle_to_s3(key: str, body: bytes, *, owner_scope: str, skill_id: int, version: str) -> None:
    await run_in_threadpool(
        _put_skill_bundle,
        SKILLS_BUCKET,
        key,
        body,
        owner_scope=owner_scope,
        skill_id=skill_id,
        version=version,
    )


async def selectable_teams(conn):
    rows = await conn.fetch(
        """
        SELECT id, team_key, name
        FROM teams
        WHERE archived_at IS NULL AND is_active = TRUE
        ORDER BY name, team_key
        """
    )
    return records(rows)


async def selectable_users(conn):
    rows = await conn.fetch(
        """
        SELECT id, email, display_name
        FROM web_users
        WHERE archived_at IS NULL AND is_active = TRUE
        ORDER BY display_name, email
        LIMIT 500
        """
    )
    return records(rows)


async def owner_context(conn, owner_scope: str, owner_team_id, owner_user_id):
    if owner_scope == "org":
        return {"owner_team_id": None, "owner_user_id": None, "team_key": None, "owner_label": "org"}
    if owner_scope == "team":
        try:
            team_id = int(owner_team_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("팀을 선택해주세요.") from exc
        team = await conn.fetchrow(
            "SELECT id, team_key, name FROM teams WHERE id = $1 AND archived_at IS NULL",
            team_id,
        )
        if not team:
            raise ValueError("활성 팀을 찾을 수 없습니다.")
        return {"owner_team_id": team["id"], "owner_user_id": None, "team_key": team["team_key"], "owner_label": team["team_key"]}
    if owner_scope == "user":
        try:
            user_id = int(owner_user_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("사용자를 선택해주세요.") from exc
        user = await conn.fetchrow(
            "SELECT id, email, display_name FROM web_users WHERE id = $1 AND archived_at IS NULL AND is_active = TRUE",
            user_id,
        )
        if not user:
            raise ValueError("활성 사용자를 찾을 수 없습니다.")
        return {"owner_team_id": None, "owner_user_id": user["id"], "team_key": None, "owner_label": user["email"]}
    raise ValueError("스코프를 확인해주세요.")


async def parse_bundle_upload(file: UploadFile | None) -> tuple[bytes | None, SkillBundle | None, str | None]:
    if not file or not getattr(file, "filename", None):
        return None, None, "zip 번들을 선택해주세요."
    raw = await file.read()
    try:
        bundle = inspect_skill_bundle(raw)
    except SkillBundleError as exc:
        return None, None, str(exc)
    return raw, bundle, None


def skill_status_after_admin_upload(owner_scope: str) -> str:
    _ = owner_scope
    return "pending"


async def create_skill(
    conn,
    *,
    actor_id: int,
    owner_scope: str,
    owner_team_id,
    owner_user_id,
    raw_bundle: bytes,
    bundle: SkillBundle,
    status: str,
) -> dict:
    owner = await owner_context(conn, owner_scope, owner_team_id, owner_user_id)
    async with conn.transaction():
        skill = await conn.fetchrow(
            """
            INSERT INTO skills (
              slug, name, description, owner_scope, owner_team_id, owner_user_id,
              status, created_by_user_id
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            RETURNING *
            """,
            bundle.slug,
            bundle.slug,
            bundle.frontmatter["description"],
            owner_scope,
            owner["owner_team_id"],
            owner["owner_user_id"],
            status,
            actor_id,
        )
        s3_key = s3_key_for(
            skill["id"],
            owner_scope,
            bundle.version,
            team_key=owner["team_key"],
            owner_user_id=owner["owner_user_id"],
        )
        await upload_skill_bundle_to_s3(
            s3_key,
            raw_bundle,
            owner_scope=owner_scope,
            skill_id=skill["id"],
            version=bundle.version,
        )
        version = await conn.fetchrow(
            """
            INSERT INTO skill_versions (
              skill_id, version, s3_key, sha256, frontmatter, body_excerpt, uploaded_by_user_id
            )
            VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7)
            RETURNING *
            """,
            skill["id"],
            bundle.version,
            s3_key,
            bundle.sha256,
            json.dumps(bundle.frontmatter, ensure_ascii=False),
            bundle.body_excerpt,
            actor_id,
        )
        skill = await conn.fetchrow(
            """
            UPDATE skills
            SET latest_version_id = $2, updated_at = now()
            WHERE id = $1
            RETURNING *
            """,
            skill["id"],
            version["id"],
        )
    result = encode(dict(skill))
    result["latest_version"] = encode(dict(version))
    result["owner_label"] = owner["owner_label"]
    return result


async def add_skill_version(conn, *, skill_id: int, actor_id: int, raw_bundle: bytes, bundle: SkillBundle) -> dict | None:
    async with conn.transaction():
        skill = await conn.fetchrow(
            """
            SELECT s.*, t.team_key
            FROM skills s
            LEFT JOIN teams t ON t.id = s.owner_team_id
            WHERE s.id = $1 AND s.archived_at IS NULL
            FOR UPDATE
            """,
            skill_id,
        )
        if not skill:
            return None
        if skill["slug"] != bundle.slug:
            raise ValueError("새 버전의 name은 기존 skill slug와 같아야 합니다.")
        s3_key = s3_key_for(
            skill["id"],
            skill["owner_scope"],
            bundle.version,
            team_key=skill["team_key"],
            owner_user_id=skill["owner_user_id"],
        )
        await upload_skill_bundle_to_s3(
            s3_key,
            raw_bundle,
            owner_scope=skill["owner_scope"],
            skill_id=skill["id"],
            version=bundle.version,
        )
        version = await conn.fetchrow(
            """
            INSERT INTO skill_versions (
              skill_id, version, s3_key, sha256, frontmatter, body_excerpt, uploaded_by_user_id
            )
            VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7)
            RETURNING *
            """,
            skill["id"],
            bundle.version,
            s3_key,
            bundle.sha256,
            json.dumps(bundle.frontmatter, ensure_ascii=False),
            bundle.body_excerpt,
            actor_id,
        )
        await conn.execute(
            "UPDATE skills SET latest_version_id = $2, description = $3, updated_at = now() WHERE id = $1",
            skill["id"],
            version["id"],
            bundle.frontmatter["description"],
        )
    return encode(dict(version))


async def list_skills(conn, *, q: str | None, status: str | None, owner_scope: str | None, page):
    params = []
    where = ["s.archived_at IS NULL"]
    if q:
        params.append(f"%{q.lower()}%")
        where.append(f"(lower(s.slug) LIKE ${len(params)} OR lower(s.description) LIKE ${len(params)})")
    if status:
        params.append(status)
        where.append(f"s.status = ${len(params)}")
    if owner_scope:
        params.append(owner_scope)
        where.append(f"s.owner_scope = ${len(params)}")
    where_sql = " AND ".join(where)
    total = await conn.fetchval(f"SELECT count(*) FROM skills s WHERE {where_sql}", *params)
    params.extend([page.per_page, page.offset])
    rows = await conn.fetch(
        f"""
        SELECT s.id,
               s.slug,
               s.name,
               s.description,
               s.owner_scope,
               s.owner_team_id,
               s.owner_user_id,
               s.is_active,
               s.status,
               s.created_at,
               s.updated_at,
               v.version AS latest_version,
               v.s3_key AS latest_s3_key,
               t.team_key AS owner_team_key,
               t.name AS owner_team_name,
               wu.email AS owner_user_email,
               wu.display_name AS owner_user_name,
               count(sa.id) FILTER (WHERE sa.enabled = TRUE)::int AS activation_count
        FROM skills s
        LEFT JOIN skill_versions v ON v.id = s.latest_version_id
        LEFT JOIN teams t ON t.id = s.owner_team_id
        LEFT JOIN web_users wu ON wu.id = s.owner_user_id
        LEFT JOIN skill_activations sa ON sa.skill_id = s.id
        WHERE {where_sql}
        GROUP BY s.id, v.id, t.id, wu.id
        ORDER BY s.created_at DESC, s.id DESC
        LIMIT ${len(params)-1} OFFSET ${len(params)}
        """,
        *params,
    )
    return envelope(records(rows), int(total or 0), page)


async def get_skill_detail(conn, skill_id: int):
    skill = await conn.fetchrow(
        """
        SELECT s.*,
               v.version AS latest_version,
               v.s3_key AS latest_s3_key,
               v.frontmatter AS latest_frontmatter,
               v.body_excerpt AS latest_body_excerpt,
               t.team_key AS owner_team_key,
               t.name AS owner_team_name,
               wu.email AS owner_user_email,
               wu.display_name AS owner_user_name
        FROM skills s
        LEFT JOIN skill_versions v ON v.id = s.latest_version_id
        LEFT JOIN teams t ON t.id = s.owner_team_id
        LEFT JOIN web_users wu ON wu.id = s.owner_user_id
        WHERE s.id = $1
        """,
        skill_id,
    )
    if not skill:
        return None
    versions = await conn.fetch(
        """
        SELECT id, version, s3_key, sha256, frontmatter, body_excerpt, uploaded_at, uploaded_by_user_id
        FROM skill_versions
        WHERE skill_id = $1
        ORDER BY uploaded_at DESC, id DESC
        """,
        skill_id,
    )
    activations = await conn.fetch(
        """
        SELECT sa.subject_scope,
               sa.subject_team_id,
               sa.subject_user_id,
               sa.enabled,
               sa.activated_at,
               t.team_key,
               t.name AS team_name,
               wu.email AS user_email,
               wu.display_name AS user_name
        FROM skill_activations sa
        LEFT JOIN teams t ON t.id = sa.subject_team_id
        LEFT JOIN web_users wu ON wu.id = sa.subject_user_id
        WHERE sa.skill_id = $1
        ORDER BY sa.activated_at DESC
        LIMIT 100
        """,
        skill_id,
    )
    return {
        "skill": encode(dict(skill)),
        "versions": records(versions),
        "activations": records(activations),
    }


async def set_skill_status(conn, skill_id: int, status: str) -> dict | None:
    row = await conn.fetchrow(
        """
        UPDATE skills
        SET status = $2, is_active = CASE WHEN $2 = 'approved' THEN TRUE ELSE is_active END, updated_at = now()
        WHERE id = $1 AND archived_at IS NULL
        RETURNING *
        """,
        skill_id,
        status,
    )
    return encode(dict(row)) if row else None


async def archive_skill(conn, skill_id: int) -> dict | None:
    row = await conn.fetchrow(
        """
        UPDATE skills
        SET is_active = FALSE, archived_at = COALESCE(archived_at, now()), updated_at = now()
        WHERE id = $1
        RETURNING *
        """,
        skill_id,
    )
    if row:
        await conn.execute("UPDATE skill_activations SET enabled = FALSE WHERE skill_id = $1", skill_id)
    return encode(dict(row)) if row else None


@page_router.get("/skills")
async def skills_page(request: Request):
    page = page_from_query(request.query_params)
    q = request.query_params.get("q") or None
    status = request.query_params.get("status") or None
    owner_scope = request.query_params.get("owner_scope") or None
    async with request.app.state.db.acquire() as conn:
        data = await list_skills(conn, q=q, status=status, owner_scope=owner_scope, page=page)
    return request.app.state.templates.TemplateResponse(
        "admin/skills.html",
        ctx(
            request,
            "skills",
            skills=data,
            q=q or "",
            selected_status=status or "",
            selected_scope=owner_scope or "",
            statuses=SKILL_STATUSES,
            owner_scopes=OWNER_SCOPES,
        ),
    )


@page_router.get("/skills/new")
async def skill_new_page(request: Request):
    async with request.app.state.db.acquire() as conn:
        teams = await selectable_teams(conn)
        users = await selectable_users(conn)
    return csrf_response(
        request,
        "admin/skill_form.html",
        ctx(request, "skills", values={"owner_scope": "org"}, errors={}, teams=teams, users=users, mode="new"),
    )


@page_router.post("/skills")
async def skill_create_post(request: Request):
    form = await request.form()
    csrf_ok = verify_csrf_token(request.app.state.session_secret, form.get("csrf_token"), request.cookies.get(CSRF_COOKIE))
    owner_scope = str(form.get("owner_scope") or "org")
    raw_bundle, bundle, bundle_error = await parse_bundle_upload(form.get("bundle"))
    errors: dict[str, str] = {}
    if not csrf_ok:
        errors["form"] = "요청이 만료되었습니다. 다시 시도해주세요."
    if owner_scope not in OWNER_SCOPES:
        errors["owner_scope"] = "스코프를 확인해주세요."
    if bundle_error:
        errors["bundle"] = bundle_error
    async with request.app.state.db.acquire() as conn:
        teams = await selectable_teams(conn)
        users = await selectable_users(conn)
        if errors:
            return csrf_response(request, "admin/skill_form.html", ctx(request, "skills", values=dict(form), errors=errors, teams=teams, users=users, mode="new"), status_code=400)
        try:
            skill = await create_skill(
                conn,
                actor_id=request.state.user["user_id"],
                owner_scope=owner_scope,
                owner_team_id=form.get("owner_team_id"),
                owner_user_id=form.get("owner_user_id"),
                raw_bundle=raw_bundle,
                bundle=bundle,
                status=skill_status_after_admin_upload(owner_scope),
            )
        except ValueError as exc:
            return csrf_response(request, "admin/skill_form.html", ctx(request, "skills", values=dict(form), errors={"owner_scope": str(exc)}, teams=teams, users=users, mode="new"), status_code=400)
        except asyncpg.UniqueViolationError:
            return csrf_response(request, "admin/skill_form.html", ctx(request, "skills", values=dict(form), errors={"bundle": "같은 스코프에 동일한 skill name 또는 version이 이미 있습니다."}, teams=teams, users=users, mode="new"), status_code=409)
    await write_audit(request, "skill.create", target_type="skill", target_id=str(skill["id"]), metadata={"skill": skill})
    await write_audit(request, "skill.version.upload", target_type="skill", target_id=str(skill["id"]), metadata={"version": skill["latest_version"]})
    response = RedirectResponse(f"/admin/skills/{skill['id']}", status_code=302)
    response.delete_cookie(CSRF_COOKIE, path="/admin")
    return set_flash(response, request.app.state.session_secret, "Skill 번들이 업로드되었습니다. 승인 후 사용할 수 있습니다.")


@page_router.get("/skills/{skill_id:int}")
async def skill_detail_page(request: Request, skill_id: int):
    async with request.app.state.db.acquire() as conn:
        detail = await get_skill_detail(conn, skill_id)
    if not detail:
        raise HTTPException(status_code=404, detail="Skill을 찾을 수 없습니다")
    return csrf_response(request, "admin/skill_detail.html", ctx(request, "skills", **detail, errors={}))


@page_router.post("/skills/{skill_id:int}/approve")
async def skill_approve_post(request: Request, skill_id: int):
    form = await request.form()
    if not verify_csrf_token(request.app.state.session_secret, form.get("csrf_token"), request.cookies.get(CSRF_COOKIE)):
        raise HTTPException(status_code=400, detail="요청이 만료되었습니다")
    async with request.app.state.db.acquire() as conn:
        skill = await set_skill_status(conn, skill_id, "approved")
    if not skill:
        raise HTTPException(status_code=404, detail="Skill을 찾을 수 없습니다")
    await write_audit(request, "skill.approve", target_type="skill", target_id=str(skill_id), metadata={"reason": str(form.get("reason") or "").strip() or None})
    response = RedirectResponse(f"/admin/skills/{skill_id}", status_code=302)
    return set_flash(response, request.app.state.session_secret, "Skill이 승인되었습니다.")


@page_router.post("/skills/{skill_id:int}/reject")
async def skill_reject_post(request: Request, skill_id: int):
    form = await request.form()
    if not verify_csrf_token(request.app.state.session_secret, form.get("csrf_token"), request.cookies.get(CSRF_COOKIE)):
        raise HTTPException(status_code=400, detail="요청이 만료되었습니다")
    async with request.app.state.db.acquire() as conn:
        skill = await set_skill_status(conn, skill_id, "rejected")
    if not skill:
        raise HTTPException(status_code=404, detail="Skill을 찾을 수 없습니다")
    await write_audit(request, "skill.reject", target_type="skill", target_id=str(skill_id), metadata={"reason": str(form.get("reason") or "").strip() or None})
    response = RedirectResponse(f"/admin/skills/{skill_id}", status_code=302)
    return set_flash(response, request.app.state.session_secret, "Skill이 반려되었습니다.")


@page_router.post("/skills/{skill_id:int}/archive")
async def skill_archive_post(request: Request, skill_id: int):
    form = await request.form()
    if not verify_csrf_token(request.app.state.session_secret, form.get("csrf_token"), request.cookies.get(CSRF_COOKIE)):
        raise HTTPException(status_code=400, detail="요청이 만료되었습니다")
    async with request.app.state.db.acquire() as conn:
        skill = await archive_skill(conn, skill_id)
    if not skill:
        raise HTTPException(status_code=404, detail="Skill을 찾을 수 없습니다")
    await write_audit(request, "skill.archive", target_type="skill", target_id=str(skill_id), metadata={"skill": skill})
    response = RedirectResponse("/admin/skills", status_code=302)
    return set_flash(response, request.app.state.session_secret, "Skill이 archive되었습니다.")


@page_router.post("/skills/{skill_id:int}/versions")
async def skill_version_upload_post(request: Request, skill_id: int):
    form = await request.form()
    if not verify_csrf_token(request.app.state.session_secret, form.get("csrf_token"), request.cookies.get(CSRF_COOKIE)):
        raise HTTPException(status_code=400, detail="요청이 만료되었습니다")
    raw_bundle, bundle, bundle_error = await parse_bundle_upload(form.get("bundle"))
    if bundle_error:
        async with request.app.state.db.acquire() as conn:
            detail = await get_skill_detail(conn, skill_id)
        if not detail:
            raise HTTPException(status_code=404, detail="Skill을 찾을 수 없습니다")
        return csrf_response(request, "admin/skill_detail.html", ctx(request, "skills", **detail, errors={"version": bundle_error}), status_code=400)
    async with request.app.state.db.acquire() as conn:
        try:
            version = await add_skill_version(conn, skill_id=skill_id, actor_id=request.state.user["user_id"], raw_bundle=raw_bundle, bundle=bundle)
        except ValueError as exc:
            detail = await get_skill_detail(conn, skill_id)
            return csrf_response(request, "admin/skill_detail.html", ctx(request, "skills", **detail, errors={"version": str(exc)}), status_code=400)
        except asyncpg.UniqueViolationError:
            detail = await get_skill_detail(conn, skill_id)
            return csrf_response(request, "admin/skill_detail.html", ctx(request, "skills", **detail, errors={"version": "이미 등록된 version입니다."}), status_code=409)
    if not version:
        raise HTTPException(status_code=404, detail="Skill을 찾을 수 없습니다")
    await write_audit(request, "skill.version.upload", target_type="skill", target_id=str(skill_id), metadata={"version": version})
    response = RedirectResponse(f"/admin/skills/{skill_id}", status_code=302)
    return set_flash(response, request.app.state.session_secret, "새 Skill version이 업로드되었습니다.")


@api_router.get("/skills")
async def api_list_skills(request: Request):
    page = page_from_query(request.query_params)
    async with request.app.state.db.acquire() as conn:
        return await list_skills(
            conn,
            q=request.query_params.get("q") or None,
            status=request.query_params.get("status") or None,
            owner_scope=request.query_params.get("owner_scope") or None,
            page=page,
        )


@api_router.get("/skills/{skill_id}")
async def api_get_skill(request: Request, skill_id: int):
    async with request.app.state.db.acquire() as conn:
        detail = await get_skill_detail(conn, skill_id)
    if not detail:
        raise HTTPException(status_code=404, detail="Skill을 찾을 수 없습니다")
    return detail


@api_router.post("/skills/{skill_id}/approve")
async def api_approve_skill(request: Request, skill_id: int, payload: SkillDecision | None = None):
    async with request.app.state.db.acquire() as conn:
        skill = await set_skill_status(conn, skill_id, "approved")
    if not skill:
        raise HTTPException(status_code=404, detail="Skill을 찾을 수 없습니다")
    await write_audit(request, "skill.approve", target_type="skill", target_id=str(skill_id), metadata={"reason": payload.reason if payload else None})
    return JSONResponse(encode(skill))


@api_router.post("/skills/{skill_id}/reject")
async def api_reject_skill(request: Request, skill_id: int, payload: SkillDecision | None = None):
    async with request.app.state.db.acquire() as conn:
        skill = await set_skill_status(conn, skill_id, "rejected")
    if not skill:
        raise HTTPException(status_code=404, detail="Skill을 찾을 수 없습니다")
    await write_audit(request, "skill.reject", target_type="skill", target_id=str(skill_id), metadata={"reason": payload.reason if payload else None})
    return JSONResponse(encode(skill))


@api_router.post("/skills/{skill_id}/archive")
async def api_archive_skill(request: Request, skill_id: int):
    async with request.app.state.db.acquire() as conn:
        skill = await archive_skill(conn, skill_id)
    if not skill:
        raise HTTPException(status_code=404, detail="Skill을 찾을 수 없습니다")
    await write_audit(request, "skill.archive", target_type="skill", target_id=str(skill_id), metadata={"skill": skill})
    return JSONResponse(encode(skill))


__all__ = ["page_router", "api_router", "s3_key_for"]
