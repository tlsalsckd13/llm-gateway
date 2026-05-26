from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from admin.skills import s3_key_for
from portal.skills import can_manage_team_skills


def template_env():
    templates = Path(__file__).resolve().parents[1] / "templates"
    return Environment(loader=FileSystemLoader(str(templates)))


def test_skill_s3_key_paths():
    assert s3_key_for(7, "org", "1.0.0") == "org/7/v1.0.0.zip"
    assert s3_key_for(7, "team", "1.0.0", team_key="infra") == "team/infra/7/v1.0.0.zip"
    assert s3_key_for(7, "user", "1.0.0", owner_user_id=42) == "user/42/7/v1.0.0.zip"


def test_team_owner_can_manage_team_skills():
    assert can_manage_team_skills({"role": "team_owner", "team_id_fk": 1}) is True
    assert can_manage_team_skills({"role": "user", "team_id_fk": 1}) is False


def test_admin_skills_template_renders():
    html = template_env().get_template("admin/skills.html").render(
        request={},
        page="skills",
        user={"display_name": "Admin", "email": "admin@example.com"},
        q="",
        selected_status="",
        selected_scope="",
        statuses=("pending", "approved", "rejected"),
        owner_scopes=("org", "team", "user"),
        skills={
            "items": [
                {
                    "id": 1,
                    "slug": "kcs-korean-formal-tone",
                    "description": "formal",
                    "owner_scope": "org",
                    "latest_version": "1.0.0",
                    "status": "pending",
                    "is_active": True,
                    "activation_count": 0,
                    "created_at": "now",
                }
            ],
            "page": 1,
            "has_prev": False,
            "has_next": False,
        },
    )
    assert "kcs-korean-formal-tone" in html
    assert "/admin/skills/1" in html


def test_portal_skills_template_renders():
    html = template_env().get_template("portal/skills.html").render(
        request={},
        section="portal",
        page="portal_skills",
        user={"display_name": "User", "email": "user@example.com"},
        csrf_token="csrf",
        can_manage_team=True,
        skills=[
            {
                "id": 1,
                "slug": "kcs-korean-formal-tone",
                "description": "formal",
                "owner_scope": "org",
                "latest_version": "1.0.0",
                "user_enabled": False,
                "team_enabled": True,
            }
        ],
        published=[],
    )
    assert "/portal/skills/1/activate" in html
    assert "/portal/skills/1/deactivate" in html
