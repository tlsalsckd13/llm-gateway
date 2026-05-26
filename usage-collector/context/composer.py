from __future__ import annotations

from dataclasses import dataclass, field


SECTION_ORDER = (
    "org_policy",
    "team_policy",
    "knowledge_base",
    "available_skills",
    "user_preferences",
)


@dataclass(frozen=True)
class PromptLayer:
    name: str
    body: str


@dataclass(frozen=True)
class CompositionResult:
    system_prompt: str
    kb_chunks_used: list[object] = field(default_factory=list)
    skills_used: list[object] = field(default_factory=list)


def wrap_section(name: str, body: str) -> str:
    return f"<{name}>\n{body.strip()}\n</{name}>"


def compose_layers(layers: list[PromptLayer]) -> str:
    order = {name: index for index, name in enumerate(SECTION_ORDER)}
    normalized = [layer for layer in layers if layer.body and layer.body.strip()]
    normalized.sort(key=lambda layer: order.get(layer.name, len(order)))
    return "\n\n".join(wrap_section(layer.name, layer.body) for layer in normalized)


async def compose_system_prompt(
    org_policy: str | None = None,
    team_policy: str | None = None,
    knowledge_base: str | None = None,
    available_skills: str | None = None,
    user_preferences: str | None = None,
    kb_chunks_used: list[object] | None = None,
    skills_used: list[object] | None = None,
) -> CompositionResult:
    layers = [
        PromptLayer("org_policy", org_policy or ""),
        PromptLayer("team_policy", team_policy or ""),
        PromptLayer("knowledge_base", knowledge_base or ""),
        PromptLayer("available_skills", available_skills or ""),
        PromptLayer("user_preferences", user_preferences or ""),
    ]
    return CompositionResult(
        system_prompt=compose_layers(layers),
        kb_chunks_used=kb_chunks_used or [],
        skills_used=skills_used or [],
    )
