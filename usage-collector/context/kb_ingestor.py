from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TextChunk:
    index: int
    content: str
    token_count: int


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4) if text else 0


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
