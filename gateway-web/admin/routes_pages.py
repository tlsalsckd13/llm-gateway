from datetime import timedelta

from fastapi import APIRouter, Request

from admin import queries

router = APIRouter(prefix="/admin")


def ctx(request: Request, page: str, **kwargs):
    base = {"request": request, "page": page, "user": request.state.user}
    base.update(kwargs)
    return base


@router.get("/")
async def overview_page(request: Request):
    async with request.app.state.db.acquire() as conn:
        data = await queries.overview(conn)
    return request.app.state.templates.TemplateResponse("admin/overview.html", ctx(request, "overview", data=data))


@router.get("/usage")
async def usage_page(request: Request):
    start, end = queries.default_range(days=7)
    async with request.app.state.db.acquire() as conn:
        data = await queries.usage_summary(conn, start, end)
    return request.app.state.templates.TemplateResponse("admin/usage.html", ctx(request, "usage", data=data))


@router.get("/budgets")
async def budgets_page(request: Request):
    async with request.app.state.db.acquire() as conn:
        data = await queries.budget_rows(conn)
    return request.app.state.templates.TemplateResponse("admin/budgets.html", ctx(request, "budgets", budgets=data))


@router.get("/keys")
async def keys_page(request: Request):
    async with request.app.state.db.acquire() as conn:
        data = await queries.key_rows(conn)
    return request.app.state.templates.TemplateResponse("admin/keys.html", ctx(request, "keys", keys=data))


@router.get("/dlp")
async def dlp_page(request: Request):
    async with request.app.state.db.acquire() as conn:
        data = await queries.dlp_rows(conn)
    return request.app.state.templates.TemplateResponse("admin/dlp.html", ctx(request, "dlp", events=data))


@router.get("/audit")
async def audit_page(request: Request):
    async with request.app.state.db.acquire() as conn:
        data = await queries.audit_rows(conn)
    return request.app.state.templates.TemplateResponse("admin/audit.html", ctx(request, "audit", events=data))
