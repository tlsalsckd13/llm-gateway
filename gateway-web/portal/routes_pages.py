from fastapi import APIRouter, Request
from fastapi import HTTPException
from fastapi.responses import RedirectResponse

from auth.csrf import CSRF_COOKIE, create_csrf_token, verify_csrf_token
from common.api_keys import issue_api_key, parse_expires_at, revoke_api_key
from common.audit import write_audit
from common.flash import set_flash
from portal import queries

router = APIRouter(prefix="/portal")


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


def parse_key_form(form):
    errors = {}
    label = str(form.get("label") or "").strip()
    expires_raw = str(form.get("expires_at") or "").strip()
    if not label:
        errors["label"] = "라벨은 필수입니다."
    try:
        expires_at = parse_expires_at(expires_raw)
    except ValueError:
        errors["expires_at"] = "만료일 형식을 확인해주세요."
        expires_at = None
    payload = {"label": label, "expires_at": expires_at, "expires_at_raw": expires_raw}
    return (None, errors) if errors else (payload, {})


@router.get("/")
async def portal_dashboard(request: Request):
    async with request.app.state.db.acquire() as conn:
        data = await queries.dashboard(conn, request.state.user)
    return request.app.state.templates.TemplateResponse("portal/dashboard.html", ctx(request, "portal_dashboard", data=data))


@router.get("/keys")
async def portal_keys(request: Request):
    async with request.app.state.db.acquire() as conn:
        keys = await queries.my_keys(conn, request.state.user)
    return csrf_response(request, "portal/keys.html", ctx(request, "portal_keys", keys=keys))


@router.get("/keys/new")
async def portal_key_new_page(request: Request):
    return csrf_response(request, "portal/key_form.html", ctx(request, "portal_keys", values={}, errors={}))


@router.post("/keys")
async def portal_key_create_post(request: Request):
    form = await request.form()
    csrf_ok = verify_csrf_token(request.app.state.session_secret, form.get("csrf_token"), request.cookies.get(CSRF_COOKIE))
    payload, errors = parse_key_form(form)
    if not csrf_ok:
        errors = {"form": "요청이 만료되었습니다. 다시 시도해주세요."}
    if errors:
        return csrf_response(request, "portal/key_form.html", ctx(request, "portal_keys", values=dict(form), errors=errors), status_code=400)
    async with request.app.state.db.acquire() as conn:
        plain_key, row = await issue_api_key(
            conn,
            user_email=request.state.user["email"],
            label=payload["label"],
            expires_at=payload["expires_at"],
            issued_by_user_id=request.state.user["user_id"],
            issued_via="portal",
        )
    await write_audit(request, "apikey.issue", target_type="api_key", target_id=row["key_prefix"], metadata={"user_id": row["user_id"], "team_id": row["team_id"], "issued_via": "portal"})
    return request.app.state.templates.TemplateResponse("portal/key_once.html", ctx(request, "portal_keys", key=row, plain_key=plain_key))


@router.post("/keys/{key_hash}/revoke")
async def portal_key_revoke_post(request: Request, key_hash: str):
    form = await request.form()
    if not verify_csrf_token(request.app.state.session_secret, form.get("csrf_token"), request.cookies.get(CSRF_COOKIE)):
        raise HTTPException(status_code=400, detail="요청이 만료되었습니다")
    async with request.app.state.db.acquire() as conn:
        row = await revoke_api_key(conn, key_hash=key_hash, actor_user_id=request.state.user["user_id"], owner_email=request.state.user["email"])
    if not row:
        raise HTTPException(status_code=403, detail="본인 API Key만 폐기할 수 있습니다")
    await write_audit(request, "apikey.revoke", target_type="api_key", target_id=row["key_prefix"], metadata={"user_id": row["user_id"], "team_id": row["team_id"], "actor": "portal"})
    response = RedirectResponse("/portal/keys", status_code=302)
    return set_flash(response, request.app.state.session_secret, "API Key가 폐기되었습니다.")


@router.get("/usage")
async def portal_usage(request: Request):
    async with request.app.state.db.acquire() as conn:
        data = await queries.dashboard(conn, request.state.user)
    return request.app.state.templates.TemplateResponse("portal/usage.html", ctx(request, "portal_usage", data=data))


@router.get("/profile")
async def portal_profile(request: Request):
    return request.app.state.templates.TemplateResponse("portal/profile.html", ctx(request, "portal_profile"))
