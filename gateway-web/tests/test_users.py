from datetime import datetime, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from admin.queries import encode
from admin.users import validate_user_payload


def template_env():
    templates = Path(__file__).resolve().parents[1] / "templates"
    return Environment(loader=FileSystemLoader(str(templates)))


def test_encode_handles_date_values():
    assert encode({"day": datetime(2026, 5, 24, tzinfo=timezone.utc).date()}) == {"day": "2026-05-24"}


def test_user_payload_rejects_invalid_email():
    payload, errors = validate_user_payload(
        {
            "email": "not-email",
            "display_name": "User",
            "role": "user",
            "team_id_fk": "1",
        },
        creating=True,
    )
    assert payload is None
    assert "email" in errors


def test_users_template_renders_items_key():
    html = template_env().get_template("admin/users.html").render(
        request={},
        page="users",
        user={"display_name": "Admin", "email": "admin@example.com"},
        roles=("admin", "user"),
        teams=[{"id": 1, "team_key": "infra", "name": "Infra"}],
        users={
            "items": [
                {
                    "id": 1,
                    "email": "user@example.com",
                    "display_name": "User",
                    "role": "user",
                    "team_name": "Infra",
                    "team_key": "infra",
                    "team_id": "infra",
                    "department": None,
                    "status": "active",
                    "last_login_at": None,
                }
            ],
            "total": 1,
            "page": 1,
            "per_page": 20,
            "has_prev": False,
            "has_next": False,
        },
        q="",
        selected_role="",
        selected_team="",
        selected_status="",
    )
    assert "user@example.com" in html


def test_user_detail_template_renders():
    html = template_env().get_template("admin/user_detail.html").render(
        request={},
        page="users",
        user={"display_name": "Admin", "email": "admin@example.com"},
        roles=("admin", "user"),
        user_record={
            "id": 1,
            "email": "user@example.com",
            "display_name": "User",
            "role": "user",
            "team_id_fk": 1,
            "team_name": "Infra",
            "department": None,
            "hire_date": None,
            "manager_user_id": None,
            "archived_at": None,
            "locked_until": None,
            "is_active": True,
        },
        teams=[{"id": 1, "team_key": "infra", "name": "Infra"}],
        managers=[],
        keys=[],
        usage={"calls": 0, "cost_usd": 0},
        audit=[],
        errors={},
        csrf_token="csrf",
    )
    assert "user@example.com" in html
