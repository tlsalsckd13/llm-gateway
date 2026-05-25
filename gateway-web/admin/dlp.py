import html
import re
from typing import Any

import asyncpg
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from admin.queries import encode, records
from auth.csrf import CSRF_COOKIE, create_csrf_token, verify_csrf_token
from common.audit import write_audit
from common.flash import set_flash

page_router = APIRouter(prefix="/admin")
api_router = APIRouter(prefix="/api/admin")

PATTERN_TYPES = ("krn", "card", "brn", "account", "custom")
ACTIONS = ("block", "mask", "block_and_mask")


class DlpPolicyInput(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    pattern_type: str = "custom"
    pattern_regex: str = Field(min_length=1, max_length=2000)
    redaction_token: str = Field(min_length=1, max_length=120)
    action: str = "block_and_mask"
    priority: int = Field(default=100, ge=1, le=10000)
    description: str | None = None
    is_active: bool = True


def ctx(request: Request, page: str, **kwargs):
    base = {
        "request": request,
        "page": page,
        "user": request.state.user,
        "pattern_types": PATTERN_TYPES,
        "actions": ACTIONS,
    }
    base.update(kwargs)
    return base


def csrf_response(request: Request, template: str, context: dict, status_code: int = 200):
    token = create_csrf_token(request.app.state.session_secret)
    context["csrf_token"] = token
    response = request.app.state.templates.TemplateResponse(template, context, status_code=status_code)
    response.set_cookie(CSRF_COOKIE, token, httponly=True, secure=True, samesite="strict", max_age=3600, path="/admin")
    return response


def parse_bool(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).lower() in ("1", "true", "on", "yes")


def validate_policy_payload(data: dict) -> tuple[dict | None, dict[str, str]]:
    errors: dict[str, str] = {}
    name = str(data.get("name") or "").strip()
    pattern_type = str(data.get("pattern_type") or "custom").strip()
    pattern_regex = str(data.get("pattern_regex") or "").strip()
    redaction_token = str(data.get("redaction_token") or "").strip()
    action = str(data.get("action") or "block_and_mask").strip()
    description = str(data.get("description") or "").strip() or None

    if not name:
        errors["name"] = "정책 이름은 필수입니다."
    if pattern_type not in PATTERN_TYPES:
        errors["pattern_type"] = "지원하는 패턴 타입을 선택해주세요."
    if not pattern_regex:
        errors["pattern_regex"] = "정규식은 필수입니다."
    else:
        try:
            re.compile(pattern_regex)
        except re.error as exc:
            errors["pattern_regex"] = f"정규식 오류: {exc}"
    if not redaction_token:
        errors["redaction_token"] = "마스킹 토큰은 필수입니다."
    if action not in ACTIONS:
        errors["action"] = "지원하는 액션을 선택해주세요."

    try:
        priority = int(data.get("priority") or 100)
        if priority < 1 or priority > 10000:
            errors["priority"] = "우선순위는 1부터 10000 사이로 입력해주세요."
    except (TypeError, ValueError):
        priority = 100
        errors["priority"] = "우선순위는 숫자로 입력해주세요."

    payload = {
        "name": name,
        "pattern_type": pattern_type,
        "pattern_regex": pattern_regex,
        "redaction_token": redaction_token,
        "action": action,
        "priority": priority,
        "description": description,
        "is_active": parse_bool(data.get("is_active"), default=False),
    }
    return (None, errors) if errors else (payload, {})


def form_values(data: dict, *, default_active: bool = False) -> dict:
    values = dict(data)
    if "is_active" not in values:
        values["is_active"] = default_active
    else:
        values["is_active"] = parse_bool(values["is_active"])
    return values


def compiled_policy(policy: dict) -> dict:
    item = dict(policy)
    item["_compiled"] = re.compile(str(item["pattern_regex"]))
    return item


def compile_policies(policies: list[dict]) -> tuple[list[dict], list[dict]]:
    compiled = []
    invalid = []
    for policy in policies:
        try:
            compiled.append(compiled_policy(policy))
        except re.error as exc:
            invalid.append({"id": policy.get("id"), "name": policy.get("name"), "error": str(exc)})
    return compiled, invalid


def non_overlapping_spans(matches: list[dict]) -> list[tuple[int, int]]:
    spans = sorted(
        [(item["start"], item["end"]) for item in matches if item["end"] > item["start"]],
        key=lambda item: (item[0], -(item[1] - item[0])),
    )
    result = []
    cursor = 0
    for start, end in spans:
        if start < cursor:
            continue
        result.append((start, end))
        cursor = end
    return result


def highlight_text(text: str, matches: list[dict]) -> str:
    parts = []
    cursor = 0
    for start, end in non_overlapping_spans(matches):
        parts.append(html.escape(text[cursor:start]))
        parts.append(f"<mark>{html.escape(text[start:end])}</mark>")
        cursor = end
    parts.append(html.escape(text[cursor:]))
    return "".join(parts)


def evaluate_sample(sample_text: str, policies: list[dict]) -> dict:
    text = sample_text or ""
    compiled, invalid = compile_policies(policies)
    matches = []
    for policy in compiled:
        if len(matches) >= 100:
            break
        for match in policy["_compiled"].finditer(text):
            if match.end() <= match.start():
                continue
            matches.append(
                {
                    "policy_id": policy.get("id"),
                    "name": policy.get("name"),
                    "pattern_type": policy.get("pattern_type"),
                    "action": policy.get("action"),
                    "redaction_token": policy.get("redaction_token"),
                    "start": match.start(),
                    "end": match.end(),
                    "matched_text": match.group(0),
                }
            )
            if len(matches) >= 100:
                break

    masked_text = text
    applied = []
    for policy in compiled:
        if policy.get("action") not in ("mask", "block_and_mask"):
            continue
        masked_text, count = policy["_compiled"].subn(policy.get("redaction_token") or "[REDACTED]", masked_text)
        if count:
            applied.append(policy.get("pattern_type") or policy.get("name"))

    will_block = any(item["action"] in ("block", "block_and_mask") for item in matches)
    return {
        "sample_text": text,
        "matches": matches,
        "invalid_policies": invalid,
        "will_block": will_block,
        "masked_text": masked_text,
        "applied": applied,
        "highlighted_text": highlight_text(text, matches),
    }


async def list_policies(conn):
    rows = await conn.fetch(
        """
        SELECT p.id,
               p.name,
               p.pattern_type,
               p.pattern_regex,
               p.redaction_token,
               p.action,
               p.is_active,
               p.priority,
               p.description,
               p.created_at,
               p.updated_at,
               COALESCE((
                   SELECT count(*)::int
                   FROM audit_log al
                   WHERE al.created_at >= now() - interval '7 days'
                     AND al.action IN ('dlp.block', 'dlp.mask')
                     AND (
                       al.metadata->>'pattern' = p.pattern_type
                       OR COALESCE(al.metadata->'patterns', '[]'::jsonb) ? p.pattern_type
                     )
               ), 0)::int AS match_count_7d
        FROM dlp_policies p
        ORDER BY p.priority ASC, p.id ASC
        """
    )
    return records(rows)


async def active_policies(conn):
    rows = await conn.fetch(
        """
        SELECT id, name, pattern_type, pattern_regex, redaction_token, action, priority
        FROM dlp_policies
        WHERE is_active = TRUE
        ORDER BY priority ASC, id ASC
        """
    )
    return records(rows)


async def get_policy(conn, policy_id: int):
    row = await conn.fetchrow(
        """
        SELECT id, name, pattern_type, pattern_regex, redaction_token, action,
               is_active, priority, description, created_at, updated_at
        FROM dlp_policies
        WHERE id = $1
        """,
        policy_id,
    )
    return encode(dict(row)) if row else None


async def create_policy(conn, payload: dict, actor_id: int):
    row = await conn.fetchrow(
        """
        INSERT INTO dlp_policies (
          name, pattern_type, pattern_regex, redaction_token, action,
          is_active, priority, description, created_by_user_id, updated_by_user_id
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $9)
        RETURNING *
        """,
        payload["name"],
        payload["pattern_type"],
        payload["pattern_regex"],
        payload["redaction_token"],
        payload["action"],
        payload["is_active"],
        payload["priority"],
        payload["description"],
        actor_id,
    )
    return dict(row)


async def update_policy(conn, policy_id: int, payload: dict, actor_id: int):
    before = await conn.fetchrow("SELECT * FROM dlp_policies WHERE id = $1", policy_id)
    if not before:
        return None, None
    row = await conn.fetchrow(
        """
        UPDATE dlp_policies
        SET name = $2,
            pattern_type = $3,
            pattern_regex = $4,
            redaction_token = $5,
            action = $6,
            is_active = $7,
            priority = $8,
            description = $9,
            updated_by_user_id = $10,
            updated_at = now()
        WHERE id = $1
        RETURNING *
        """,
        policy_id,
        payload["name"],
        payload["pattern_type"],
        payload["pattern_regex"],
        payload["redaction_token"],
        payload["action"],
        payload["is_active"],
        payload["priority"],
        payload["description"],
        actor_id,
    )
    return dict(row), dict(before)


async def deactivate_policy(conn, policy_id: int, actor_id: int):
    before = await conn.fetchrow("SELECT * FROM dlp_policies WHERE id = $1", policy_id)
    if not before:
        return None, None
    row = await conn.fetchrow(
        """
        UPDATE dlp_policies
        SET is_active = FALSE,
            updated_by_user_id = $2,
            updated_at = now()
        WHERE id = $1
        RETURNING *
        """,
        policy_id,
        actor_id,
    )
    return dict(row), dict(before)


def default_policy_values():
    return {
        "name": "",
        "pattern_type": "custom",
        "pattern_regex": "",
        "redaction_token": "[REDACTED-CUSTOM]",
        "action": "block_and_mask",
        "priority": 100,
        "description": "",
        "is_active": True,
        "sample_text": "",
    }


def render_policy_form(
    request: Request,
    *,
    mode: str,
    values: dict,
    errors: dict | None = None,
    policy: dict | None = None,
    preview: dict | None = None,
    status_code: int = 200,
):
    return csrf_response(
        request,
        "admin/dlp_policy_form.html",
        ctx(request, "dlp", active_tab="policies", mode=mode, values=values, errors=errors or {}, policy=policy, preview=preview),
        status_code=status_code,
    )


@page_router.get("/dlp/policies")
async def dlp_policies_page(request: Request):
    async with request.app.state.db.acquire() as conn:
        policies = await list_policies(conn)
    return csrf_response(
        request,
        "admin/dlp_policies.html",
        ctx(request, "dlp", active_tab="policies", policies=policies),
    )


@page_router.get("/dlp/policies/new")
async def dlp_policy_new_page(request: Request):
    return render_policy_form(request, mode="new", values=default_policy_values())


@page_router.post("/dlp/policies")
async def dlp_policy_create_post(request: Request):
    form = await request.form()
    values = form_values(form, default_active=False)
    if not verify_csrf_token(request.app.state.session_secret, form.get("csrf_token"), request.cookies.get(CSRF_COOKIE)):
        return render_policy_form(request, mode="new", values=values, errors={"form": "요청이 만료되었습니다. 다시 시도해주세요."}, status_code=400)

    payload, errors = validate_policy_payload(values)
    if form.get("_action") == "preview":
        preview = evaluate_sample(str(values.get("sample_text") or ""), [payload]) if payload else None
        return render_policy_form(request, mode="new", values=values, errors=errors, preview=preview, status_code=400 if errors else 200)
    if errors:
        return render_policy_form(request, mode="new", values=values, errors=errors, status_code=400)

    async with request.app.state.db.acquire() as conn:
        try:
            policy = await create_policy(conn, payload, request.state.user["user_id"])
        except asyncpg.PostgresError as exc:
            raise HTTPException(status_code=500, detail="DLP 정책 저장에 실패했습니다.") from exc
    await write_audit(request, "dlp.policy.create", target_type="dlp_policy", target_id=str(policy["id"]), metadata={"after": encode(policy)})
    response = RedirectResponse(f"/admin/dlp/policies/{policy['id']}", status_code=302)
    response.delete_cookie(CSRF_COOKIE, path="/admin")
    return set_flash(response, request.app.state.session_secret, "DLP 정책이 생성되었습니다.")


@page_router.get("/dlp/policies/{policy_id:int}")
async def dlp_policy_edit_page(request: Request, policy_id: int):
    async with request.app.state.db.acquire() as conn:
        policy = await get_policy(conn, policy_id)
    if not policy:
        raise HTTPException(status_code=404, detail="DLP 정책을 찾을 수 없습니다.")
    values = dict(policy)
    values["sample_text"] = ""
    return render_policy_form(request, mode="edit", values=values, policy=policy)


@page_router.post("/dlp/policies/{policy_id:int}")
async def dlp_policy_update_post(request: Request, policy_id: int):
    form = await request.form()
    values = form_values(form, default_active=False)
    async with request.app.state.db.acquire() as conn:
        policy = await get_policy(conn, policy_id)
    if not policy:
        raise HTTPException(status_code=404, detail="DLP 정책을 찾을 수 없습니다.")
    if not verify_csrf_token(request.app.state.session_secret, form.get("csrf_token"), request.cookies.get(CSRF_COOKIE)):
        return render_policy_form(request, mode="edit", values=values, policy=policy, errors={"form": "요청이 만료되었습니다. 다시 시도해주세요."}, status_code=400)

    payload, errors = validate_policy_payload(values)
    if form.get("_action") == "preview":
        preview = evaluate_sample(str(values.get("sample_text") or ""), [payload]) if payload else None
        return render_policy_form(request, mode="edit", values=values, policy=policy, errors=errors, preview=preview, status_code=400 if errors else 200)
    if errors:
        return render_policy_form(request, mode="edit", values=values, policy=policy, errors=errors, status_code=400)

    async with request.app.state.db.acquire() as conn:
        updated, before = await update_policy(conn, policy_id, payload, request.state.user["user_id"])
    if not updated:
        raise HTTPException(status_code=404, detail="DLP 정책을 찾을 수 없습니다.")
    action = "dlp.policy.deactivate" if before.get("is_active") and not updated.get("is_active") else "dlp.policy.update"
    await write_audit(request, action, target_type="dlp_policy", target_id=str(policy_id), metadata={"before": encode(before), "after": encode(updated)})
    response = RedirectResponse(f"/admin/dlp/policies/{policy_id}", status_code=302)
    response.delete_cookie(CSRF_COOKIE, path="/admin")
    return set_flash(response, request.app.state.session_secret, "DLP 정책이 수정되었습니다.")


@page_router.post("/dlp/policies/{policy_id:int}/deactivate")
async def dlp_policy_deactivate_post(request: Request, policy_id: int):
    form = await request.form()
    if not verify_csrf_token(request.app.state.session_secret, form.get("csrf_token"), request.cookies.get(CSRF_COOKIE)):
        raise HTTPException(status_code=400, detail="요청이 만료되었습니다.")
    async with request.app.state.db.acquire() as conn:
        policy, before = await deactivate_policy(conn, policy_id, request.state.user["user_id"])
    if not policy:
        raise HTTPException(status_code=404, detail="DLP 정책을 찾을 수 없습니다.")
    await write_audit(request, "dlp.policy.deactivate", target_type="dlp_policy", target_id=str(policy_id), metadata={"before": encode(before), "after": encode(policy)})
    response = RedirectResponse("/admin/dlp/policies", status_code=302)
    return set_flash(response, request.app.state.session_secret, "DLP 정책이 비활성화되었습니다.")


@page_router.get("/dlp/test")
async def dlp_test_page(request: Request):
    async with request.app.state.db.acquire() as conn:
        policies = await active_policies(conn)
    return csrf_response(
        request,
        "admin/dlp_test.html",
        ctx(request, "dlp", active_tab="test", policies=policies, sample_text="", result=None, errors={}),
    )


@page_router.post("/dlp/test")
async def dlp_test_post(request: Request):
    form = await request.form()
    sample_text = str(form.get("sample_text") or "")
    if not verify_csrf_token(request.app.state.session_secret, form.get("csrf_token"), request.cookies.get(CSRF_COOKIE)):
        errors = {"form": "요청이 만료되었습니다. 다시 시도해주세요."}
    else:
        errors = {}
    async with request.app.state.db.acquire() as conn:
        policies = await active_policies(conn)
    result = evaluate_sample(sample_text, policies) if not errors else None
    return csrf_response(
        request,
        "admin/dlp_test.html",
        ctx(request, "dlp", active_tab="test", policies=policies, sample_text=sample_text, result=result, errors=errors),
        status_code=400 if errors else 200,
    )


@api_router.get("/dlp/policies")
async def api_list_dlp_policies(request: Request):
    async with request.app.state.db.acquire() as conn:
        return {"items": await list_policies(conn)}


@api_router.post("/dlp/policies")
async def api_create_dlp_policy(request: Request, payload: DlpPolicyInput):
    data, errors = validate_policy_payload(payload.model_dump())
    if errors:
        raise HTTPException(status_code=422, detail=errors)
    async with request.app.state.db.acquire() as conn:
        policy = await create_policy(conn, data, request.state.user["user_id"])
    await write_audit(request, "dlp.policy.create", target_type="dlp_policy", target_id=str(policy["id"]), metadata={"after": encode(policy)})
    return JSONResponse(status_code=201, content=encode(policy))


@api_router.get("/dlp/policies/{policy_id:int}")
async def api_get_dlp_policy(request: Request, policy_id: int):
    async with request.app.state.db.acquire() as conn:
        policy = await get_policy(conn, policy_id)
    if not policy:
        raise HTTPException(status_code=404, detail="DLP 정책을 찾을 수 없습니다.")
    return policy


@api_router.put("/dlp/policies/{policy_id:int}")
async def api_update_dlp_policy(request: Request, policy_id: int, payload: DlpPolicyInput):
    data, errors = validate_policy_payload(payload.model_dump())
    if errors:
        raise HTTPException(status_code=422, detail=errors)
    async with request.app.state.db.acquire() as conn:
        policy, before = await update_policy(conn, policy_id, data, request.state.user["user_id"])
    if not policy:
        raise HTTPException(status_code=404, detail="DLP 정책을 찾을 수 없습니다.")
    action = "dlp.policy.deactivate" if before.get("is_active") and not policy.get("is_active") else "dlp.policy.update"
    await write_audit(request, action, target_type="dlp_policy", target_id=str(policy_id), metadata={"before": encode(before), "after": encode(policy)})
    return encode(policy)


@api_router.post("/dlp/policies/{policy_id:int}/deactivate")
async def api_deactivate_dlp_policy(request: Request, policy_id: int):
    async with request.app.state.db.acquire() as conn:
        policy, before = await deactivate_policy(conn, policy_id, request.state.user["user_id"])
    if not policy:
        raise HTTPException(status_code=404, detail="DLP 정책을 찾을 수 없습니다.")
    await write_audit(request, "dlp.policy.deactivate", target_type="dlp_policy", target_id=str(policy_id), metadata={"before": encode(before), "after": encode(policy)})
    return encode(policy)


@api_router.post("/dlp/test")
async def api_test_dlp(request: Request):
    body = await request.json()
    sample_text = str(body.get("sample_text") or "")
    async with request.app.state.db.acquire() as conn:
        policies = await active_policies(conn)
    return evaluate_sample(sample_text, policies)
