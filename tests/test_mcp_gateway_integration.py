"""End-to-end integration test for the MCP Gateway (Property P1).

This is the context-reduction assertion from the design's Testing Strategy
("Integration / context-reduction assertion") and task 10.1:

* Stand up several **fake in-process MCP servers** as downstreams via the
  :class:`~headroom.mcp_gateway.downstream.DownstreamClientManager` injectable
  connector seam (the same ``FakeSession`` pattern used in
  ``tests/test_mcp_gateway_downstream.py``), giving them **many** tools total
  to stand in for a real hundreds-of-tools scenario.
* Build a real :class:`~headroom.mcp_gateway.server.MCPGatewayServer` over that
  manager and ``await server.start()``.
* **Property P1 (context reduction):** the gateway's own ``list_tools`` returns
  *exactly* the four meta-tools ``{find_tools, invoke_tool, describe_tool,
  list_servers}`` regardless of how many downstream tools exist — asserted both
  with several populated servers and with zero downstreams.
* **End-to-end flow:** ``find_tools(query)`` returns a relevant subset *with*
  input schemas (byte-equal to what the downstream advertised), then
  ``invoke_tool(server, tool, arguments)`` returns the fake downstream result
  byte-equal (the passthrough case, since ``compress_results`` defaults off).

Validates: Requirements 1.1, 1.2, 1.3, 1.4.

These exercise the gateway entirely through the connector seam with fake
in-process sessions — no subprocess or socket is spawned. ``mcp`` is installed
in this environment, so :class:`MCPGatewayServer` constructs normally; the
``list_tools`` handler registered via ``@self.server.list_tools()`` is reached
through ``server.server.request_handlers`` exactly as the ``mcp`` SDK dispatches
it at runtime.
"""

from __future__ import annotations

import json
from typing import Any

from mcp.types import ListToolsRequest

from headroom.mcp_gateway.config import DownstreamSpec, GatewayConfigModel
from headroom.mcp_gateway.downstream import DownstreamClientManager
from headroom.mcp_gateway.server import META_TOOL_NAMES, MCPGatewayServer
from headroom.mcp_registry.base import ServerSpec


class FakeSession:
    """A fake in-process downstream session exposing a canned tool list.

    Mirrors the ``FakeSession`` pattern in ``tests/test_mcp_gateway_downstream``
    but returns tool *schemas* (so the index/search have real ``input_schema``s
    to serve) and canned string results (so ``invoke_tool`` passthrough is
    byte-equal).
    """

    def __init__(self, name: str, tools: list[dict[str, Any]]) -> None:
        self.name = name
        self._tools = tools
        self.list_calls = 0
        self.calls: list[tuple[str, dict]] = []
        self.closed = False

    async def list_tools(self) -> list[dict[str, Any]]:
        self.list_calls += 1
        return list(self._tools)

    async def call_tool(self, tool: str, arguments: dict) -> str:
        self.calls.append((tool, arguments))
        # Deterministic, byte-equal-checkable downstream result.
        return f"RESULT::{self.name}::{tool}::{json.dumps(arguments, sort_keys=True)}"

    async def ping(self) -> None:
        return None

    async def aclose(self) -> None:
        self.closed = True


def _tool(name: str, description: str) -> dict[str, Any]:
    """Build a downstream tool advertisement with a non-trivial input schema."""
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": {
                "q": {"type": "string", "description": "a query argument"},
                "count": {"type": "integer", "minimum": 1},
            },
            "required": ["q"],
        },
    }


def _build_fakes() -> dict[str, FakeSession]:
    """Several fake servers with MANY tools total (tens across servers).

    Represents the "hundreds of tools" scenario the gateway is designed to collapse.
    Includes one highly distinctive tool (``reticulate_splines`` on ``svc-a``)
    so the end-to-end ``find_tools`` query matches deterministically under both
    the embedding and BM25 keyword ranking paths.
    """
    sessions: dict[str, FakeSession] = {}
    # 5 servers x ~44 generic tools = 220 tools, standing in for hundreds.
    for s in range(5):
        server = f"svc-{chr(ord('a') + s)}"
        tools = [
            _tool(f"tool_{server}_{i:02d}", f"generic capability number {i} on {server}")
            for i in range(44)
        ]
        sessions[server] = FakeSession(server, tools)

    # Distinctive discoverable tool for the deterministic e2e flow.
    sessions["svc-a"]._tools.append(
        _tool(
            "reticulate_splines",
            "reticulate splines for a mesh; distinctive discoverable capability",
        )
    )
    return sessions


def _connector_from(sessions: dict[str, FakeSession]):
    async def connector(spec: DownstreamSpec) -> FakeSession:
        session = sessions.get(spec.name)
        if session is None:
            raise RuntimeError(f"connect refused for {spec.name}")
        return session

    return connector


def _stdio_downstream(name: str) -> DownstreamSpec:
    return DownstreamSpec(
        name=name,
        transport="stdio",
        stdio=ServerSpec(name=name, command="cmd", args=(), env={}),
    )


async def _gateway_list_tools(server: MCPGatewayServer) -> list[str]:
    """Invoke the gateway's registered ``list_tools`` handler and return names.

    Reaches the handler the same way the ``mcp`` SDK does at runtime: the
    ``@self.server.list_tools()`` decorator stores a handler under
    ``ListToolsRequest`` in ``server.server.request_handlers``. Calling it
    returns a ``ServerResult`` whose ``.root`` is a ``ListToolsResult``.
    """
    handler = server.server.request_handlers[ListToolsRequest]
    result = await handler(ListToolsRequest(method="tools/list"))
    return [tool.name for tool in result.root.tools]


async def test_p1_list_tools_returns_only_meta_tools_with_many_downstreams() -> None:
    """P1: many downstream tools, gateway still exposes only the four meta-tools."""
    sessions = _build_fakes()
    total_downstream_tools = sum(len(s._tools) for s in sessions.values())
    assert total_downstream_tools >= 200  # the "hundreds of tools" scenario

    manager = DownstreamClientManager(connector=_connector_from(sessions))
    config = GatewayConfigModel(
        downstreams=[_stdio_downstream(name) for name in sessions],
    )
    server = MCPGatewayServer(config, manager=manager)
    await server.start()

    names = await _gateway_list_tools(server)
    assert set(names) == set(META_TOOL_NAMES)
    assert len(names) == 4

    await server.cleanup()


async def test_p1_list_tools_returns_only_meta_tools_with_zero_downstreams() -> None:
    """P1: zero downstreams still yields exactly the four meta-tools (Req 1.3)."""
    manager = DownstreamClientManager(connector=_connector_from({}))
    server = MCPGatewayServer(GatewayConfigModel(downstreams=[]), manager=manager)
    await server.start()

    names = await _gateway_list_tools(server)
    assert set(names) == set(META_TOOL_NAMES)
    assert len(names) == 4

    await server.cleanup()


async def test_end_to_end_find_then_invoke_returns_downstream_result() -> None:
    """find_tools -> invoke_tool: relevant subset with schemas, byte-equal result."""
    sessions = _build_fakes()
    manager = DownstreamClientManager(connector=_connector_from(sessions))
    config = GatewayConfigModel(
        downstreams=[_stdio_downstream(name) for name in sessions],
    )
    server = MCPGatewayServer(config, manager=manager)
    await server.start()

    # find_tools returns a relevant, bounded subset — each entry with its schema.
    find_result = await server._handle_find_tools({"query": "reticulate splines"})
    payload = json.loads(find_result[0].text)
    assert payload["count"] == len(payload["tools"])
    assert 1 <= payload["count"] <= config.find_limit_default

    for entry in payload["tools"]:
        assert set(entry) >= {"namespaced_id", "server", "tool", "description", "input_schema"}
        assert isinstance(entry["input_schema"], dict)

    # The distinctive tool ranks in; its schema is byte-equal to the advertised one.
    match = next(e for e in payload["tools"] if e["tool"] == "reticulate_splines")
    assert match["server"] == "svc-a"
    advertised = next(
        t["inputSchema"] for t in sessions["svc-a"]._tools if t["name"] == "reticulate_splines"
    )
    assert match["input_schema"] == advertised

    # invoke_tool routes to exactly that server+tool and returns byte-equal output.
    arguments = {"q": "mesh-42", "count": 3}
    invoke_result = await server._handle_invoke_tool(
        {"server": match["server"], "tool": match["tool"], "arguments": arguments}
    )
    assert len(invoke_result) == 1
    expected = f"RESULT::svc-a::reticulate_splines::{json.dumps(arguments, sort_keys=True)}"
    assert invoke_result[0].text == expected

    # Routing reached only svc-a's session (Property P2 groundwork).
    assert sessions["svc-a"].calls == [("reticulate_splines", arguments)]
    for name, session in sessions.items():
        if name != "svc-a":
            assert session.calls == []

    await server.cleanup()
