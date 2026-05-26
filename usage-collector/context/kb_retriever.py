from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RetrievedChunk:
    id: int
    content: str
    document_id: int
    source: str
    score: float


def format_chunks_for_prompt(chunks: list[RetrievedChunk]) -> str:
    return "\n\n".join(
        f'<chunk source="{chunk.source}" score="{chunk.score:.3f}">{chunk.content}</chunk>'
        for chunk in chunks
    )


async def retrieve(team_id: int, query: str, top_k: int = 5) -> list[RetrievedChunk]:
    _ = (team_id, query, top_k)
    return []
