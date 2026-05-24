from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from admin import queries
from admin.queries import encode
from auth.csrf import CSRF_COOKIE, create_csrf_token, verify_csrf_token
from common.api_keys import issue_api_key, parse_expires_at, revoke_api_key, selectable_users
from common.audit import write_audit
from common.flash import set_flash

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


def parse_key_form(form) -> tuple[dict | None, dict[str, str]]:
    errors: dict[str, str] = {}
    user_email = str(form.get("user_email") or "").strip().lower()
    label = str(form.get("label") or "").strip()
    expires_raw = str(form.get("expires_at") or "").strip()
    if not user_email:
        errors["user_email"] = "사용자를 선택해주세요."
    if not label:
        errors["label"] = "라벨은 필수입니다."
    try:
        expires_at = parse_expires_at(expires_raw)
    except ValueError:
        errors["expires_at"] = "만료일 형식을 확인해주세요."
        expires_at = None
    payload = {"user_email": user_email, "label": label, "expires_at": expires_at, "expires_at_raw": expires_raw}
    return (None, errors) if errors else (payload, {})


@page_router.get("/keys/new")
async def admin_key_new_page(request: Request):
    async with request.app.state.db.acquire() as conn:
        users = await selectable_users(conn)
    values = {"label": "", "expires_at": ""}
    return csrf_response(request, "admin/key_form.html", ctx(request, "keys", values=values, errors={}, users=users))


@page_router.post("/keys")
async def admin_key_create_post(request: Request):
    form = await request.form()
    csrf_ok = verify_csrf_token(request.app.state.session_secret, form.get("csrf_token"), request.cookies.get(CSRF_COOKIE))
    payload, errors = parse_key_form(form)
    async with request.app.state.db.acquire() as conn:
        users = await selectable_users(conn)
        if not csrf_ok:
            errors = {"form": "요청이 만료되었습니다. 다시 시도해주세요."}
        if errors:
            return csrf_response(request, "admin/key_form.html", ctx(request, "keys", values=dict(form), errors=errors, users=users), status_code=400)
        try:
            plain_key, row = await issue_api_key(
                conn,
                user_email=payload["user_email"],
                label=payload["label"],
                expires_at=payload["expires_at"],
                issued_by_user_id=request.state.user["user_id"],
                issued_via="admin",
            )
        except ValueError:
            return csrf_response(request, "admin/key_form.html", ctx(request, "keys", values=dict(form), errors={"user_email": "활성 사용자를 찾을 수 없습니다."}, users=users), status_code=400)
    await write_audit(request, "apikey.issue", target_type="api_key", target_id=row["key_prefix"], metadata={"user_id": row["user_id"], "team_id": row["team_id"], "issued_via": "admin"})
    return request.app.state.templates.TemplateResponse(
        "admin/key_once.html",
        ctx(request, "keys", key=row, plain_key=plain_key),
    )


@page_router.post("/keys/{key_hash}/revoke")
async def admin_key_revoke_post(request: Request, key_hash: str):
    form = await request.form()
    if not verify_csrf_token(request.app.state.session_secret, form.get("csrf_token"), request.cookies.get(CSRF_COOKIE)):
        raise HTTPException(status_code=400, detail="요청이 만료되었습니다")
    async with request.app.state.db.acquire() as conn:
        row = await revoke_api_key(conn, key_hash=key_hash, actor_user_id=request.state.user["user_id"])
    if not row:
        raise HTTPException(status_code=404, detail="API Key를 찾을 수 없습니다")
    await write_audit(request, "apikey.revoke", target_type="api_key", target_id=row["key_prefix"], metadata={"user_id": row["user_id"], "team_id": row["team_id"], "actor": "admin"})
    response = RedirectResponse("/admin/keys", status_code=302)
    return set_flash(response, request.app.state.session_secret, "API Key가 폐기되었습니다.")


@api_router.post("/keys")
async def api_admin_key_create(request: Request):
    body = await request.json()
    label = str(body.get("label") or "").strip()
    user_email = str(body.get("user_email") or "").strip().lower()
    if not user_email or not label:
        raise HTTPException(status_code=422, detail="user_email과 label은 필수입니다.")
    try:
        expires_at = parse_expires_at(body.get("expires_at"))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="만료일 형식을 확인해주세요.") from exc
    async with request.app.state.db.acquire() as conn:
        try:
            plain_key, row = await issue_api_key(
                conn,
                user_email=user_email,
                label=label,
                expires_at=expires_at,
                issued_by_user_id=request.state.user["user_id"],
                issued_via="admin",
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="활성 사용자를 찾을 수 없습니다.") from exc
    await write_audit(request, "apikey.issue", target_type="api_key", target_id=row["key_prefix"], metadata={"user_id": row["user_id"], "team_id": row["team_id"], "issued_via": "admin"})
    result = encode(row)
    result["plain_key"] = plain_key
    return JSONResponse(status_code=201, content=result)


@api_router.post("/keys/{key_hash}/revoke")
async def api_admin_key_revoke(request: Request, key_hash: str):
    async with request.app.state.db.acquire() as conn:
        row = await revoke_api_key(conn, key_hash=key_hash, actor_user_id=request.state.user["user_id"])
    if not row:
        raise HTTPException(status_code=404, detail="API Key를 찾을 수 없습니다")
    await write_audit(request, "apikey.revoke", target_type="api_key", target_id=row["key_prefix"], metadata={"user_id": row["user_id"], "team_id": row["team_id"], "actor": "admin"})
    return encode(row)
