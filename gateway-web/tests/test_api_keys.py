from datetime import timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from common.api_keys import display_prefix, generate_api_key, hash_api_key, parse_expires_at


def template_env():
    templates = Path(__file__).resolve().parents[1] / "templates"
    return Environment(loader=FileSystemLoader(str(templates)))


def test_generate_api_key_hash_and_prefix():
    key = generate_api_key()
    assert key.startswith("ai-poc-")
    assert len(hash_api_key(key)) == 64
    assert display_prefix(key) == key[:18]


def test_parse_expires_at_makes_datetime_local_utc():
    parsed = parse_expires_at("2026-05-24T12:30")
    assert parsed.tzinfo == timezone.utc


def test_admin_keys_template_renders_actions():
    html = template_env().get_template("admin/keys.html").render(
        request={},
        page="keys",
        user={"display_name": "Admin", "email": "admin@example.com"},
        csrf_token="csrf",
        keys=[
            {
                "key_hash": "abc",
                "key_prefix": "ai-poc-123",
                "user_id": "user@example.com",
                "team_id": "infra",
                "label": "local",
                "issued_via": "admin",
                "created_at": "now",
                "expires_at": None,
                "last_used_at": None,
                "status": "active",
            }
        ],
    )
    assert "/admin/keys/abc/revoke" in html


def test_portal_keys_template_renders_actions():
    html = template_env().get_template("portal/keys.html").render(
        request={},
        section="portal",
        page="portal_keys",
        user={"display_name": "User", "email": "user@example.com"},
        csrf_token="csrf",
        keys=[
            {
                "key_hash": "abc",
                "key_prefix": "ai-poc-123",
                "label": "local",
                "created_at": "now",
                "expires_at": None,
                "last_used_at": None,
                "status": "active",
            }
        ],
    )
    assert "/portal/keys/abc/revoke" in html
