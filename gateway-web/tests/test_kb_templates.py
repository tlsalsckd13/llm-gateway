from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from portal.kb import can_manage_team_kb


def template_env():
    templates = Path(__file__).resolve().parents[1] / "templates"
    return Environment(loader=FileSystemLoader(str(templates)))


def test_admin_kb_template_renders_create_action():
    html = template_env().get_template("admin/kb.html").render(
        request={},
        page="kb",
        user={"display_name": "Admin", "email": "admin@example.com"},
        csrf_token="csrf",
        rows=[
            {
                "team_id": 1,
                "team_key": "infra",
                "team_name": "Infra",
                "kb_id": None,
                "document_count": 0,
                "chunk_count": 0,
                "embedding_cost_usd": 0,
            }
        ],
    )
    assert "/admin/kb/teams/1/create" in html


def test_admin_kb_detail_template_renders_documents():
    html = template_env().get_template("admin/kb_detail.html").render(
        request={},
        page="kb",
        user={"display_name": "Admin", "email": "admin@example.com"},
        csrf_token="csrf",
        kb={
            "id": 1,
            "name": "Infra",
            "team_name": "Infra",
            "s3_prefix": "team/infra/",
            "embedding_model": "amazon.titan-embed-text-v2:0",
            "status": "active",
            "document_count": 1,
            "chunk_count": 2,
            "embedding_cost_usd": 0.0001,
            "top_k_default": 5,
        },
        documents=[
            {
                "id": 1,
                "title": "handover.md",
                "s3_key": "team/infra/1.md",
                "ingestion_status": "succeeded",
                "ingestion_error": None,
                "chunk_count": 2,
                "embedding_token_cost_usd": 0.0001,
                "uploaded_at": "now",
                "removed_at": None,
            }
        ],
        errors={},
        retrieve=None,
    )
    assert "handover.md" in html
    assert "/admin/kb/1/test-retrieve" in html


def test_portal_kb_template_and_role_check():
    assert can_manage_team_kb({"role": "team_owner", "team_id_fk": 1}) is True
    assert can_manage_team_kb({"role": "user", "team_id_fk": 1}) is False
    html = template_env().get_template("portal/kb.html").render(
        request={},
        section="portal",
        page="portal_kb",
        user={"display_name": "User", "email": "user@example.com"},
        csrf_token="csrf",
        can_manage_team=True,
        kb={
            "id": 1,
            "team_name": "Infra",
            "document_count": 0,
            "chunk_count": 0,
            "user_kb_enabled": True,
        },
        documents=[],
        errors={},
    )
    assert "/portal/kb/toggle" in html
    assert "/portal/kb/documents" in html
