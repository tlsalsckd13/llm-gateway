from decimal import Decimal
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from admin.budgets import decorate_team_budget, decorate_user_budget, parse_team_budget_payload, parse_user_budget_payload


def template_env():
    templates = Path(__file__).resolve().parents[1] / "templates"
    return Environment(loader=FileSystemLoader(str(templates)))


def test_team_budget_payload_requires_reason():
    payload, errors = parse_team_budget_payload(
        {
            "monthly_limit_usd": "100",
            "daily_limit_usd": "10",
            "alert_threshold_pct": "80",
            "reason": "",
        }
    )
    assert payload is None
    assert errors["reason"] == "변경 사유는 필수입니다."


def test_user_budget_payload_allows_blank_override():
    payload, errors = parse_user_budget_payload({"monthly_limit_usd": "", "reason": "clear"})
    assert errors == {}
    assert payload["monthly_limit_usd"] is None


def test_budget_decorators_compute_alerts():
    team = decorate_team_budget(
        {
            "today_used_usd": 8.0,
            "daily_limit_usd": 10.0,
            "month_used_usd": 10.0,
            "monthly_limit_usd": 100.0,
            "alert_threshold_pct": 80,
        }
    )
    assert team["daily_pct"] == 80.0
    assert team["alert_level"] == "warning"

    user = decorate_user_budget(
        {
            "has_user_override": True,
            "user_monthly_limit_usd": Decimal("20"),
            "user_month_used_usd": 19,
            "team_monthly_limit_usd": Decimal("100"),
            "team_month_used_usd": 30,
            "team_alert_threshold_pct": 80,
        }
    )
    assert user["effective_monthly_pct"] == 95.0
    assert user["alert_level"] == "critical"


def test_budget_templates_render_actions():
    env = template_env()
    base = {"request": {}, "page": "budgets", "user": {"display_name": "Admin", "email": "admin@example.com"}, "csrf_token": "csrf", "errors": {}}
    team_html = env.get_template("admin/budgets.html").render(
        **base,
        active_tab="teams",
        budgets=[
            {
                "id": 1,
                "team_key": "infra",
                "name": "Infra",
                "monthly_limit_usd": 100,
                "daily_limit_usd": 10,
                "alert_threshold_pct": 80,
                "today_used_usd": 1,
                "month_used_usd": 5,
                "daily_pct": 10,
                "monthly_pct": 5,
                "alert_level": "ok",
            }
        ],
    )
    assert "/admin/budgets/teams/1" in team_html

    user_html = env.get_template("admin/budgets_users.html").render(
        **base,
        active_tab="users",
        users=[
            {
                "id": 2,
                "email": "user@example.com",
                "display_name": "User",
                "team_id": "infra",
                "team_name": "Infra",
                "user_month_used_usd": 1,
                "team_month_used_usd": 5,
                "team_monthly_limit_usd": 100,
                "user_monthly_limit_usd": None,
                "has_user_override": False,
                "effective_monthly_pct": 5,
                "alert_level": "ok",
            }
        ],
    )
    assert "/admin/budgets/users/2" in user_html


def test_portal_usage_template_renders_budget_summary():
    html = template_env().get_template("portal/usage.html").render(
        request={},
        section="portal",
        page="portal_usage",
        user={"display_name": "User", "email": "user@example.com"},
        data={
            "usage": {"calls": 1, "input_tokens": 10, "cost_usd": 0.01},
            "budget": {
                "alert_level": "warning",
                "team_alert_threshold_pct": 80,
                "monthly_limit_usd": 10,
                "month_used": 8,
                "monthly_pct": 80,
                "monthly_remaining_usd": 2,
                "team_month_used_usd": 8,
                "team_monthly_limit_usd": 10,
                "team_monthly_remaining_usd": 2,
                "team_monthly_pct": 80,
                "team_day_used_usd": 1,
                "team_daily_limit_usd": 2,
                "team_daily_remaining_usd": 1,
                "team_daily_pct": 50,
                "has_user_override": False,
                "user_month_used_usd": 0.5,
                "user_monthly_limit_usd": None,
                "user_monthly_remaining_usd": None,
                "user_monthly_pct": 0,
            },
        },
    )
    assert "Team Monthly" in html
    assert "예산 사용률" in html
