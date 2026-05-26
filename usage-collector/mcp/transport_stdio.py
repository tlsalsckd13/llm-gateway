from __future__ import annotations

from typing import Any

from .broker import MCPServer, MCPToolResult


class StdioMCPTransport:
    async def list_tools(self, server: MCPServer) -> list[dict[str, Any]]:
        _ = server
        return []

    async def call_tool(self, server: MCPServer, tool_name: str, arguments: dict[str, Any]) -> MCPToolResult:
        _ = (server, tool_name, arguments)
        return MCPToolResult(status="error", content={"error": "stdio MCP transport is disabled in v3.0"})
