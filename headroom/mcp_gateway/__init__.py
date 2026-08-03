"""MCP Gateway — aggregate many downstream MCP servers behind one server.

MCP clients load every enabled server's full tool schemas into context on
every turn. The MCP Gateway is a single Headroom MCP server that connects to N
downstream servers, keeps their tools *out of context* in a searchable index,
and exposes only a small fixed set of meta-tools (``find_tools`` /
``invoke_tool`` / ``describe_tool`` / ``list_servers``).

This package is additive and reuses existing Headroom building blocks
(``ServerSpec`` / ``KiroRegistrar``, ``CompressionStore``, ``EmbeddingScorer``,
``headroom.compress``). This module currently exports the core data models;
later tasks add config resolution, the downstream client manager, tool search,
and the gateway server.
"""

from __future__ import annotations

from .config import DownstreamSpec, GatewayConfigModel, resolve_gateway_config
from .downstream import AggregationResult, DownstreamClientManager
from .index import ToolIndex, ToolIndexEntry
from .search import ToolSearch
from .server import MCPGatewayServer

__all__ = [
    "AggregationResult",
    "DownstreamClientManager",
    "DownstreamSpec",
    "GatewayConfigModel",
    "MCPGatewayServer",
    "ToolIndex",
    "ToolIndexEntry",
    "ToolSearch",
    "resolve_gateway_config",
]
