from __future__ import annotations

from auth.middleware import require_role


ADMIN_ROLE = "admin"
TEAM_OWNER_ROLE = "team_owner"
USER_ROLE = "user"


def require_admin():
    return require_role(ADMIN_ROLE)


def require_team_owner_or_admin():
    return require_role(ADMIN_ROLE, TEAM_OWNER_ROLE)


def require_authenticated_user():
    return require_role(ADMIN_ROLE, TEAM_OWNER_ROLE, USER_ROLE)


def can_manage_team(actor: dict, team_id: int | None) -> bool:
    if actor.get("role") == ADMIN_ROLE:
        return True
    if actor.get("role") != TEAM_OWNER_ROLE:
        return False
    return str(actor.get("team_id_fk")) == str(team_id)
