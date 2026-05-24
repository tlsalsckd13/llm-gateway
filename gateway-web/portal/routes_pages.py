from fastapi import APIRouter, Request

from portal import queries

router = APIRouter(prefix="/portal")


def ctx(request: Request, page: str, **kwargs):
    base = {"request": request, "page": page, "section": "portal", "user": request.state.user}
    base.update(kwargs)
    return base


@router.get("/")
async def portal_dashboard(request: Request):
    async with request.app.state.db.acquire() as conn:
        data = await queries.dashboard(conn, request.state.user)
    return request.app.state.templates.TemplateResponse("portal/dashboard.html", ctx(request, "portal_dashboard", data=data))


@router.get("/keys")
async def portal_keys(request: Request):
    async with request.app.state.db.acquire() as conn:
        keys = await queries.my_keys(conn, request.state.user)
    return request.app.state.templates.TemplateResponse("portal/keys.html", ctx(request, "portal_keys", keys=keys))


@router.get("/usage")
async def portal_usage(request: Request):
    async with request.app.state.db.acquire() as conn:
        data = await queries.dashboard(conn, request.state.user)
    return request.app.state.templates.TemplateResponse("portal/usage.html", ctx(request, "portal_usage", data=data))


@router.get("/profile")
async def portal_profile(request: Request):
    return request.app.state.templates.TemplateResponse("portal/profile.html", ctx(request, "portal_profile"))
