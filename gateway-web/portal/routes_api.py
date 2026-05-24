from fastapi import APIRouter, Request

from portal import queries

router = APIRouter(prefix="/api/portal")


@router.get("/me")
async def me(request: Request):
    user = request.state.user
    return {
        "id": user["user_id"],
        "email": user["email"],
        "display_name": user["display_name"],
        "role": user["role"],
        "team_id": user.get("team_key") or user.get("team_id"),
    }


@router.get("/me/keys")
async def me_keys(request: Request):
    async with request.app.state.db.acquire() as conn:
        return {"items": await queries.my_keys(conn, request.state.user)}


@router.get("/me/budget")
async def me_budget(request: Request):
    async with request.app.state.db.acquire() as conn:
        data = await queries.dashboard(conn, request.state.user)
    return data["budget"]
