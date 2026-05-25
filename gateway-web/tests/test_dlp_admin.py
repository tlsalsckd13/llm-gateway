from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from admin.dlp import evaluate_sample, validate_policy_payload


def template_env():
    templates = Path(__file__).resolve().parents[1] / "templates"
    return Environment(loader=FileSystemLoader(str(templates)))


def test_dlp_policy_payload_rejects_invalid_regex():
    payload, errors = validate_policy_payload(
        {
            "name": "Broken",
            "pattern_type": "custom",
            "pattern_regex": "[",
            "redaction_token": "[REDACTED]",
            "action": "block_and_mask",
            "priority": "100",
            "is_active": "true",
        }
    )
    assert payload is None
    assert "정규식 오류" in errors["pattern_regex"]


def test_dlp_preview_blocks_and_masks():
    result = evaluate_sample(
        "token secret-123",
        [
            {
                "id": 1,
                "name": "Secret",
                "pattern_type": "custom",
                "pattern_regex": r"secret-\d+",
                "redaction_token": "[MASKED]",
                "action": "block_and_mask",
                "priority": 100,
            }
        ],
    )
    assert result["will_block"] is True
    assert result["masked_text"] == "token [MASKED]"
    assert result["matches"][0]["matched_text"] == "secret-123"
    assert "<mark>secret-123</mark>" in result["highlighted_text"]


def test_dlp_templates_render_policy_actions():
    env = template_env()
    base = {
        "request": {},
        "page": "dlp",
        "user": {"display_name": "Admin", "email": "admin@example.com"},
        "active_tab": "policies",
        "csrf_token": "csrf",
        "pattern_types": ("krn", "card", "brn", "account", "custom"),
        "actions": ("block", "mask", "block_and_mask"),
    }
    list_html = env.get_template("admin/dlp_policies.html").render(
        **base,
        policies=[
            {
                "id": 1,
                "name": "Secret",
                "pattern_type": "custom",
                "pattern_regex": r"secret-\d+",
                "redaction_token": "[MASKED]",
                "action": "block_and_mask",
                "is_active": True,
                "priority": 100,
                "description": None,
                "match_count_7d": 2,
            }
        ],
    )
    assert "/admin/dlp/policies/1" in list_html
    assert "/admin/dlp/policies/1/deactivate" in list_html

    form_html = env.get_template("admin/dlp_policy_form.html").render(
        **base,
        mode="new",
        values={
            "name": "",
            "pattern_type": "custom",
            "pattern_regex": "",
            "redaction_token": "[REDACTED-CUSTOM]",
            "action": "block_and_mask",
            "priority": 100,
            "description": "",
            "is_active": True,
            "sample_text": "",
        },
        errors={},
        policy=None,
        preview=None,
    )
    assert 'name="_action" value="preview"' in form_html
    assert "/admin/dlp/test" in form_html
