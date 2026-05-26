from common.kb_processing import (
    chunk_text,
    estimate_embedding_cost,
    extract_text,
    is_allowed_document,
    normalize_mime,
    sha256_bytes,
    vector_literal,
)


def test_kb_processing_allows_supported_docs_by_extension():
    assert is_allowed_document("handover.md", "application/octet-stream")
    assert normalize_mime("handover.md", "application/octet-stream") == "text/markdown"
    assert is_allowed_document("manual.pdf", "application/pdf")
    assert not is_allowed_document("archive.zip", "application/zip")


def test_extract_text_for_markdown_and_html():
    assert "hello" in extract_text("a.md", "text/markdown", b"# hello")
    html_text = extract_text("a.html", "text/html", b"<html><body><h1>Hello</h1></body></html>")
    assert "Hello" in html_text


def test_chunking_cost_hash_and_vector_literal():
    chunks = chunk_text("a" * 5000, chunk_size_tokens=200, overlap_tokens=20)
    assert len(chunks) > 1
    assert estimate_embedding_cost(1000) == 0.00002
    assert len(sha256_bytes(b"abc")) == 64
    assert vector_literal([0.1, 0.2, 0.3]) == "[0.1,0.2,0.3]"
