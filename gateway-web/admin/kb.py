import json
import logging
import os
import re
from decimal import Decimal
from pathlib import PurePosixPath

import asyncpg
import boto3
from fastapi import APIRouter, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from admin.queries import encode, records
from auth.csrf import CSRF_COOKIE, create_csrf_token, verify_csrf_token
from common.audit import write_audit
from common.flash import set_flash
from common.kb_processing import (
    chunk_text,
    estimate_embedding_cost,
    extract_text,
    extension_for_filename,
    is_allowed_document,
    normalize_mime,
    sha256_bytes,
    vector_literal,
)


page_router = APIRouter(prefix="/admin")
api_router = APIRouter(prefix="/api/admin")
log = logging.getLogger(__name__)

KB_BUCKET = os.environ.get("KB_BUCKET", "kcs-llm-gateway-kb-prod")
AWS_REGION = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "ap-northeast-2"
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
MAX_UPLOAD_FILES = 10
MAX_UPLOAD_CHUNKS = 5000


class TestRetrieveInput(BaseModel):
    query: str
    top_k: int | None = None


def ctx(request: Request, page: str, **kwargs):
    base = {"request": request, "page": page, "user": request.state.user}
    base.update(kwargs)
    return base


def csrf_response(request: Request, template: str, context: dict, status_code: int = 200):
    token = create_csrf_token(request.app.state.session_secret)
    context["csrf_token"] = token
    response = request.app.state.templates.TemplateResponse(template, context, status_code=status_code)
    response.set_cookie(CSRF_COOKIE, token, httponly=True, secure=True, samesite="strict", max_age=3600, path="/admin")
    return response


def _s3_put_object(bucket: str, key: str, data: bytes, content_type: str) -> None:
    boto3.client("s3", region_name=AWS_REGION).put_object(
        Bucket=bucket,
        Key=key,
        Body=data,
        ContentType=content_type,
        ServerSideEncryption="AES256",
    )


async def put_kb_object(key: str, data: bytes, content_type: str) -> None:
    await run_in_threadpool(_s3_put_object, KB_BUCKET, key, data, content_type)


def _s3_delete_object(bucket: str, key: str) -> None:
    boto3.client("s3", region_name=AWS_REGION).delete_object(Bucket=bucket, Key=key)


async def delete_kb_object(key: str) -> None:
    await run_in_threadpool(_s3_delete_object, KB_BUCKET, key)


def _invoke_titan_embedding(text: str, model_id: str) -> dict:
    client = boto3.client("bedrock-runtime", region_name=AWS_REGION)
    response = client.invoke_model(
        modelId=model_id,
        contentType="application/json",
        accept="application/json",
        body=json.dumps({"inputText": text, "dimensions": 1024, "normalize": True}, ensure_ascii=False).encode("utf-8"),
    )
    return json.loads(response["body"].read())


async def embed_text(text: str, model_id: str) -> tuple[list[float], int, Decimal]:
    body = await run_in_threadpool(_invoke_titan_embedding, text, model_id)
    vector = body.get("embedding") or []
    if len(vector) != 1024:
        raise RuntimeError(f"Titan embedding dimension mismatch: {len(vector)}")
    tokens = int(body.get("inputTextTokenCount") or max(1, len(text) // 4))
    return [float(item) for item in vector], tokens, Decimal(str(estimate_embedding_cost(tokens)))


async def active_blocking_dlp_policies(conn):
    rows = await conn.fetch(
        """
        SELECT id, name, pattern_type, pattern_regex, action
        FROM dlp_policies
        WHERE is_active = TRUE
          AND action IN ('block', 'block_and_mask')
        ORDER BY priority ASC, id ASC
        """
    )
    policies = []
    for row in rows:
        item = dict(row)
        item["_compiled"] = re.compile(item["pattern_regex"])
        policies.append(item)
    return policies


def find_dlp_violation(text: str, policies: list[dict]) -> dict | None:
    for policy in policies:
        if policy["_compiled"].search(text or ""):
            return {"pattern_type": policy["pattern_type"], "name": policy["name"], "policy_id": policy["id"]}
    return None


async def kb_rows(conn):
    rows = await conn.fetch(
        """
        SELECT t.id AS team_id,
               t.team_key,
               t.name AS team_name,
               kb.id AS kb_id,
               kb.name,
               kb.embedding_model,
               kb.chunk_size,
               kb.chunk_overlap,
               kb.top_k_default,
               kb.s3_prefix,
               kb.status,
               kb.is_active,
               kb.last_ingested_at,
               count(DISTINCT d.id) FILTER (WHERE d.removed_at IS NULL)::int AS document_count,
               COALESCE(sum(d.chunk_count) FILTER (WHERE d.removed_at IS NULL), 0)::int AS chunk_count,
               COALESCE(sum(d.embedding_token_cost_usd) FILTER (WHERE d.removed_at IS NULL), 0)::float8 AS embedding_cost_usd
        FROM teams t
        LEFT JOIN knowledge_bases kb ON kb.team_id = t.id
        LEFT JOIN kb_documents d ON d.kb_id = kb.id
        WHERE t.archived_at IS NULL
        GROUP BY t.id, kb.id
        ORDER BY t.name, t.team_key
        """
    )
    return records(rows)


async def get_kb(conn, kb_id: int):
    row = await conn.fetchrow(
        """
        SELECT kb.*,
               t.team_key,
               t.name AS team_name,
               count(DISTINCT d.id) FILTER (WHERE d.removed_at IS NULL)::int AS document_count,
               COALESCE(sum(d.chunk_count) FILTER (WHERE d.removed_at IS NULL), 0)::int AS chunk_count,
               COALESCE(sum(d.embedding_token_cost_usd) FILTER (WHERE d.removed_at IS NULL), 0)::float8 AS embedding_cost_usd
        FROM knowledge_bases kb
        JOIN teams t ON t.id = kb.team_id
        LEFT JOIN kb_documents d ON d.kb_id = kb.id
        WHERE kb.id = $1
        GROUP BY kb.id, t.id
        """,
        kb_id,
    )
    return encode(dict(row)) if row else None


async def kb_documents(conn, kb_id: int):
    rows = await conn.fetch(
        """
        SELECT d.id,
               d.kb_id,
               d.team_id,
               d.s3_key,
               d.mime,
               d.sha256,
               d.size_bytes,
               d.title,
               d.tags,
               d.uploaded_at,
               d.ingestion_status,
               d.ingestion_error,
               d.chunk_count,
               d.embedding_token_cost_usd::float8 AS embedding_token_cost_usd,
               d.ingested_at,
               d.removed_at,
               wu.email AS uploaded_by_email,
               wu.display_name AS uploaded_by_name
        FROM kb_documents d
        LEFT JOIN web_users wu ON wu.id = d.uploaded_by_user_id
        WHERE d.kb_id = $1
        ORDER BY d.uploaded_at DESC, d.id DESC
        """
        ,
        kb_id,
    )
    return records(rows)


async def create_kb_for_team(conn, team_id: int):
    team = await conn.fetchrow(
        "SELECT id, team_key, name FROM teams WHERE id = $1 AND archived_at IS NULL",
        team_id,
    )
    if not team:
        return None
    row = await conn.fetchrow(
        """
        INSERT INTO knowledge_bases (team_id, name, s3_prefix)
        VALUES ($1, $2, $3)
        ON CONFLICT (team_id) DO UPDATE
        SET is_active = TRUE,
            status = CASE WHEN knowledge_bases.status = 'archived' THEN 'active' ELSE knowledge_bases.status END,
            updated_at = now()
        RETURNING *
        """,
        team["id"],
        team["name"],
        f"team/{team['team_key']}/",
    )
    return encode(dict(row))


def title_for_upload(file: UploadFile) -> str:
    return PurePosixPath(file.filename or "document").name


async def prepare_upload(conn, kb: dict, file: UploadFile, policies: list[dict]) -> tuple[dict | None, str | None]:
    filename = title_for_upload(file)
    raw = await file.read()
    if not raw:
        return None, f"{filename}: 빈 파일입니다."
    if len(raw) > MAX_UPLOAD_BYTES:
        return None, f"{filename}: 50MB 이하 파일만 업로드할 수 있습니다."
    if not is_allowed_document(filename, file.content_type):
        return None, f"{filename}: 지원하지 않는 파일 형식입니다."
    try:
        text = extract_text(filename, file.content_type, raw)
    except ValueError as exc:
        return None, f"{filename}: {exc}"
    if not text.strip():
        return None, f"{filename}: 추출 가능한 텍스트가 없습니다."
    violation = find_dlp_violation(text, policies)
    if violation:
        return None, f"{filename}: DLP 정책({violation['pattern_type']})에 의해 차단되었습니다."

    sha256 = sha256_bytes(raw)
    duplicate = await conn.fetchval(
        "SELECT id FROM kb_documents WHERE kb_id = $1 AND sha256 = $2 AND removed_at IS NULL",
        kb["id"],
        sha256,
    )
    if duplicate:
        return None, f"{filename}: 이미 업로드된 문서입니다."

    chunks = chunk_text(text, kb.get("chunk_size") or 800, kb.get("chunk_overlap") or 100)
    if not chunks:
        return None, f"{filename}: 생성된 청크가 없습니다."
    if len(chunks) > MAX_UPLOAD_CHUNKS:
        return None, f"{filename}: 청크가 너무 많습니다. 더 작은 문서로 나눠주세요."

    return {
        "filename": filename,
        "raw": raw,
        "text": text,
        "sha256": sha256,
        "mime": normalize_mime(filename, file.content_type),
        "extension": extension_for_filename(filename),
        "size_bytes": len(raw),
        "chunks": chunks,
    }, None


async def create_document_row(conn, kb: dict, upload: dict, actor_id: int):
    document_id = await conn.fetchval("SELECT nextval(pg_get_serial_sequence('kb_documents', 'id'))")
    s3_key = f"{kb['s3_prefix']}{document_id}{upload['extension']}"
    await put_kb_object(s3_key, upload["raw"], upload["mime"])
    row = await conn.fetchrow(
        """
        INSERT INTO kb_documents (
          id, kb_id, team_id, s3_key, mime, sha256, size_bytes, title,
          uploaded_by_user_id, ingestion_status
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, 'pending')
        RETURNING *
        """,
        document_id,
        kb["id"],
        kb["team_id"],
        s3_key,
        upload["mime"],
        upload["sha256"],
        upload["size_bytes"],
        upload["filename"],
        actor_id,
    )
    return encode(dict(row))


async def ingest_upload(conn, kb: dict, document: dict, upload: dict):
    await conn.execute(
        "UPDATE kb_documents SET ingestion_status = 'running', ingestion_error = NULL WHERE id = $1",
        document["id"],
    )
    await conn.execute("DELETE FROM kb_chunks WHERE document_id = $1", document["id"])
    total_cost = Decimal("0")
    try:
        for chunk in upload["chunks"]:
            vector, input_tokens, cost = await embed_text(chunk.content, kb["embedding_model"])
            total_cost += cost
            await conn.execute(
                """
                INSERT INTO kb_chunks (
                  document_id, kb_id, team_id, chunk_index, content, token_count, embedding
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7::vector)
                """,
                document["id"],
                kb["id"],
                kb["team_id"],
                chunk.index,
                chunk.content,
                input_tokens or chunk.token_count,
                vector_literal(vector),
            )
        await conn.execute(
            """
            UPDATE kb_documents
            SET ingestion_status = 'succeeded',
                ingestion_error = NULL,
                chunk_count = $2,
                embedding_token_cost_usd = $3,
                ingested_at = now()
            WHERE id = $1
            """,
            document["id"],
            len(upload["chunks"]),
            total_cost,
        )
        await conn.execute(
            "UPDATE knowledge_bases SET last_ingested_at = now(), updated_at = now(), status = 'active' WHERE id = $1",
            kb["id"],
        )
    except Exception as exc:
        log.exception("KB ingestion failed: document_id=%s", document["id"])
        await conn.execute(
            """
            UPDATE kb_documents
            SET ingestion_status = 'failed',
                ingestion_error = $2,
                ingested_at = now()
            WHERE id = $1
            """,
            document["id"],
            str(exc)[:2000],
        )
        raise


async def retrieve_chunks(conn, kb: dict, query: str, top_k: int | None = None):
    vector, _, _ = await embed_text(query, kb["embedding_model"])
    rows = await conn.fetch(
        """
        SELECT c.id,
               c.content,
               c.document_id,
               d.title,
               d.s3_key,
               1 - (c.embedding <=> $1::vector) AS score
        FROM kb_chunks c
        JOIN kb_documents d ON d.id = c.document_id
        WHERE c.kb_id = $2
          AND c.team_id = $3
          AND d.ingestion_status = 'succeeded'
          AND d.removed_at IS NULL
        ORDER BY c.embedding <=> $1::vector
        LIMIT $4
        """,
        vector_literal(vector),
        kb["id"],
        kb["team_id"],
        min(max(int(top_k or kb.get("top_k_default") or 5), 1), 20),
    )
    return records(rows)


@page_router.get("/kb")
async def kb_page(request: Request):
    async with request.app.state.db.acquire() as conn:
        rows = await kb_rows(conn)
    return csrf_response(request, "admin/kb.html", ctx(request, "kb", rows=rows))


@page_router.post("/kb/teams/{team_id:int}/create")
async def kb_create_post(request: Request, team_id: int):
    form = await request.form()
    if not verify_csrf_token(request.app.state.session_secret, form.get("csrf_token"), request.cookies.get(CSRF_COOKIE)):
        raise HTTPException(status_code=400, detail="요청이 만료되었습니다")
    async with request.app.state.db.acquire() as conn:
        kb = await create_kb_for_team(conn, team_id)
    if not kb:
        raise HTTPException(status_code=404, detail="팀을 찾을 수 없습니다")
    await write_audit(request, "kb.create", target_type="knowledge_base", target_id=str(kb["id"]), metadata={"kb": kb})
    response = RedirectResponse(f"/admin/kb/{kb['id']}", status_code=302)
    return set_flash(response, request.app.state.session_secret, "팀 KB가 활성화되었습니다.")


@page_router.get("/kb/{kb_id:int}")
async def kb_detail_page(request: Request, kb_id: int):
    async with request.app.state.db.acquire() as conn:
        kb = await get_kb(conn, kb_id)
        if not kb:
            raise HTTPException(status_code=404, detail="KB를 찾을 수 없습니다")
        documents = await kb_documents(conn, kb_id)
    return csrf_response(request, "admin/kb_detail.html", ctx(request, "kb", kb=kb, documents=documents, errors={}, retrieve=None))


@page_router.post("/kb/{kb_id:int}/documents")
async def kb_upload_documents_post(request: Request, kb_id: int):
    form = await request.form()
    if not verify_csrf_token(request.app.state.session_secret, form.get("csrf_token"), request.cookies.get(CSRF_COOKIE)):
        raise HTTPException(status_code=400, detail="요청이 만료되었습니다")
    files = [item for item in form.getlist("documents") if hasattr(item, "filename") and hasattr(item, "read")]
    errors = []
    uploaded = []
    if not files:
        errors.append("업로드할 문서를 선택해주세요.")
    if len(files) > MAX_UPLOAD_FILES:
        errors.append("한 번에 최대 10개 문서만 업로드할 수 있습니다.")

    async with request.app.state.db.acquire() as conn:
        kb = await get_kb(conn, kb_id)
        if not kb:
            raise HTTPException(status_code=404, detail="KB를 찾을 수 없습니다")
        policies = await active_blocking_dlp_policies(conn)
        if not errors:
            for file in files:
                upload, error = await prepare_upload(conn, kb, file, policies)
                if error:
                    errors.append(error)
                    continue
                try:
                    document = await create_document_row(conn, kb, upload, request.state.user["user_id"])
                    await write_audit(request, "kb.document.upload", target_type="kb_document", target_id=str(document["id"]), metadata={"document": document})
                    await write_audit(request, "kb.ingestion.start", target_type="kb_document", target_id=str(document["id"]), metadata={"kb_id": kb_id})
                    await ingest_upload(conn, kb, document, upload)
                    await write_audit(request, "kb.ingestion.complete", target_type="kb_document", target_id=str(document["id"]), metadata={"status": "succeeded", "chunks": len(upload["chunks"])})
                    uploaded.append(document["title"])
                except asyncpg.UniqueViolationError:
                    errors.append(f"{upload['filename']}: 이미 업로드된 문서입니다.")
                except Exception as exc:
                    errors.append(f"{upload['filename']}: ingestion 실패 - {exc}")
        documents = await kb_documents(conn, kb_id)

    if errors:
        return csrf_response(request, "admin/kb_detail.html", ctx(request, "kb", kb=kb, documents=documents, errors={"upload": errors}, retrieve=None), status_code=400)
    response = RedirectResponse(f"/admin/kb/{kb_id}", status_code=302)
    return set_flash(response, request.app.state.session_secret, f"{len(uploaded)}개 문서가 업로드/인덱싱되었습니다.")


@page_router.post("/kb/{kb_id:int}/documents/{document_id:int}/remove")
async def kb_remove_document_post(request: Request, kb_id: int, document_id: int):
    form = await request.form()
    if not verify_csrf_token(request.app.state.session_secret, form.get("csrf_token"), request.cookies.get(CSRF_COOKIE)):
        raise HTTPException(status_code=400, detail="요청이 만료되었습니다")
    async with request.app.state.db.acquire() as conn:
        doc = await conn.fetchrow("SELECT * FROM kb_documents WHERE id = $1 AND kb_id = $2", document_id, kb_id)
        if not doc:
            raise HTTPException(status_code=404, detail="문서를 찾을 수 없습니다")
        await delete_kb_object(doc["s3_key"])
        await conn.execute("DELETE FROM kb_chunks WHERE document_id = $1", document_id)
        await conn.execute(
            "UPDATE kb_documents SET ingestion_status = 'removed', removed_at = now() WHERE id = $1",
            document_id,
        )
    await write_audit(request, "kb.document.remove", target_type="kb_document", target_id=str(document_id), metadata={"kb_id": kb_id})
    response = RedirectResponse(f"/admin/kb/{kb_id}", status_code=302)
    return set_flash(response, request.app.state.session_secret, "문서가 제거되었습니다.")


@page_router.post("/kb/{kb_id:int}/test-retrieve")
async def kb_test_retrieve_post(request: Request, kb_id: int):
    form = await request.form()
    if not verify_csrf_token(request.app.state.session_secret, form.get("csrf_token"), request.cookies.get(CSRF_COOKIE)):
        raise HTTPException(status_code=400, detail="요청이 만료되었습니다")
    query = str(form.get("query") or "").strip()
    if not query:
        raise HTTPException(status_code=422, detail="query는 필수입니다")
    async with request.app.state.db.acquire() as conn:
        kb = await get_kb(conn, kb_id)
        if not kb:
            raise HTTPException(status_code=404, detail="KB를 찾을 수 없습니다")
        documents = await kb_documents(conn, kb_id)
        chunks = await retrieve_chunks(conn, kb, query, int(form.get("top_k") or kb.get("top_k_default") or 5))
    return csrf_response(request, "admin/kb_detail.html", ctx(request, "kb", kb=kb, documents=documents, errors={}, retrieve={"query": query, "chunks": chunks}))


@api_router.get("/kb")
async def api_kb_rows(request: Request):
    async with request.app.state.db.acquire() as conn:
        return {"items": await kb_rows(conn)}


@api_router.post("/kb/teams/{team_id}/create")
async def api_create_kb(request: Request, team_id: int):
    async with request.app.state.db.acquire() as conn:
        kb = await create_kb_for_team(conn, team_id)
    if not kb:
        raise HTTPException(status_code=404, detail="팀을 찾을 수 없습니다")
    await write_audit(request, "kb.create", target_type="knowledge_base", target_id=str(kb["id"]), metadata={"kb": kb})
    return JSONResponse(status_code=201, content=kb)


@api_router.post("/kb/{kb_id}/test-retrieve")
async def api_test_retrieve(request: Request, kb_id: int, payload: TestRetrieveInput):
    if not payload.query.strip():
        raise HTTPException(status_code=422, detail="query는 필수입니다")
    async with request.app.state.db.acquire() as conn:
        kb = await get_kb(conn, kb_id)
        if not kb:
            raise HTTPException(status_code=404, detail="KB를 찾을 수 없습니다")
        return {"items": await retrieve_chunks(conn, kb, payload.query, payload.top_k)}


__all__ = [
    "page_router",
    "api_router",
    "get_kb",
    "kb_documents",
    "create_kb_for_team",
    "retrieve_chunks",
    "prepare_upload",
]
