from functools import wraps

from fastapi import HTTPException
from itsdangerous import BadSignature, URLSafeSerializer
from starlette.responses import JSONResponse, RedirectResponse


SESSION_COOKIE = "gw_session"


def session_serializer(secret: str) -> URLSafeSerializer:
    return URLSafeSerializer(secret_key=secret, salt="web-session")


def sign_session_id(secret: str, session_id: str) -> str:
    return session_serializer(secret).dumps(session_id)


def unsign_session_id(secret: str, cookie_value: str | None) -> str | None:
    if not cookie_value:
        return None
    try:
        return session_serializer(secret).loads(cookie_value)
    except BadSignature:
        return None


async def load_session(request, session_id: str | None):
    if not session_id:
        return None
    async with request.app.state.db.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT s.session_id,
                   s.user_id,
                   u.id,
                   u.email,
                   u.display_name,
                   u.role,
                   u.team_id,
                   u.team_id_fk,
                   t.team_key,
                   t.name AS team_name
            FROM web_sessions s
            JOIN web_users u ON u.id = s.user_id
            LEFT JOIN teams t ON t.id = u.team_id_fk
            WHERE s.session_id = $1
              AND s.revoked_at IS NULL
              AND s.expires_at > now()
              AND u.is_active = TRUE
              AND u.archived_at IS NULL
            """,
            session_id,
        )
    return dict(row) if row else None


async def load_admin_session(request, session_id: str | None):
    session = await load_session(request, session_id)
    if not session or session["role"] != "admin":
        return None
    return session


def require_role(*roles):
    def deco(handler):
        @wraps(handler)
        async def wrapper(*args, **kwargs):
            request = kwargs.get("request")
            if request is None:
                request = next((arg for arg in args if hasattr(arg, "state") and hasattr(arg, "app")), None)
            user = getattr(request.state, "user", None) if request else None
            if not user or user.get("role") not in roles:
                raise HTTPException(status_code=403, detail="권한이 없습니다")
            return await handler(*args, **kwargs)

        return wrapper

    return deco


def require_self_or_admin(get_target_user_id):
    def deco(handler):
        @wraps(handler)
        async def wrapper(*args, **kwargs):
            request = kwargs.get("request")
            if request is None:
                request = next((arg for arg in args if hasattr(arg, "state") and hasattr(arg, "app")), None)
            actor = getattr(request.state, "user", None) if request else None
            target_id = get_target_user_id(*args, **kwargs)
            if actor and (actor.get("role") == "admin" or str(actor.get("user_id")) == str(target_id) or str(actor.get("id")) == str(target_id)):
                return await handler(*args, **kwargs)
            raise HTTPException(status_code=403, detail="권한이 없습니다")

        return wrapper

    return deco


def attach_auth_middleware(app):
    @app.middleware("http")
    async def auth_middleware(request, call_next):
        path = request.url.path
        public = (
            path == "/"
            or path == "/healthz"
            or path == "/admin/login"
            or path.startswith("/auth/accept-invite")
            or path.startswith("/auth/reset-password")
            or path.startswith("/static/")
        )
        if public:
            return await call_next(request)

        logout_path = path == "/admin/logout"
        admin_path = path.startswith("/admin/") or path == "/admin" or path.startswith("/api/admin/")
        portal_path = path.startswith("/portal/") or path == "/portal" or path.startswith("/api/portal/")
        protected = admin_path or portal_path or logout_path
        if not protected:
            return await call_next(request)

        secret = request.app.state.session_secret
        raw_session_id = unsign_session_id(secret, request.cookies.get(SESSION_COOKIE))
        session = await load_session(request, raw_session_id)
        if not session:
            if path.startswith("/api/"):
                return JSONResponse(status_code=401, content={"error": "unauthorized"})
            return RedirectResponse("/admin/login", status_code=302)
        if admin_path and not logout_path and session["role"] != "admin":
            if path.startswith("/api/"):
                return JSONResponse(status_code=403, content={"error": "forbidden"})
            return JSONResponse(status_code=403, content={"error": "권한이 없습니다"})

        request.state.user = session
        request.state.session_id = raw_session_id
        return await call_next(request)
