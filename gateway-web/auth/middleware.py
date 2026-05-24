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


async def load_admin_session(request, session_id: str | None):
    if not session_id:
        return None
    async with request.app.state.db.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT s.session_id, s.user_id, u.email, u.display_name, u.role, u.team_id
            FROM web_sessions s
            JOIN web_users u ON u.id = s.user_id
            WHERE s.session_id = $1
              AND s.revoked_at IS NULL
              AND s.expires_at > now()
              AND u.is_active = TRUE
              AND u.role = 'admin'
            """,
            session_id,
        )
    return dict(row) if row else None


def attach_auth_middleware(app):
    @app.middleware("http")
    async def auth_middleware(request, call_next):
        path = request.url.path
        public = (
            path == "/"
            or path == "/healthz"
            or path == "/admin/login"
            or path.startswith("/static/")
        )
        if public:
            return await call_next(request)

        protected = path.startswith("/admin/") or path == "/admin" or path.startswith("/api/admin/")
        if not protected:
            return await call_next(request)

        secret = request.app.state.session_secret
        raw_session_id = unsign_session_id(secret, request.cookies.get(SESSION_COOKIE))
        session = await load_admin_session(request, raw_session_id)
        if not session:
            if path.startswith("/api/"):
                return JSONResponse(status_code=401, content={"error": "unauthorized"})
            return RedirectResponse("/admin/login", status_code=302)

        request.state.user = session
        request.state.session_id = raw_session_id
        return await call_next(request)
