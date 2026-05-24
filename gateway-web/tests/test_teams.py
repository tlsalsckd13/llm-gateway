from decimal import Decimal

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
