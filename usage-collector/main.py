import os, re, time, json, hashlib, asyncio, logging, copy
from datetime import datetime, timezone
import asyncpg, httpx, boto3
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("usage-collector")

PORTKEY_URL = os.environ.get("PORTKEY_URL", "http://gateway:8787/v1/chat/completions")
AWS_REGION  = os.environ.get("AWS_REGION", "ap-northeast-2")
ANTHROPIC_BEDROCK_VERSION = "bedrock-2023-05-31"

BEDROCK_ANTHROPIC_ALLOWED_FIELDS = {
    "anthropic_version", "anthropic_beta",
    "messages", "system", "max_tokens",
    "metadata", "stop_sequences",
    "temperature", "top_p", "top_k",
    "tools", "tool_choice",
}

def sanitize_for_bedrock(body):
    """Bedrock-on-Anthropic이 받는 필드만 통과. Claude Code의 신규/beta 필드는 제거."""
    allowed = {}
    dropped = []
    for k, v in body.items():
        if k in BEDROCK_ANTHROPIC_ALLOWED_FIELDS:
            allowed[k] = v
        else:
            dropped.append(k)
    if dropped:
        log.info(f"sanitize: dropped {dropped}")
    return allowed


PRICING = {
    "global.anthropic.claude-opus-4-7":                {"input": 0.015, "output": 0.075},
    "global.anthropic.claude-haiku-4-5-20251001-v1:0": {"input": 0.001, "output": 0.005},
}

TEAM_MODEL_MAP = {
    "infra":           "global.anthropic.claude-opus-4-7",
    "credit-analysis": "global.anthropic.claude-opus-4-7",
    "default":         "global.anthropic.claude-opus-4-7",
}
FALLBACK_MODEL = "global.anthropic.claude-opus-4-7"

DLP_PATTERNS = [
    ("krn",     re.compile(r"\b\d{6}[-\s]?[1-8]\d{6}\b")),
    ("brn",     re.compile(r"\b\d{3}-\d{2}-\d{5}\b")),
    ("card",    re.compile(r"\b(?:\d{4}[- ]?){3}\d{4}\b")),
    ("account", re.compile(r"\b\d{3,6}-\d{2,6}-\d{4,8}\b")),
]

DLP_REDACTION_TOKENS = {
    "krn": "[REDACTED-KRN]",
    "card": "[REDACTED-CARD]",
    "brn": "[REDACTED-BRN]",
    "account": "[REDACTED-ACCOUNT]",
}

DLP_CACHE_TTL_SEC = 60
_dlp_cache_lock = asyncio.Lock()
_dlp_cache = {"policies": None, "loaded_at": 0.0}

def route_model(team_id):
    return TEAM_MODEL_MAP.get(team_id, FALLBACK_MODEL)

def calc_cost(model, in_tok, out_tok):
    p = PRICING.get(model)
    if not p:
        log.warning(f"no pricing for model: {model}")
        return 0.0
    return (in_tok/1000.0)*p["input"] + (out_tok/1000.0)*p["output"]

def extract_text_openai(messages):
    chunks = []
    for m in messages or []:
        c = m.get("content")
        if isinstance(c, str): chunks.append(c)
        elif isinstance(c, list):
            for block in c:
                if isinstance(block, dict) and block.get("type") == "text":
                    chunks.append(block.get("text", ""))
    return "\n".join(chunks)

def extract_text_anthropic(body):
    chunks = []
    sys_field = body.get("system")
    if isinstance(sys_field, str): chunks.append(sys_field)
    elif isinstance(sys_field, list):
        for block in sys_field:
            if isinstance(block, dict) and block.get("type") == "text":
                chunks.append(block.get("text", ""))
    for m in body.get("messages") or []:
        c = m.get("content")
        if isinstance(c, str): chunks.append(c)
        elif isinstance(c, list):
            for block in c:
                if not isinstance(block, dict): continue
                if block.get("type") == "text":
                    chunks.append(block.get("text", ""))
                elif block.get("type") == "tool_result":
                    tr = block.get("content")
                    if isinstance(tr, str): chunks.append(tr)
                    elif isinstance(tr, list):
                        for tb in tr:
                            if isinstance(tb, dict) and tb.get("type") == "text":
                                chunks.append(tb.get("text", ""))
    return "\n".join(chunks)

def iter_dlp_policies(policies=None):
    if policies is not None:
        for policy in policies:
            yield policy
        return
    for name, pat in DLP_PATTERNS:
        yield {
            "name": name,
            "pattern_type": name,
            "_compiled": pat,
            "redaction_token": DLP_REDACTION_TOKENS.get(name, f"[REDACTED-{name.upper()}]"),
            "action": "block_and_mask",
        }


async def get_active_dlp_policies():
    now = time.time()
    async with _dlp_cache_lock:
        if _dlp_cache["policies"] is not None and (now - _dlp_cache["loaded_at"]) <= DLP_CACHE_TTL_SEC:
            return _dlp_cache["policies"]
        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT id, name, pattern_type, pattern_regex, redaction_token, action, priority
                    FROM dlp_policies
                    WHERE is_active = TRUE
                    ORDER BY priority ASC, id ASC
                    """
                )
            policies = []
            for row in rows:
                item = dict(row)
                item["_compiled"] = re.compile(item["pattern_regex"])
                policies.append(item)
            _dlp_cache["policies"] = policies or list(iter_dlp_policies())
            _dlp_cache["loaded_at"] = now
            return _dlp_cache["policies"]
        except Exception as exc:
            log.warning(f"dlp policy load failed; using built-in policies: {exc}")
            _dlp_cache["policies"] = list(iter_dlp_policies())
            _dlp_cache["loaded_at"] = now
            return _dlp_cache["policies"]


def dlp_scan(text, policies=None):
    for policy in iter_dlp_policies(policies):
        if policy.get("_compiled").search(text or ""):
            return policy.get("pattern_type") or policy.get("name")
    return None

def dlp_check_strict(text, policies=None):
    for policy in iter_dlp_policies(policies):
        if policy.get("action") not in ("block", "block_and_mask"):
            continue
        if policy.get("_compiled").search(text or ""):
            return {"blocked": True, "pattern": policy.get("pattern_type") or policy.get("name")}
    return {"blocked": False, "pattern": None}

def _merge_applied(dst, names):
    for name in names or []:
        if name not in dst:
            dst.append(name)

def dlp_redact(text, policies=None):
    redacted = text or ""
    applied = []
    for policy in iter_dlp_policies(policies):
        if policy.get("action") not in ("mask", "block_and_mask"):
            continue
        name = policy.get("pattern_type") or policy.get("name")
        token = policy.get("redaction_token") or DLP_REDACTION_TOKENS.get(name, f"[REDACTED-{str(name).upper()}]")
        new_text, count = policy.get("_compiled").subn(token, redacted)
        if count:
            redacted = new_text
            _merge_applied(applied, [name])
    return {"redacted_text": redacted, "applied": applied}

def extract_text_from_value(value):
    chunks = []
    if isinstance(value, str):
        chunks.append(value)
    elif isinstance(value, list):
        for item in value:
            text = extract_text_from_value(item)
            if text:
                chunks.append(text)
    elif isinstance(value, dict):
        for item in value.values():
            text = extract_text_from_value(item)
            if text:
                chunks.append(text)
    return "\n".join(chunks)

def extract_text_from_message(message):
    if not isinstance(message, dict):
        return ""
    return extract_text_from_value(message.get("content"))

def dlp_prepare_current_user_message(message, policies=None):
    """Return strict-check text for the newest user input and redact older context
    that Claude Code may pack into the same user message.
    """
    if not isinstance(message, dict):
        return {"message": message, "current_text": "", "applied": []}

    redacted_message = copy.deepcopy(message)
    content = redacted_message.get("content")

    if isinstance(content, str):
        return {"message": redacted_message, "current_text": content, "applied": []}

    if not isinstance(content, list):
        return {
            "message": redacted_message,
            "current_text": extract_text_from_value(content),
            "applied": [],
        }

    current_idx = None
    for i in range(len(content) - 1, -1, -1):
        block = content[i]
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
            current_idx = i
            break
        if isinstance(block, str):
            current_idx = i
            break

    applied = []
    current_text = ""
    new_content = []
    for i, block in enumerate(content):
        if i == current_idx:
            if isinstance(block, dict):
                current_text = block.get("text", "")
            elif isinstance(block, str):
                current_text = block
            new_content.append(block)
            continue

        result = dlp_redact_recursive(block, policies=policies)
        new_content.append(result["redacted"])
        _merge_applied(applied, result["applied"])

    redacted_message["content"] = new_content
    return {"message": redacted_message, "current_text": current_text, "applied": applied}

def dlp_redact_recursive(value, policies=None):
    if isinstance(value, str):
        result = dlp_redact(value, policies=policies)
        return {"redacted": result["redacted_text"], "applied": result["applied"]}
    if isinstance(value, list):
        applied = []
        redacted_items = []
        for item in value:
            result = dlp_redact_recursive(item, policies=policies)
            redacted_items.append(result["redacted"])
            _merge_applied(applied, result["applied"])
        return {"redacted": redacted_items, "applied": applied}
    if isinstance(value, dict):
        applied = []
        redacted_dict = {}
        for key, item in value.items():
            result = dlp_redact_recursive(item, policies=policies)
            redacted_dict[key] = result["redacted"]
            _merge_applied(applied, result["applied"])
        return {"redacted": redacted_dict, "applied": applied}
    return {"redacted": value, "applied": []}

def dlp_redact_message(message, policies=None):
    if not isinstance(message, dict):
        return {"message": message, "applied": []}
    redacted_message = copy.deepcopy(message)
    result = dlp_redact_recursive(redacted_message.get("content"), policies=policies)
    redacted_message["content"] = result["redacted"]
    return {"message": redacted_message, "applied": result["applied"]}

def find_last_user_message_index(messages):
    if not isinstance(messages, list):
        return None
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if isinstance(msg, dict) and msg.get("role") == "user":
            return i
    return None

def hash_api_key(key):
    return hashlib.sha256(key.encode("utf-8")).hexdigest()

app = FastAPI()
pool = None
bedrock_runtime = None

@app.on_event("startup")
async def startup():
    global pool, bedrock_runtime
    pool = await asyncpg.create_pool(
        host=os.environ["DB_HOST"], port=int(os.environ["DB_PORT"]),
        database=os.environ["DB_NAME"], user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"], ssl="require", min_size=1, max_size=5,
    )
    bedrock_runtime = boto3.client("bedrock-runtime", region_name=AWS_REGION)
    log.info(f"DB pool ready, bedrock client ready (region={AWS_REGION})")

@app.on_event("shutdown")
async def shutdown():
    if pool: await pool.close()

@app.get("/health")
async def health():
    try:
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        return {"status": "ok"}
    except Exception as e:
        return JSONResponse(status_code=503, content={"status":"unhealthy","error":str(e)})

async def log_usage(user_id, team_id, skill, model, in_tok, out_tok, cost, req_id, blocked_reason, latency_ms):
    try:
        async with pool.acquire() as conn:
            await conn.execute("""
              INSERT INTO llm_usage(
                user_id, team_id, skill, model, provider,
                input_tokens, output_tokens, cost_usd,
                request_id, blocked_reason, latency_ms
              ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
            """, user_id, team_id, skill, model, "bedrock",
                  in_tok, out_tok, cost, req_id, blocked_reason, latency_ms)
    except Exception as e:
        log.error(f"DB insert failed: {e}")

async def log_audit_event(actor_role, action, target_type=None, target_id=None, metadata=None, ip_address=None):
    try:
        async with pool.acquire() as conn:
            await conn.execute("""
              INSERT INTO audit_log(
                actor_role, action, target_type, target_id, metadata, ip_address
              ) VALUES ($1,$2,$3,$4,$5::jsonb,$6::inet)
            """, actor_role, action, target_type, target_id,
                  json.dumps(metadata or {}, ensure_ascii=False), ip_address)
    except Exception as e:
        log.error(f"audit_log insert failed: {e}")

async def touch_api_key(key_hash):
    try:
        async with pool.acquire() as conn:
            await conn.execute("UPDATE api_keys SET last_used_at = now() WHERE key_hash = $1", key_hash)
    except Exception as exc:
        log.warning(f"api key last_used_at update failed: {exc}")


async def resolve_api_key(api_key):
    if not api_key:
        return {"ok": False, "reason": "missing"}
    h = hash_api_key(api_key)
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT k.key_hash,
                   k.user_id,
                   COALESCE(t.team_key, k.team_id) AS team_id,
                   k.expires_at,
                   k.revoked_at,
                   u.is_active,
                   u.archived_at
            FROM api_keys k
            LEFT JOIN web_users u ON lower(u.email) = lower(k.user_id)
            LEFT JOIN teams t ON t.id = u.team_id_fk
            WHERE k.key_hash = $1
            """,
            h,
        )
    if not row:
        return {"ok": False, "reason": "invalid"}
    now = datetime.now(timezone.utc)
    if row["revoked_at"]:
        return {"ok": False, "reason": "revoked"}
    if row["expires_at"] and row["expires_at"] < now:
        return {"ok": False, "reason": "expired"}
    if row["is_active"] is False or row["archived_at"]:
        return {"ok": False, "reason": "user_inactive"}
    asyncio.create_task(touch_api_key(row["key_hash"]))
    return {"ok": True, "user_id": row["user_id"], "team_id": row["team_id"], "key_hash": row["key_hash"]}

async def get_budget_status(team_id, user_id):
    async with pool.acquire() as conn:
        team_row = await conn.fetchrow("""
            WITH team_usage AS (
                SELECT
                    COALESCE(SUM(cost_usd) FILTER (WHERE ts >= date_trunc('month', now())), 0) AS month_used,
                    COALESCE(SUM(cost_usd) FILTER (WHERE ts >= date_trunc('day',   now())), 0) AS day_used
                FROM llm_usage WHERE team_id = $1 AND blocked_reason IS NULL
            )
            SELECT COALESCE(t.monthly_limit_usd, b.monthly_limit_usd)::float8 AS monthly_limit,
                   COALESCE(t.daily_limit_usd, b.daily_limit_usd)::float8 AS daily_limit,
                   COALESCE(t.alert_threshold_pct, 80)::int AS alert_threshold_pct,
                   u.month_used::float8 AS month_used, u.day_used::float8 AS day_used
            FROM team_usage u
            LEFT JOIN teams t ON t.team_key = $1 AND t.is_active = TRUE AND t.archived_at IS NULL
            LEFT JOIN team_budget b ON b.team_id = $1
        """, team_id)
        user_row = await conn.fetchrow("""
            WITH user_usage AS (
                SELECT COALESCE(SUM(cost_usd) FILTER (WHERE ts >= date_trunc('month', now())), 0) AS month_used
                FROM llm_usage WHERE user_id = $1 AND blocked_reason IS NULL
            )
            SELECT b.monthly_limit_usd::float8 AS monthly_limit, u.month_used::float8 AS month_used
            FROM user_budget b, user_usage u WHERE b.user_id = $1
        """, user_id)
    return {
        "team_monthly_limit": team_row["monthly_limit"] if team_row else None,
        "team_daily_limit":   team_row["daily_limit"]   if team_row else None,
        "team_alert_threshold_pct": team_row["alert_threshold_pct"] if team_row else 80,
        "team_monthly_used":  (team_row["month_used"] if team_row else 0.0),
        "team_daily_used":    (team_row["day_used"]   if team_row else 0.0),
        "user_monthly_limit": user_row["monthly_limit"] if user_row else None,
        "user_monthly_used":  (user_row["month_used"] if user_row else 0.0),
    }

def check_budget(status):
    if status["team_monthly_limit"] is None:
        return ("budget_no_team", "No budget defined for this team.", {})
    if status["team_monthly_used"] >= status["team_monthly_limit"]:
        return ("budget_exceeded_team_monthly",
                f"Monthly team budget exceeded: used=${status['team_monthly_used']:.4f} / limit=${status['team_monthly_limit']:.4f}", {})
    if status["team_daily_limit"] is not None and status["team_daily_used"] >= status["team_daily_limit"]:
        return ("budget_exceeded_team_daily",
                f"Daily team budget exceeded: used=${status['team_daily_used']:.4f} / limit=${status['team_daily_limit']:.4f}", {})
    if status["user_monthly_limit"] is not None and status["user_monthly_used"] >= status["user_monthly_limit"]:
        return ("budget_exceeded_user_monthly",
                f"Monthly user budget exceeded: used=${status['user_monthly_used']:.4f} / limit=${status['user_monthly_limit']:.4f}", {})
    headers = {"X-Budget-Remaining-Team-Monthly": f"{status['team_monthly_limit'] - status['team_monthly_used']:.4f}"}
    threshold = (status.get("team_alert_threshold_pct") or 80) / 100.0
    monthly_ratio = status["team_monthly_used"] / status["team_monthly_limit"] if status["team_monthly_limit"] else 0
    daily_ratio = status["team_daily_used"] / status["team_daily_limit"] if status["team_daily_limit"] else 0
    if monthly_ratio >= 0.95 or daily_ratio >= 0.95:
        headers["X-Budget-Alert"] = "critical"
    elif monthly_ratio >= threshold or daily_ratio >= threshold:
        headers["X-Budget-Alert"] = "warning"
    if status["team_daily_limit"] is not None:
        headers["X-Budget-Remaining-Team-Daily"] = f"{status['team_daily_limit'] - status['team_daily_used']:.4f}"
    if status["user_monthly_limit"] is not None:
        headers["X-Budget-Remaining-User-Monthly"] = f"{status['user_monthly_limit'] - status['user_monthly_used']:.4f}"
    return (None, None, headers)

# ============================================================
# /v1/chat/completions — OpenAI 호환 (변경 없음)
# ============================================================
@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    meta_raw = request.headers.get("x-user-metadata", "{}")
    try: meta = json.loads(meta_raw)
    except Exception: meta = {}
    user_id = meta.get("user_id", "unknown")
    team_id = meta.get("team_id", "default")
    skill   = meta.get("skill")
    requested_model = body.get("model")
    routed_model = route_model(team_id)
    body["model"] = routed_model
    if requested_model and requested_model != routed_model:
        log.info(f"routed(openai): team={team_id} {requested_model} -> {routed_model}")
    dlp_policies = await get_active_dlp_policies()
    text_to_scan = extract_text_openai(body.get("messages"))
    strict_result = dlp_check_strict(text_to_scan, policies=dlp_policies)
    if strict_result["blocked"]:
        dlp_hit = strict_result["pattern"]
        blocked_reason = f"dlp_{dlp_hit}"
        log.warning(f"dlp blocked(openai): user={user_id} team={team_id} reason={blocked_reason}")
        await log_usage(user_id, team_id, skill, routed_model, 0, 0, 0.0, None, blocked_reason, 0)
        return JSONResponse(status_code=403, content={"error":{"code":"dlp_blocked","type":"data_loss_prevention",
            "message":f"Request blocked by DLP policy ({dlp_hit}).","blocked_reason":blocked_reason}})
    budget_status = await get_budget_status(team_id, user_id)
    b_reason, b_msg, b_headers = check_budget(budget_status)
    if b_reason:
        log.warning(f"budget blocked(openai): user={user_id} team={team_id} reason={b_reason}")
        await log_usage(user_id, team_id, skill, routed_model, 0, 0, 0.0, None, b_reason, 0)
        return JSONResponse(status_code=429, content={"error":{"code":"budget_exceeded","type":"rate_limit_exceeded",
            "message":b_msg,"blocked_reason":b_reason}})
    fwd = {k: v for k, v in request.headers.items()
           if k.lower() in ("content-type", "authorization") or k.lower().startswith("x-portkey-")}
    started = time.time()
    blocked_reason = None; status_code = 502; result = {}
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(PORTKEY_URL, json=body, headers=fwd)
        status_code = r.status_code
        try: result = r.json()
        except Exception: result = {"raw": r.text}
    except Exception as e:
        log.exception("upstream call failed")
        blocked_reason = f"upstream_error: {str(e)[:180]}"
        result = {"error": {"message": blocked_reason}}
    latency_ms = int((time.time() - started) * 1000)
    model = body.get("model", "unknown")
    usage = result.get("usage", {}) if isinstance(result, dict) else {}
    in_tok = usage.get("prompt_tokens", 0); out_tok = usage.get("completion_tokens", 0)
    cost = calc_cost(model, in_tok, out_tok)
    req_id = result.get("id") if isinstance(result, dict) else None
    if isinstance(result, dict) and "error" in result and not blocked_reason:
        err = result["error"]
        msg = err.get("message", "") if isinstance(err, dict) else str(err)
        blocked_reason = f"upstream_error: {msg[:180]}"
    await log_usage(user_id, team_id, skill, model, in_tok, out_tok, cost, req_id, blocked_reason, latency_ms)
    return JSONResponse(content=result, status_code=status_code, headers=b_headers)

# ============================================================
# /v1/messages — Anthropic 호환 (streaming + non-streaming)
# ============================================================
@app.post("/v1/messages")
async def messages(request: Request):
    body = await request.json()

    # 인증
    api_key = request.headers.get("x-api-key", "").strip()
    if not api_key:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            api_key = auth[7:].strip()
    auth_result = await resolve_api_key(api_key)
    if not auth_result["ok"]:
        return JSONResponse(status_code=401, content={
            "type": "error",
            "error": {
                "type": "authentication_error",
                "message": f"Invalid or unavailable API key ({auth_result['reason']})",
                "reason": auth_result["reason"],
            },
        })
    user_id = auth_result["user_id"]
    team_id = auth_result["team_id"]
    skill = request.headers.get("x-skill")

    routed_model = route_model(team_id)
    log.info(f"messages: user={user_id} team={team_id} -> {routed_model}")

    # DLP Hybrid:
    # - newest user text block: strict block
    # - previous history/system and older blocks packed in the same user message: redact and continue
    messages_list = body.get("messages") or []
    dlp_policies = await get_active_dlp_policies()
    last_user_idx = find_last_user_message_index(messages_list)
    last_user_turn = messages_list[last_user_idx] if last_user_idx is not None else None
    history_messages = messages_list[:last_user_idx] if last_user_idx is not None else messages_list
    redaction_log = []
    prepared_last_user_turn = last_user_turn

    if last_user_turn is not None:
        prepared = dlp_prepare_current_user_message(last_user_turn, policies=dlp_policies)
        prepared_last_user_turn = prepared["message"]
        if prepared["applied"]:
            redaction_log.append({"target": "current_user_packed_history", "applied": prepared["applied"]})

        strict_result = dlp_check_strict(prepared["current_text"], policies=dlp_policies)
        if strict_result["blocked"]:
            dlp_hit = strict_result["pattern"]
            blocked_reason = f"dlp_{dlp_hit}"
            log.warning(f"DLP block(messages): user={user_id} team={team_id} pattern={dlp_hit} scope=current_user_input")
            await log_audit_event(
                "system",
                "dlp.block",
                target_type="user",
                target_id=user_id,
                metadata={
                    "pattern": dlp_hit,
                    "scope": "current_user_input",
                    "team_id": team_id,
                    "model": routed_model,
                },
                ip_address=request.client.host if request.client else None,
            )
            await log_usage(user_id, team_id, skill, routed_model, 0, 0, 0.0, None, blocked_reason, 0)
            return JSONResponse(status_code=403, content={
                "type": "error",
                "error": {
                    "type": "dlp_blocked",
                    "message": f"민감정보 패턴({dlp_hit})이 감지되어 요청이 차단되었습니다. 해당 정보를 제거하고 다시 시도해주세요.",
                    "matched_pattern": dlp_hit,
                    "scope": "current_user_input",
                }
            })

    if body.get("system") is not None:
        sys_result = dlp_redact_recursive(body.get("system"), policies=dlp_policies)
        if sys_result["applied"]:
            body["system"] = sys_result["redacted"]
            redaction_log.append({"target": "system", "applied": sys_result["applied"]})

    redacted_history = []
    for msg in history_messages:
        msg_result = dlp_redact_message(msg, policies=dlp_policies)
        redacted_history.append(msg_result["message"])
        if msg_result["applied"]:
            role = msg.get("role", "unknown") if isinstance(msg, dict) else "unknown"
            redaction_log.append({"target": f"history[{role}]", "applied": msg_result["applied"]})

    body["messages"] = redacted_history + ([prepared_last_user_turn] if prepared_last_user_turn is not None else [])

    if redaction_log:
        log.info(f"DLP redact(messages): user={user_id} entries={len(redaction_log)} details={redaction_log}")
        redacted_patterns = []
        for entry in redaction_log:
            _merge_applied(redacted_patterns, entry.get("applied"))
        await log_audit_event(
            "system",
            "dlp.mask",
            target_type="user",
            target_id=user_id,
            metadata={
                "patterns": redacted_patterns,
                "scope": "history_or_tool_context",
                "team_id": team_id,
                "model": routed_model,
            },
            ip_address=request.client.host if request.client else None,
        )

    # Budget
    budget_status = await get_budget_status(team_id, user_id)
    b_reason, b_msg, b_headers = check_budget(budget_status)
    if b_reason:
        log.warning(f"budget blocked(messages): user={user_id} team={team_id} reason={b_reason}")
        await log_usage(user_id, team_id, skill, routed_model, 0, 0, 0.0, None, b_reason, 0)
        return JSONResponse(status_code=429, content={
            "type": "error", "error": {"type": "rate_limit_exceeded", "message": b_msg}
        })

    # Bedrock 호출 준비 — allowlist sanitize (Claude Code 신규 필드 제거)
    is_streaming = bool(body.get("stream", False))
    bedrock_body = sanitize_for_bedrock(body)
    bedrock_body["anthropic_version"] = ANTHROPIC_BEDROCK_VERSION

    started = time.time()

    # -------- Streaming path (SSE) --------
    if is_streaming:
        async def event_stream():
            captured = {"input_tokens": 0, "output_tokens": 0,
                        "request_id": None, "blocked_reason": None}
            try:
                resp = await asyncio.to_thread(
                    bedrock_runtime.invoke_model_with_response_stream,
                    modelId=routed_model,
                    body=json.dumps(bedrock_body),
                )
                stream = resp["body"]
                it = iter(stream)
                while True:
                    event = await asyncio.to_thread(lambda: next(it, None))
                    if event is None:
                        break
                    if "chunk" not in event:
                        continue
                    chunk_bytes = event["chunk"]["bytes"]
                    chunk_text = chunk_bytes.decode("utf-8")
                    try: chunk_json = json.loads(chunk_text)
                    except Exception: chunk_json = {}
                    etype = chunk_json.get("type", "message")
                    if etype == "message_start":
                        msg = chunk_json.get("message", {})
                        captured["request_id"] = msg.get("id")
                        u = msg.get("usage", {}) or {}
                        captured["input_tokens"] = u.get("input_tokens", 0)
                        captured["output_tokens"] = u.get("output_tokens", 0)
                    elif etype == "message_delta":
                        u = chunk_json.get("usage", {}) or {}
                        if "output_tokens" in u:
                            captured["output_tokens"] = u["output_tokens"]
                    yield f"event: {etype}\ndata: {chunk_text}\n\n".encode("utf-8")
            except Exception as e:
                log.exception("bedrock stream failed")
                captured["blocked_reason"] = f"upstream_error: {str(e)[:180]}"
                err_payload = json.dumps({"type": "error",
                                          "error": {"type": "api_error", "message": str(e)[:200]}})
                yield f"event: error\ndata: {err_payload}\n\n".encode("utf-8")
            finally:
                latency_ms = int((time.time() - started) * 1000)
                cost = calc_cost(routed_model, captured["input_tokens"], captured["output_tokens"])
                await log_usage(user_id, team_id, skill, routed_model,
                                captured["input_tokens"], captured["output_tokens"],
                                cost, captured["request_id"], captured["blocked_reason"], latency_ms)

        return StreamingResponse(event_stream(), media_type="text/event-stream", headers=b_headers)

    # -------- Non-streaming path --------
    blocked_reason = None; status_code = 200; result = {}
    try:
        resp = await asyncio.to_thread(
            bedrock_runtime.invoke_model,
            modelId=routed_model, body=json.dumps(bedrock_body),
        )
        result = json.loads(resp["body"].read())
    except Exception as e:
        log.exception("bedrock invoke failed")
        blocked_reason = f"upstream_error: {str(e)[:180]}"
        status_code = 502
        result = {"type": "error", "error": {"type": "api_error", "message": blocked_reason}}
    latency_ms = int((time.time() - started) * 1000)
    usage = result.get("usage", {}) if isinstance(result, dict) else {}
    in_tok = usage.get("input_tokens", 0); out_tok = usage.get("output_tokens", 0)
    cost = calc_cost(routed_model, in_tok, out_tok)
    req_id = result.get("id") if isinstance(result, dict) else None
    await log_usage(user_id, team_id, skill, routed_model, in_tok, out_tok, cost, req_id, blocked_reason, latency_ms)
    return JSONResponse(content=result, status_code=status_code, headers=b_headers)
