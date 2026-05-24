from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from admin.queries import encode
from common.api_keys import issue_api_key, parse_expires_at, revoke_api_key
from common.audit import write_audit
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


@router.post("/me/keys")
async def me_key_create(request: Request):
    body = await request.json()
    label = str(body.get("label") or "").strip()
    if not label:
        raise HTTPException(status_code=422, detail="라벨은 필수입니다.")
    try:
        expires_at = parse_expires_at(body.get("expires_at"))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="만료일 형식을 확인해주세요.") from exc
    async with request.app.state.db.acquire() as conn:
        plain_key, row = await issue_api_key(
            conn,
            user_email=request.state.user["email"],
            label=label,
            expires_at=expires_at,
            issued_by_user_id=request.state.user["user_id"],
            issued_via="portal",
        )
    await write_audit(request, "apikey.issue", target_type="api_key", target_id=row["key_prefix"], metadata={"user_id": row["user_id"], "team_id": row["team_id"], "issued_via": "portal"})
    result = encode(row)
    result["plain_key"] = plain_key
    return JSONResponse(status_code=201, content=result)


@router.post("/me/keys/{key_hash}/revoke")
async def me_key_revoke(request: Request, key_hash: str):
    async with request.app.state.db.acquire() as conn:
        row = await revoke_api_key(conn, key_hash=key_hash, actor_user_id=request.state.user["user_id"], owner_email=request.state.user["email"])
    if not row:
        raise HTTPException(status_code=403, detail="본인 API Key만 폐기할 수 있습니다.")
    await write_audit(request, "apikey.revoke", target_type="api_key", target_id=row["key_prefix"], metadata={"user_id": row["user_id"], "team_id": row["team_id"], "actor": "portal"})
    return encode(row)


@router.get("/me/budget")
async def me_budget(request: Request):
    async with request.app.state.db.acquire() as conn:
        data = await queries.dashboard(conn, request.state.user)
    return data["budget"]
