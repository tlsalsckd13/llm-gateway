import json
from functools import wraps
from typing import Any, Callable


def client_ip(request) -> str | None:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


def _json_default(value: Any):
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


async def write_audit(
    request,
    action: str,
    target_type: str | None = None,
    target_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    actor_role: str | None = None,
    actor_user_id: int | None = None,
) -> None:
    user = getattr(request.state, "user", None) or {}
    role = actor_role or user.get("role") or "system"
    user_id = actor_user_id if actor_user_id is not None else user.get("user_id")
    async with request.app.state.db.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO audit_log (
              actor_user_id, actor_role, action, target_type, target_id, metadata, ip_address
            )
            VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7::inet)
            """,
            user_id,
            role,
            action,
            target_type,
            target_id,
            json.dumps(metadata or {}, ensure_ascii=False, default=_json_default),
            client_ip(request),
        )


def audit(action: str, target_type: str, target_id_getter: Callable[..., str | None] | None = None):
    def deco(handler):
        @wraps(handler)
        async def wrapper(*args, **kwargs):
            request = kwargs.get("request")
            if request is None:
                request = next((arg for arg in args if hasattr(arg, "app") and hasattr(arg, "state")), None)
            result = await handler(*args, **kwargs)
            if request is not None:
                target_id = target_id_getter(*args, **kwargs) if target_id_getter else None
                await write_audit(request, action, target_type=target_type, target_id=target_id)
            return result

        return wrapper

    return deco
