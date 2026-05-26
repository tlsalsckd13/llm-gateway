from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass
from io import BytesIO


ALLOWED_EXTENSIONS = {".md", ".txt", ".html", ".htm", ".pdf", ".docx"}
ALLOWED_MIME_TYPES = {
    "text/markdown",
    "text/plain",
    "text/html",
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
TITAN_EMBEDDING_PRICE_PER_1K_TOKENS = 0.00002


@dataclass(frozen=True)
class TextChunk:
    index: int
    content: str
    token_count: int


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4) if text else 0


def estimate_embedding_cost(input_tokens: int) -> float:
    return (input_tokens / 1000.0) * TITAN_EMBEDDING_PRICE_PER_1K_TOKENS


def chunk_text(text: str, chunk_size_tokens: int = 800, overlap_tokens: int = 100) -> list[TextChunk]:
    if not text:
        return []
    chars_per_token = 4
    chunk_chars = max(1, chunk_size_tokens * chars_per_token)
    overlap_chars = max(0, min(overlap_tokens * chars_per_token, chunk_chars - 1))
    step = chunk_chars - overlap_chars
    chunks: list[TextChunk] = []
    for index, start in enumerate(range(0, len(text), step)):
        content = text[start:start + chunk_chars].strip()
        if content:
            chunks.append(TextChunk(index=index, content=content, token_count=estimate_tokens(content)))
        if start + chunk_chars >= len(text):
            break
    return chunks


def vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(f"{value:.10g}" for value in vector) + "]"


def extension_for_filename(filename: str) -> str:
    lower = (filename or "").lower()
    for extension in sorted(ALLOWED_EXTENSIONS, key=len, reverse=True):
        if lower.endswith(extension):
            return extension
    return ""


def normalize_mime(filename: str, content_type: str | None) -> str:
    content_type = (content_type or "").split(";", 1)[0].strip().lower()
    extension = extension_for_filename(filename)
    if content_type in ALLOWED_MIME_TYPES:
        return content_type
    return {
        ".md": "text/markdown",
        ".txt": "text/plain",
        ".html": "text/html",
        ".htm": "text/html",
        ".pdf": "application/pdf",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }.get(extension, content_type or "application/octet-stream")


def is_allowed_document(filename: str, content_type: str | None) -> bool:
    mime = normalize_mime(filename, content_type)
    return extension_for_filename(filename) in ALLOWED_EXTENSIONS and mime in ALLOWED_MIME_TYPES


def extract_text(filename: str, content_type: str | None, data: bytes) -> str:
    mime = normalize_mime(filename, content_type)
    extension = extension_for_filename(filename)
    if mime in ("text/plain", "text/markdown") or extension in (".md", ".txt"):
        return data.decode("utf-8", errors="replace")
    if mime == "text/html" or extension in (".html", ".htm"):
        return _extract_html_text(data)
    if mime == "application/pdf" or extension == ".pdf":
        return _extract_pdf_text(data)
    if mime == "application/vnd.openxmlformats-officedocument.wordprocessingml.document" or extension == ".docx":
        return _extract_docx_text(data)
    raise ValueError("지원하지 않는 문서 형식입니다.")


def _extract_html_text(data: bytes) -> str:
    raw = data.decode("utf-8", errors="replace")
    try:
        from bs4 import BeautifulSoup

        return BeautifulSoup(raw, "html.parser").get_text("\n")
    except Exception:
        no_script = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", raw)
        return html.unescape(re.sub(r"(?s)<[^>]+>", " ", no_script))


def _extract_pdf_text(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except Exception as exc:
        raise ValueError("PDF 처리를 위해 pypdf가 필요합니다.") from exc
    reader = PdfReader(BytesIO(data))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def _extract_docx_text(data: bytes) -> str:
    try:
        from docx import Document
    except Exception as exc:
        raise ValueError("DOCX 처리를 위해 python-docx가 필요합니다.") from exc
    doc = Document(BytesIO(data))
    return "\n".join(paragraph.text for paragraph in doc.paragraphs)
