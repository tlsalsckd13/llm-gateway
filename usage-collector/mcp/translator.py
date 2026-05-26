from __future__ import annotations

from typing import Any


def mcp_tool_to_bedrock(tool: dict[str, Any]) -> dict[str, Any]:
    name = tool.get("name")
    description = tool.get("description", "")
    input_schema = tool.get("inputSchema") or tool.get("input_schema") or {"type": "object", "properties": {}}
    return {
        "toolSpec": {
            "name": name,
            "description": description,
            "inputSchema": {"json": input_schema},
        }
    }


def bedrock_tool_use_to_mcp_call(tool_use: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    return str(tool_use.get("name")), dict(tool_use.get("input") or {})
