from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class MCPServer:
    id: int
    slug: str
    transport: str
    endpoint: str | None


@dataclass(frozen=True)
class MCPToolResult:
    status: str
    content: Any
    latency_ms: int | None = None


class MCPTransport(Protocol):
    async def list_tools(self, server: MCPServer) -> list[dict[str, Any]]:
        ...

    async def call_tool(self, server: MCPServer, tool_name: str, arguments: dict[str, Any]) -> MCPToolResult:
        ...


class MCPBroker:
    def __init__(self, transports: dict[str, MCPTransport] | None = None):
        self.transports = transports or {}

    async def list_tools(self, server: MCPServer) -> list[dict[str, Any]]:
        transport = self._transport_for(server)
        return await transport.list_tools(server)

    async def call_tool(self, server: MCPServer, tool_name: str, arguments: dict[str, Any]) -> MCPToolResult:
        transport = self._transport_for(server)
        return await transport.call_tool(server, tool_name, arguments)

    def _transport_for(self, server: MCPServer) -> MCPTransport:
        try:
            return self.transports[server.transport]
        except KeyError as exc:
            raise ValueError(f"unsupported MCP transport: {server.transport}") from exc
