import asyncpg
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from admin.kb import (
    active_blocking_dlp_policies,
    create_document_row,
    create_kb_for_team,
    get_kb,
    ingest_upload,
    kb_documents,
    MAX_UPLOAD_FILES,
    prepare_upload,
)
from admin.queries import encode
from auth.csrf import CSRF_COOKIE, create_csrf_token, verify_csrf_token
from common.audit import write_audit
from common.flash import set_flash


page_router = APIRouter(prefix="/portal")
api_router = APIRouter(prefix="/api/portal/me/kb")


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


def can_manage_team_kb(user: dict) -> bool:
    return user.get("role") in ("admin", "team_owner") and bool(user.get("team_id_fk"))


async def my_kb(conn, user: dict):
    row = await conn.fetchrow(
        """
        SELECT kb.*,
               t.team_key,
               t.name AS team_name,
               COALESCE(up.default_kb_enabled, TRUE) AS user_kb_enabled,
               count(DISTINCT d.id) FILTER (WHERE d.removed_at IS NULL)::int AS document_count,
               COALESCE(sum(d.chunk_count) FILTER (WHERE d.removed_at IS NULL), 0)::int AS chunk_count
        FROM web_users wu
        LEFT JOIN teams t ON t.id = wu.team_id_fk
        LEFT JOIN knowledge_bases kb ON kb.team_id = t.id
        LEFT JOIN user_preferences up ON up.user_id = wu.id
        LEFT JOIN kb_documents d ON d.kb_id = kb.id
        WHERE wu.id = $1
        GROUP BY kb.id, t.id, up.user_id
        """,
        user["user_id"],
    )
    return encode(dict(row)) if row and row.get("id") else None


async def set_user_kb_enabled(conn, user_id: int, enabled: bool):
    row = await conn.fetchrow(
        """
        INSERT INTO user_preferences (user_id, default_kb_enabled, updated_at)
        VALUES ($1, $2, now())
        ON CONFLICT (user_id) DO UPDATE
        SET default_kb_enabled = EXCLUDED.default_kb_enabled,
            updated_at = now()
        RETURNING *
        """,
        user_id,
        enabled,
    )
    return encode(dict(row))


async def ensure_team_kb(conn, user: dict):
    if not can_manage_team_kb(user):
        raise PermissionError("팀 KB 관리 권한이 없습니다.")
    kb = await my_kb(conn, user)
    if kb:
        return kb
    return await create_kb_for_team(conn, user["team_id_fk"])


@page_router.get("/kb")
async def portal_kb_page(request: Request):
    async with request.app.state.db.acquire() as conn:
        kb = await my_kb(conn, request.state.user)
        documents = await kb_documents(conn, kb["id"]) if kb else []
    return csrf_response(
        request,
        "portal/kb.html",
        ctx(request, "portal_kb", kb=kb, documents=documents, errors={}, can_manage_team=can_manage_team_kb(request.state.user)),
    )


@page_router.post("/kb/create")
async def portal_kb_create_post(request: Request):
    form = await request.form()
    if not verify_csrf_token(request.app.state.session_secret, form.get("csrf_token"), request.cookies.get(CSRF_COOKIE)):
        raise HTTPException(status_code=400, detail="요청이 만료되었습니다")
    if not can_manage_team_kb(request.state.user):
        raise HTTPException(status_code=403, detail="팀 KB 관리 권한이 없습니다")
    async with request.app.state.db.acquire() as conn:
        kb = await ensure_team_kb(conn, request.state.user)
    await write_audit(request, "kb.create", target_type="knowledge_base", target_id=str(kb["id"]), metadata={"origin": "portal", "kb": kb})
    response = RedirectResponse("/portal/kb", status_code=302)
    return set_flash(response, request.app.state.session_secret, "팀 KB가 활성화되었습니다.")


@page_router.post("/kb/toggle")
async def portal_kb_toggle_post(request: Request):
    form = await request.form()
    if not verify_csrf_token(request.app.state.session_secret, form.get("csrf_token"), request.cookies.get(CSRF_COOKIE)):
        raise HTTPException(status_code=400, detail="요청이 만료되었습니다")
    enabled = str(form.get("enabled") or "false").lower() in ("1", "true", "on", "yes")
    async with request.app.state.db.acquire() as conn:
        prefs = await set_user_kb_enabled(conn, request.state.user["user_id"], enabled)
    await write_audit(request, "kb.activate" if enabled else "kb.deactivate", target_type="user_preferences", target_id=str(request.state.user["user_id"]), metadata={"prefs": prefs})
    response = RedirectResponse("/portal/kb", status_code=302)
    return set_flash(response, request.app.state.session_secret, "KB 기본 설정이 변경되었습니다.")


@page_router.post("/kb/documents")
async def portal_kb_upload_post(request: Request):
    form = await request.form()
    if not verify_csrf_token(request.app.state.session_secret, form.get("csrf_token"), request.cookies.get(CSRF_COOKIE)):
        raise HTTPException(status_code=400, detail="요청이 만료되었습니다")
    if not can_manage_team_kb(request.state.user):
        raise HTTPException(status_code=403, detail="팀 KB 관리 권한이 없습니다")
    files = [item for item in form.getlist("documents") if hasattr(item, "filename") and hasattr(item, "read")]
    errors = []
    uploaded = []
    if not files:
        errors.append("업로드할 문서를 선택해주세요.")
    if len(files) > MAX_UPLOAD_FILES:
        errors.append("한 번에 최대 10개 문서만 업로드할 수 있습니다.")
    async with request.app.state.db.acquire() as conn:
        kb = await ensure_team_kb(conn, request.state.user)
        policies = await active_blocking_dlp_policies(conn)
        if not errors:
            for file in files:
                upload, error = await prepare_upload(conn, kb, file, policies)
                if error:
                    errors.append(error)
                    continue
                try:
                    document = await create_document_row(conn, kb, upload, request.state.user["user_id"])
                    await write_audit(request, "kb.document.upload", target_type="kb_document", target_id=str(document["id"]), metadata={"origin": "portal", "document": document})
                    await write_audit(request, "kb.ingestion.start", target_type="kb_document", target_id=str(document["id"]), metadata={"kb_id": kb["id"]})
                    await ingest_upload(conn, kb, document, upload)
                    await write_audit(request, "kb.ingestion.complete", target_type="kb_document", target_id=str(document["id"]), metadata={"status": "succeeded", "chunks": len(upload["chunks"])})
                    uploaded.append(document["title"])
                except asyncpg.UniqueViolationError:
                    errors.append(f"{upload['filename']}: 이미 업로드된 문서입니다.")
                except Exception as exc:
                    errors.append(f"{upload['filename']}: ingestion 실패 - {exc}")
        documents = await kb_documents(conn, kb["id"])
    if errors:
        return csrf_response(request, "portal/kb.html", ctx(request, "portal_kb", kb=kb, documents=documents, errors={"upload": errors}, can_manage_team=True), status_code=400)
    response = RedirectResponse("/portal/kb", status_code=302)
    return set_flash(response, request.app.state.session_secret, f"{len(uploaded)}개 문서가 업로드/인덱싱되었습니다.")


@api_router.get("")
async def api_my_kb(request: Request):
    async with request.app.state.db.acquire() as conn:
        kb = await my_kb(conn, request.state.user)
        return {"kb": kb, "documents": await kb_documents(conn, kb["id"]) if kb else []}


@api_router.post("/toggle")
async def api_toggle_kb(request: Request):
    body = await request.json()
    enabled = bool(body.get("enabled"))
    async with request.app.state.db.acquire() as conn:
        prefs = await set_user_kb_enabled(conn, request.state.user["user_id"], enabled)
    await write_audit(request, "kb.activate" if enabled else "kb.deactivate", target_type="user_preferences", target_id=str(request.state.user["user_id"]), metadata={"prefs": prefs})
    return JSONResponse(prefs)


__all__ = ["page_router", "api_router", "my_kb", "set_user_kb_enabled"]
