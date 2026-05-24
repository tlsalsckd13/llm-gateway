import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response

from auth.csrf import CSRF_COOKIE, create_csrf_token, verify_csrf_token
from auth.magic_link import consume_magic_link, get_magic_link, magic_link_is_usable
from auth.middleware import SESSION_COOKIE, sign_session_id
from auth.password import hash_password, validate_password_policy, verify_password

router = APIRouter()

LOGIN_ERROR = "이메일 또는 비밀번호가 올바르지 않습니다"
SESSION_HOURS = 12


def client_ip(request: Request) -> str | None:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


async def write_audit(
    request: Request,
    action: str,
    actor_role: str,
    actor_user_id: int | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    metadata: str = "{}",
):
    async with request.app.state.db.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO audit_log (
              actor_user_id, actor_role, action, target_type, target_id, metadata, ip_address
            )
            VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7::inet)
            """,
            actor_user_id,
            actor_role,
            action,
            target_type,
            target_id,
            metadata,
            client_ip(request),
        )


async def create_web_session(request: Request, conn, user, *, actor_role: str | None = None):
    now = datetime.now(timezone.utc)
    session_id = secrets.token_hex(32)
    expires_at = now + timedelta(hours=SESSION_HOURS)
    await conn.execute(
        """
        INSERT INTO web_sessions (session_id, user_id, expires_at, ip_address, user_agent)
        VALUES ($1, $2, $3, $4::inet, $5)
        """,
        session_id,
        user["id"],
        expires_at,
        client_ip(request),
        request.headers.get("user-agent"),
    )
    await conn.execute(
        """
        UPDATE web_users
        SET failed_login_count = 0, locked_until = NULL, last_login_at = now()
        WHERE id = $1
        """,
        user["id"],
    )
    await conn.execute(
        """
        INSERT INTO audit_log (actor_user_id, actor_role, action, target_type, target_id, metadata, ip_address)
        VALUES ($1, $2, 'login.success', 'web_user', $3, '{}'::jsonb, $4::inet)
        """,
        user["id"],
        actor_role or user["role"],
        str(user["id"]),
        client_ip(request),
    )
    return session_id


def session_redirect(request: Request, user, path: str | None = None):
    target = path or ("/admin/" if user["role"] == "admin" else "/portal/")
    response = RedirectResponse(target, status_code=302)
    response.delete_cookie(CSRF_COOKIE, path="/admin")
    response.delete_cookie(CSRF_COOKIE, path="/auth")
    return response


def set_session_cookie(request: Request, response, session_id: str):
    response.set_cookie(
        SESSION_COOKIE,
        sign_session_id(request.app.state.session_secret, session_id),
        httponly=True,
        secure=True,
        samesite="strict",
        max_age=SESSION_HOURS * 3600,
        path="/",
    )
    return response


@router.get("/admin/login")
async def login_form(request: Request):
    token = create_csrf_token(request.app.state.session_secret)
    response = request.app.state.templates.TemplateResponse(
        "admin/login.html",
        {"request": request, "csrf_token": token, "error": None},
    )
    response.set_cookie(
        CSRF_COOKIE,
        token,
        httponly=True,
        secure=True,
        samesite="strict",
        max_age=3600,
        path="/admin",
    )
    return response


@router.head("/admin/login")
async def login_head():
    return Response(status_code=200)


@router.post("/admin/login")
async def login_post(request: Request):
    form = await request.form()
    csrf_ok = verify_csrf_token(
        request.app.state.session_secret,
        form.get("csrf_token"),
        request.cookies.get(CSRF_COOKIE),
    )
    if not csrf_ok:
        token = create_csrf_token(request.app.state.session_secret)
        response = request.app.state.templates.TemplateResponse(
            "admin/login.html",
            {"request": request, "csrf_token": token, "error": LOGIN_ERROR},
            status_code=400,
        )
        response.set_cookie(CSRF_COOKIE, token, httponly=True, secure=True, samesite="strict", max_age=3600, path="/admin")
        return response

    email = str(form.get("email", "")).strip().lower()
    password = str(form.get("password", ""))

    async with request.app.state.db.acquire() as conn:
        user = await conn.fetchrow(
            """
            SELECT id, email, display_name, role, team_id, password_hash, is_active,
                   failed_login_count, locked_until, archived_at
            FROM web_users
            WHERE lower(email) = $1
            """,
            email,
        )

        now = datetime.now(timezone.utc)
        locked = bool(user and user["locked_until"] and user["locked_until"] > now)
        ok = bool(
            user
            and user["is_active"]
            and not user["archived_at"]
            and not locked
            and verify_password(password, user["password_hash"])
        )

        if not ok:
            if user and not locked:
                await conn.execute(
                    """
                    UPDATE web_users
                    SET failed_login_count = failed_login_count + 1,
                        locked_until = CASE
                          WHEN failed_login_count + 1 >= 5 THEN now() + interval '10 minutes'
                          ELSE locked_until
                        END
                    WHERE id = $1
                    """,
                    user["id"],
                )
            await conn.execute(
                """
                INSERT INTO audit_log (actor_role, action, target_type, target_id, metadata, ip_address)
                VALUES ('anonymous', 'login.failure', 'web_user', $1, '{}'::jsonb, $2::inet)
                """,
                email or None,
                client_ip(request),
            )
            token = create_csrf_token(request.app.state.session_secret)
            response = request.app.state.templates.TemplateResponse(
                "admin/login.html",
                {"request": request, "csrf_token": token, "error": LOGIN_ERROR},
                status_code=401,
            )
            response.set_cookie(CSRF_COOKIE, token, httponly=True, secure=True, samesite="strict", max_age=3600, path="/admin")
            return response

        session_id = await create_web_session(request, conn, user)

    return set_session_cookie(request, session_redirect(request, user), session_id)


@router.get("/admin/logout")
async def logout(request: Request):
    session_id = getattr(request.state, "session_id", None)
    user = getattr(request.state, "user", None)
    if session_id:
        async with request.app.state.db.acquire() as conn:
            await conn.execute("UPDATE web_sessions SET revoked_at = now() WHERE session_id = $1", session_id)
            await conn.execute(
                """
                INSERT INTO audit_log (actor_user_id, actor_role, action, target_type, target_id, metadata, ip_address)
                VALUES ($1, $2, 'logout', 'web_session', $3, '{}'::jsonb, $4::inet)
                """,
                user["user_id"] if user else None,
                user["role"] if user else "unknown",
                session_id[:12],
                client_ip(request),
            )
    response = RedirectResponse("/admin/login", status_code=302)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


def auth_csrf_response(request: Request, template: str, context: dict, status_code: int = 200):
    token = create_csrf_token(request.app.state.session_secret)
    context["csrf_token"] = token
    response = request.app.state.templates.TemplateResponse(template, context, status_code=status_code)
    response.set_cookie(CSRF_COOKIE, token, httponly=True, secure=True, samesite="strict", max_age=3600, path="/auth")
    return response


@router.get("/auth/accept-invite")
async def accept_invite_form(request: Request):
    token = request.query_params.get("token") or ""
    async with request.app.state.db.acquire() as conn:
        link = await get_magic_link(conn, token, "invite")
    if not magic_link_is_usable(link):
        return auth_csrf_response(
            request,
            "auth/magic_password.html",
            {"request": request, "purpose": "invite", "token": token, "link": link, "error": "초대 링크가 만료되었거나 이미 사용되었습니다."},
            status_code=403,
        )
    return auth_csrf_response(request, "auth/magic_password.html", {"request": request, "purpose": "invite", "token": token, "link": link, "error": None})


@router.post("/auth/accept-invite")
async def accept_invite_post(request: Request):
    form = await request.form()
    token = str(form.get("token", ""))
    csrf_ok = verify_csrf_token(request.app.state.session_secret, form.get("csrf_token"), request.cookies.get(CSRF_COOKIE))
    if not csrf_ok:
        return auth_csrf_response(request, "auth/magic_password.html", {"request": request, "purpose": "invite", "token": token, "link": None, "error": "요청이 만료되었습니다. 다시 시도해주세요."}, status_code=400)
    password = str(form.get("password", ""))
    password_confirm = str(form.get("password_confirm", ""))
    if password != password_confirm:
        return auth_csrf_response(request, "auth/magic_password.html", {"request": request, "purpose": "invite", "token": token, "link": None, "error": "비밀번호 확인이 일치하지 않습니다."}, status_code=400)
    ok, message = validate_password_policy(password)
    if not ok:
        return auth_csrf_response(request, "auth/magic_password.html", {"request": request, "purpose": "invite", "token": token, "link": None, "error": message}, status_code=400)
    async with request.app.state.db.acquire() as conn:
        link = await get_magic_link(conn, token, "invite")
        if not magic_link_is_usable(link):
            return auth_csrf_response(request, "auth/magic_password.html", {"request": request, "purpose": "invite", "token": token, "link": link, "error": "초대 링크가 만료되었거나 이미 사용되었습니다."}, status_code=403)
        user_id = await consume_magic_link(conn, token, "invite")
        await conn.execute(
            """
            UPDATE web_users
            SET password_hash = $2,
                last_password_changed_at = now(),
                is_active = TRUE,
                failed_login_count = 0,
                locked_until = NULL
            WHERE id = $1
            """,
            user_id,
            hash_password(password),
        )
        user = await conn.fetchrow("SELECT id, email, display_name, role FROM web_users WHERE id = $1", user_id)
        session_id = await create_web_session(request, conn, user, actor_role=user["role"])
    return set_session_cookie(request, session_redirect(request, user, "/portal/"), session_id)


@router.get("/auth/reset-password")
async def reset_password_form(request: Request):
    token = request.query_params.get("token") or ""
    async with request.app.state.db.acquire() as conn:
        link = await get_magic_link(conn, token, "password_reset")
    if not magic_link_is_usable(link):
        return auth_csrf_response(
            request,
            "auth/magic_password.html",
            {"request": request, "purpose": "password_reset", "token": token, "link": link, "error": "비밀번호 재설정 링크가 만료되었거나 이미 사용되었습니다."},
            status_code=403,
        )
    return auth_csrf_response(request, "auth/magic_password.html", {"request": request, "purpose": "password_reset", "token": token, "link": link, "error": None})


@router.post("/auth/reset-password")
async def reset_password_post(request: Request):
    form = await request.form()
    token = str(form.get("token", ""))
    csrf_ok = verify_csrf_token(request.app.state.session_secret, form.get("csrf_token"), request.cookies.get(CSRF_COOKIE))
    if not csrf_ok:
        return auth_csrf_response(request, "auth/magic_password.html", {"request": request, "purpose": "password_reset", "token": token, "link": None, "error": "요청이 만료되었습니다. 다시 시도해주세요."}, status_code=400)
    password = str(form.get("password", ""))
    password_confirm = str(form.get("password_confirm", ""))
    if password != password_confirm:
        return auth_csrf_response(request, "auth/magic_password.html", {"request": request, "purpose": "password_reset", "token": token, "link": None, "error": "비밀번호 확인이 일치하지 않습니다."}, status_code=400)
    ok, message = validate_password_policy(password)
    if not ok:
        return auth_csrf_response(request, "auth/magic_password.html", {"request": request, "purpose": "password_reset", "token": token, "link": None, "error": message}, status_code=400)
    async with request.app.state.db.acquire() as conn:
        link = await get_magic_link(conn, token, "password_reset")
        if not magic_link_is_usable(link):
            return auth_csrf_response(request, "auth/magic_password.html", {"request": request, "purpose": "password_reset", "token": token, "link": link, "error": "비밀번호 재설정 링크가 만료되었거나 이미 사용되었습니다."}, status_code=403)
        user_id = await consume_magic_link(conn, token, "password_reset")
        await conn.execute(
            """
            UPDATE web_users
            SET password_hash = $2,
                last_password_changed_at = now(),
                failed_login_count = 0,
                locked_until = NULL
            WHERE id = $1
            """,
            user_id,
            hash_password(password),
        )
        await conn.execute("UPDATE web_sessions SET revoked_at = now() WHERE user_id = $1 AND revoked_at IS NULL", user_id)
        user = await conn.fetchrow("SELECT id, email, display_name, role FROM web_users WHERE id = $1", user_id)
        session_id = await create_web_session(request, conn, user, actor_role=user["role"])
    return set_session_cookie(request, session_redirect(request, user), session_id)
