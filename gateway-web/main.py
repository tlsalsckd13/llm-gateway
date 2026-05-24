import os
import socket
from contextlib import asynccontextmanager

import asyncpg
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from admin.routes_api import router as admin_api_router
from admin.routes_pages import router as admin_pages_router
from auth.middleware import attach_auth_middleware
from auth.routes import router as auth_router
from portal.routes_api import router as portal_api_router
from portal.routes_pages import router as portal_pages_router


def db_dsn() -> str:
    return (
        f"host={os.environ['DB_HOST']} "
        f"port={os.environ.get('DB_PORT', '5432')} "
        f"dbname={os.environ['DB_NAME']} "
        f"user={os.environ['DB_USER']} "
        "sslmode=require"
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.db = await asyncpg.create_pool(
        host=os.environ["DB_HOST"],
        port=int(os.environ.get("DB_PORT", "5432")),
        database=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        ssl="require",
        min_size=1,
        max_size=5,
    )
    app.state.collector_url = os.environ.get("COLLECTOR_URL", "http://usage-collector:8080")
    yield
    await app.state.db.close()


app = FastAPI(lifespan=lifespan)
templates = Jinja2Templates(directory="templates")
app.state.templates = templates
app.state.session_secret = os.environ["SESSION_SECRET"]
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com https://cdn.jsdelivr.net; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "frame-ancestors 'none'"
    )
    return response


attach_auth_middleware(app)
app.include_router(auth_router)
app.include_router(admin_pages_router)
app.include_router(admin_api_router)
app.include_router(portal_pages_router)
app.include_router(portal_api_router)


@app.get("/")
async def root():
    return RedirectResponse("/admin/")


@app.get("/healthz")
async def healthz(request: Request):
    status = {"status": "ok", "db": "ok", "collector": "unknown", "gateway": "unknown"}
    code = 200
    try:
        async with request.app.state.db.acquire() as conn:
            await conn.fetchval("SELECT 1")
    except Exception as exc:
        status["status"] = "unhealthy"
        status["db"] = str(exc)
        code = 503

    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(f"{request.app.state.collector_url}/health")
        status["collector"] = "ok" if resp.status_code < 500 else f"http_{resp.status_code}"
    except Exception as exc:
        status["collector"] = str(exc)
        status["status"] = "degraded"

    try:
        with socket.create_connection(("gateway", 8787), timeout=1.5):
            status["gateway"] = "ok"
    except Exception as exc:
        status["gateway"] = str(exc)
        status["status"] = "degraded"

    return JSONResponse(status_code=code, content=status)
