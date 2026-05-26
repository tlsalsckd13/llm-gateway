from __future__ import annotations

from dataclasses import dataclass


DEFAULT_EMBEDDING_MODEL = "amazon.titan-embed-text-v2:0"
DEFAULT_EMBEDDING_DIM = 1024
TITAN_EMBEDDING_PRICE_PER_1K_TOKENS = 0.00002


@dataclass(frozen=True)
class EmbeddingResult:
    vector: list[float]
    input_tokens: int
    cost_usd: float


def estimate_embedding_cost(input_tokens: int) -> float:
    return (input_tokens / 1000.0) * TITAN_EMBEDDING_PRICE_PER_1K_TOKENS


async def embed_text(text: str, model_id: str = DEFAULT_EMBEDDING_MODEL) -> EmbeddingResult:
    _ = model_id
    input_tokens = max(1, len(text) // 4) if text else 0
    return EmbeddingResult(
        vector=[0.0] * DEFAULT_EMBEDDING_DIM,
        input_tokens=input_tokens,
        cost_usd=estimate_embedding_cost(input_tokens),
    )
