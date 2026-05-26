from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ActiveSkill:
    id: int
    name: str
    description: str
    version: str


def format_skills_for_prompt(skills: list[ActiveSkill]) -> str:
    return "\n".join(f"- **{skill.name}**: {skill.description}" for skill in skills)


async def list_active(user_id: int, team_id: int) -> list[ActiveSkill]:
    _ = (user_id, team_id)
    return []
