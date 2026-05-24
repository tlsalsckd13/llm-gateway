from decimal import Decimal
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from admin.teams import budget_changed, validate_team_payload


def test_budget_changed_ignores_decimal_scale():
    before = {
        "monthly_limit_usd": Decimal("50.0000"),
        "daily_limit_usd": Decimal("5.0000"),
        "alert_threshold_pct": 80,
    }
    after = {
        "monthly_limit_usd": Decimal("50.0"),
        "daily_limit_usd": Decimal("5"),
        "alert_threshold_pct": 80,
    }
    assert not budget_changed(before, after)


def test_team_key_validation_rejects_uppercase():
    payload, errors = validate_team_payload(
        {
            "team_key": "Infra",
            "name": "Infra",
            "monthly_limit_usd": "10",
            "daily_limit_usd": "1",
            "alert_threshold_pct": "80",
        },
        creating=True,
    )
    assert payload is None
    assert "team_key" in errors


def test_teams_template_renders_items_key():
    templates = Path(__file__).resolve().parents[1] / "templates"
    env = Environment(loader=FileSystemLoader(str(templates)))
    html = env.get_template("admin/teams.html").render(
        request={},
        page="teams",
        user={"display_name": "Admin", "email": "admin@example.com"},
        q="",
        sort="name",
        teams={
            "items": [
                {
                    "id": 1,
                    "team_key": "infra",
                    "name": "Infra",
                    "description": None,
                    "user_count": 1,
                    "month_used_usd": 0.1,
                    "monthly_pct": 0.2,
                    "alert_threshold_pct": 80,
                    "monthly_limit_usd": 50,
                    "daily_limit_usd": 5,
                    "is_active": True,
                }
            ],
            "total": 1,
            "page": 1,
            "per_page": 20,
            "has_prev": False,
            "has_next": False,
        },
    )
    assert "infra" in html
