import asyncio

from context.composer import PromptLayer, compose_layers, compose_system_prompt
from context.kb_embedder import DEFAULT_EMBEDDING_DIM, embed_text, estimate_embedding_cost
from context.kb_ingestor import chunk_text
from context.kb_retriever import RetrievedChunk, format_chunks_for_prompt
from context.policy_cache import TTLCache
from context.skill_loader import ActiveSkill, format_skills_for_prompt
from guardrails import apply_guardrail, sha256_text
from mcp.broker import MCPBroker, MCPServer
from mcp.translator import bedrock_tool_use_to_mcp_call, mcp_tool_to_bedrock


def test_composer_preserves_v3_layer_order():
    prompt = compose_layers([
        PromptLayer("user_preferences", "내 말투"),
        PromptLayer("org_policy", "조직 정책"),
        PromptLayer("available_skills", "스킬"),
    ])
    assert prompt.index("<org_policy>") < prompt.index("<available_skills>") < prompt.index("<user_preferences>")


def test_async_composer_returns_result():
    result = asyncio.run(compose_system_prompt(org_policy="org", team_policy="team"))
    assert "<org_policy>" in result.system_prompt
    assert "<team_policy>" in result.system_prompt


def test_kb_and_skill_formatters_are_prompt_ready():
    chunks = [RetrievedChunk(id=1, content="문서 내용", document_id=2, source="handover.md", score=0.91)]
    skills = [ActiveSkill(id=1, name="tone", description="formal", version="1.0.0")]
    assert 'source="handover.md"' in format_chunks_for_prompt(chunks)
    assert "**tone**" in format_skills_for_prompt(skills)


def test_ingestor_embedder_and_cache_stubs():
    chunks = chunk_text("가" * 5000, chunk_size_tokens=200, overlap_tokens=20)
    assert len(chunks) > 1
    assert estimate_embedding_cost(1000) > 0
    embedding = asyncio.run(embed_text("hello"))
    assert len(embedding.vector) == DEFAULT_EMBEDDING_DIM

    cache = TTLCache[str](ttl_seconds=60)
    cache.set("key", "value")
    assert cache.get("key") == "value"


def test_mcp_translator_and_broker_error():
    bedrock_tool = mcp_tool_to_bedrock({"name": "echo", "description": "Echo", "inputSchema": {"type": "object"}})
    assert bedrock_tool["toolSpec"]["name"] == "echo"
    assert bedrock_tool_use_to_mcp_call({"name": "echo", "input": {"x": 1}}) == ("echo", {"x": 1})

    broker = MCPBroker()
    server = MCPServer(id=1, slug="echo", transport="http", endpoint="https://example.com")
    try:
        asyncio.run(broker.list_tools(server))
    except ValueError as exc:
        assert "unsupported MCP transport" in str(exc)
    else:
        raise AssertionError("broker should reject unregistered transports")


def test_guardrails_stub_hashes_text_and_allows_by_default():
    assert len(sha256_text("secret")) == 64
    decision = asyncio.run(apply_guardrail("hello"))
    assert decision.allowed is True
