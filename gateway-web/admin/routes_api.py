import csv
import io
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from admin import queries

router = APIRouter(prefix="/api/admin")


def parse_dt(value):
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def range_from_request(request: Request):
    days = int(request.query_params.get("days", "7"))
    start = parse_dt(request.query_params.get("start"))
    end = parse_dt(request.query_params.get("end"))
    if not start or not end:
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days)
    return start, end


@router.get("/health")
async def health(request: Request):
    async with request.app.state.db.acquire() as conn:
        await conn.fetchval("SELECT 1")
    collector = "unknown"
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(f"{request.app.state.collector_url}/health")
        collector = "ok" if resp.status_code < 500 else f"http_{resp.status_code}"
    except Exception as exc:
        collector = str(exc)
    return {"db": "ok", "usage_collector": collector}


@router.get("/usage")
async def usage(request: Request):
    start, end = range_from_request(request)
    async with request.app.state.db.acquire() as conn:
        data = await queries.usage_summary(
            conn,
            start,
            end,
            team=request.query_params.get("team") or None,
            user=request.query_params.get("user") or None,
            model=request.query_params.get("model") or None,
        )
    return JSONResponse(data)


@router.get("/usage.csv")
async def usage_csv(request: Request):
    start, end = range_from_request(request)
    async with request.app.state.db.acquire() as conn:
        rows = await queries.usage_csv_rows(
            conn,
            start,
            end,
            team=request.query_params.get("team") or None,
            user=request.query_params.get("user") or None,
            model=request.query_params.get("model") or None,
        )
    buf = io.StringIO()
    writer = csv.DictWriter(
        buf,
        fieldnames=["ts", "user_id", "team_id", "model", "input_tokens", "output_tokens", "cost_usd", "latency_ms", "blocked_reason"],
    )
    writer.writeheader()
    writer.writerows(rows)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=llm-usage.csv"},
    )


@router.get("/keys")
async def keys(request: Request):
    async with request.app.state.db.acquire() as conn:
        return {"items": await queries.key_rows(conn)}


@router.get("/dlp")
async def dlp(request: Request):
    since = parse_dt(request.query_params.get("since"))
    async with request.app.state.db.acquire() as conn:
        return {"items": await queries.dlp_rows(conn, since=since)}


@router.get("/audit")
async def audit(request: Request):
    since = parse_dt(request.query_params.get("since"))
    async with request.app.state.db.acquire() as conn:
        return {
            "items": await queries.audit_rows(
                conn,
                action=request.query_params.get("action") or None,
                since=since,
            )
        }
