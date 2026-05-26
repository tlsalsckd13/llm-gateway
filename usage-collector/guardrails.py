from __future__ import annotations

import hashlib
from dataclasses import dataclass, field


@dataclass(frozen=True)
class GuardrailDecision:
    allowed: bool
    action: str
    violation_types: list[str] = field(default_factory=list)
    redacted_text: str | None = None


def sha256_text(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


async def apply_guardrail(text: str, side: str = "input") -> GuardrailDecision:
    _ = side
    return GuardrailDecision(allowed=True, action="allowed", redacted_text=text)
