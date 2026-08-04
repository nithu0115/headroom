"""Tests for :class:`headroom.mcp_gateway.server.MCPGatewayServer` meta-tools.

These drive the four meta-tool handlers directly on injected instance state
(``_index`` / ``_search`` / ``_manager`` / ``_config``) with in-process fakes,
so no real stdio session, subprocess, or socket is ever spawned. ``mcp`` is
installed in this environment, so the server constructs normally. The
downstream index is a real :class:`~headroom.mcp_gateway.index.ToolIndex` built
from plain tool dicts, and :class:`~headroom.mcp_gateway.search.ToolSearch`
runs its deterministic BM25 keyword path (``embedding_scorer=None``).

Covers the section-7 test tasks:

* 7.3 — ``find_tools`` completeness (**Property P9**, Req 5.4), default-limit
  application (Req 5.6), and the invalid-limit structured error (Req 5.7).
* 7.5 — ``describe_tool`` resolution paths (Req 6.2-6.5): namespaced hit, unique
  bare-name hit (byte-equal schema), ambiguous disambiguation, not-found, and
  the empty/whitespace validation error.
* 7.7 — invocation routing (**Property P2**, Req 7.1/7.2): with duplicate tool
  names across servers, ``invoke_tool`` reaches exactly the addressed session.
* 7.8 — result fidelity (**Property P4**, Req 7.3/7.4/7.5): byte-equal
  passthrough (P4a) and the compression round-trip (P4b) whose stored ``hash``
  retrieves the exact original.
* 7.9 — secret redaction (**Property P12**, Req 12.7): a stored downstream
  result's retrieval-log path emits no raw secret/auth/API-key values.
* 7.11 — ``list_servers`` inventory + health (Req 8.1/8.2/8.3), including the
  empty-inventory case.

The final section covers background retry of failed downstreams (Req 2.9): the
gateway is driven through a real :class:`DownstreamClientManager` over a
late-binding fake connector, so a downstream that becomes healthy after
aggregation is recovered, its tools land in a rebuilt index, and the retry task
is always cancelled by ``cleanup()`` rather than leaked.
"""

from __future__ import annotations

import asyncio
import json

import pytest

# Skip entire module if hypothesis not installed
pytest.importorskip("hypothesis")
from hypothesis import given, settings
from hypothesis import strategies as st

from headroom.cache.compression_store import get_compression_store
from headroom.mcp_gateway.config import DownstreamSpec, GatewayConfigModel
from headroom.mcp_gateway.downstream import (
    DownstreamClientManager,
    DownstreamUnavailableError,
)
from headroom.mcp_gateway.index import ToolIndex
from headroom.mcp_gateway.search import ToolSearch
from headroom.mcp_gateway.server import MCPGatewayServer
from headroom.mcp_registry.base import ServerSpec

# --- fakes / helpers --------------------------------------------------------


class FakeManager:
    """A fake DownstreamClientManager exposing only ``call`` and ``status``.

    Records every ``(server, tool, arguments)`` it is asked to route so routing
    tests can assert exactly which downstream session was addressed.
    """

    def __init__(
        self,
        *,
        result: object = "ok",
        raise_exc: BaseException | None = None,
        status_map: dict[str, str] | None = None,
    ) -> None:
        self.result = result
        self.raise_exc = raise_exc
        self._status = dict(status_map or {})
        self.calls: list[tuple[str, str, dict]] = []

    async def call(self, server: str, tool: str, arguments: dict) -> object:
        self.calls.append((server, tool, arguments))
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.result

    async def status(self) -> dict[str, str]:
        return dict(self._status)


def _tool(name: str, *, description: str = "", schema: dict | None = None) -> dict:
    return {
        "name": name,
        "description": description,
        "inputSchema": schema if schema is not None else {"type": "object"},
    }


def _build_index(tools_by_server: dict[str, list[dict]]) -> ToolIndex:
    index = ToolIndex(embedding_scorer=None)
    index.build(tools_by_server)
    return index


def _make_server(
    index: ToolIndex,
    manager: FakeManager | None,
    *,
    compress_results: bool = False,
    compress_min_tokens: int = 1000,
    find_limit_default: int = 5,
) -> MCPGatewayServer:
    config = GatewayConfigModel(
        downstreams=[],
        find_limit_default=find_limit_default,
        compress_results=compress_results,
        compress_min_tokens=compress_min_tokens,
    )
    server = MCPGatewayServer(config, manager=manager)
    server._index = index
    server._search = ToolSearch(index, embedding_scorer=None)
    return server


def _payload(result: list) -> dict:
    """Parse the JSON payload from a single-``TextContent`` handler result."""
    assert len(result) == 1
    return json.loads(result[0].text)


# ===========================================================================
# 7.3 — find_tools completeness (P9) + default limit + invalid limit
# Validates: Requirements 5.4, 5.6, 5.7
# ===========================================================================


async def test_find_tools_result_includes_all_fields() -> None:
    # Property P9: every returned entry carries all five fields.
    index = _build_index(
        {
            "srv": [
                _tool(
                    "search_files",
                    description="search files on disk",
                    schema={"type": "object", "x": 1},
                ),
                _tool(
                    "search_web",
                    description="search the web for pages",
                    schema={"type": "object", "y": 2},
                ),
            ]
        }
    )
    server = _make_server(index, FakeManager())

    result = await server._handle_find_tools({"query": "search"})
    payload = _payload(result)

    assert payload["count"] == len(payload["tools"])
    assert payload["tools"], "expected at least one match for 'search'"
    for entry in payload["tools"]:
        assert set(entry) == {
            "namespaced_id",
            "server",
            "tool",
            "description",
            "input_schema",
        }
        assert entry["namespaced_id"] == f"{entry['server']}::{entry['tool']}"
        # Schema is byte-equal to what the index stored (P3 downstream).
        stored = index.get(entry["namespaced_id"])
        assert stored is not None
        assert entry["input_schema"] == stored.input_schema


async def test_find_tools_applies_default_limit_when_omitted() -> None:
    # Six tools all matching "report"; default limit 2 caps the result.
    tools = [_tool(f"report_{i}", description="generate a report summary") for i in range(6)]
    index = _build_index({"srv": tools})
    server = _make_server(index, FakeManager(), find_limit_default=2)

    payload = _payload(await server._handle_find_tools({"query": "report"}))
    assert payload["count"] == 2
    assert len(payload["tools"]) == 2


async def test_find_tools_explicit_limit_overrides_default() -> None:
    tools = [_tool(f"report_{i}", description="generate a report") for i in range(6)]
    index = _build_index({"srv": tools})
    server = _make_server(index, FakeManager(), find_limit_default=2)

    payload = _payload(await server._handle_find_tools({"query": "report", "limit": 4}))
    assert payload["count"] == 4


@pytest.mark.parametrize("bad_limit", [0, -1, "3", 1.5])
async def test_find_tools_invalid_limit_returns_structured_error(bad_limit: object) -> None:
    index = _build_index({"srv": [_tool("t", description="a tool")]})
    server = _make_server(index, FakeManager())

    payload = _payload(await server._handle_find_tools({"query": "tool", "limit": bad_limit}))
    assert "error" in payload
    assert payload["field"] == "limit"
    # No results are leaked alongside the error.
    assert "tools" not in payload


# ===========================================================================
# 7.5 — describe_tool resolution paths
# Validates: Requirements 6.2, 6.3, 6.4, 6.5
# ===========================================================================


async def test_describe_tool_namespaced_hit() -> None:
    schema = {"type": "object", "properties": {"a": {"type": "string"}}}
    index = _build_index({"srv": [_tool("thing", description="does a thing", schema=schema)]})
    server = _make_server(index, FakeManager())

    payload = _payload(await server._handle_describe_tool({"name": "srv::thing"}))
    assert payload["namespaced_id"] == "srv::thing"
    assert payload["server"] == "srv"
    assert payload["tool"] == "thing"
    assert payload["description"] == "does a thing"
    assert payload["input_schema"] == schema


async def test_describe_tool_unique_bare_name_hit_byte_equal_schema() -> None:
    schema = {"type": "object", "required": ["x"], "properties": {"x": {"type": "number"}}}
    index = _build_index({"srv": [_tool("only_here", description="unique", schema=schema)]})
    server = _make_server(index, FakeManager())

    payload = _payload(await server._handle_describe_tool({"name": "only_here"}))
    assert payload["namespaced_id"] == "srv::only_here"
    # Byte-equal schema round-trips through the JSON payload.
    assert json.dumps(payload["input_schema"], sort_keys=True) == json.dumps(schema, sort_keys=True)


async def test_describe_tool_ambiguous_bare_name_disambiguation() -> None:
    index = _build_index(
        {
            "alpha": [_tool("search", description="alpha search")],
            "beta": [_tool("search", description="beta search")],
        }
    )
    server = _make_server(index, FakeManager())

    payload = _payload(await server._handle_describe_tool({"name": "search"}))
    assert "ambiguous" in payload["error"].lower()
    assert payload["matches"] == ["alpha::search", "beta::search"]
    # No single tool's schema is returned for an ambiguous name.
    assert "input_schema" not in payload


async def test_describe_tool_not_found() -> None:
    index = _build_index({"srv": [_tool("real")]})
    server = _make_server(index, FakeManager())

    payload = _payload(await server._handle_describe_tool({"name": "ghost"}))
    assert payload["error"] == "tool not found"
    assert payload["name"] == "ghost"


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
async def test_describe_tool_empty_or_whitespace_name_validation_error(blank: str) -> None:
    index = _build_index({"srv": [_tool("real")]})
    server = _make_server(index, FakeManager())

    payload = _payload(await server._handle_describe_tool({"name": blank}))
    assert payload["error"] == "a tool name is required"
    assert payload["field"] == "name"


# ===========================================================================
# 7.7 — Property P2: invocation routing
# Validates: Requirements 7.1, 7.2
# ===========================================================================

_names = st.text(
    alphabet=st.characters(min_codepoint=97, max_codepoint=122), min_size=1, max_size=5
)
_tool_pool = st.sampled_from(["read", "write", "search", "list", "call"])


@settings(max_examples=60, deadline=None)
@given(
    servers=st.lists(_names, min_size=1, max_size=5, unique=True),
    data=st.data(),
)
def test_p2_invoke_routes_to_addressed_session_only(
    servers: list[str], data: st.DataObject
) -> None:
    async def scenario() -> None:
        # Build an index with tool names deliberately colliding across servers.
        tools_by_server: dict[str, list[dict]] = {}
        for server in servers:
            names = data.draw(st.lists(_tool_pool, min_size=0, max_size=4, unique=True))
            tools_by_server[server] = [_tool(name) for name in names]

        index = _build_index(tools_by_server)
        entries = index.entries()
        if not entries:
            return  # nothing to route in this example

        target = data.draw(st.sampled_from(entries))
        manager = FakeManager(result="downstream-result")
        srv = _make_server(index, manager)

        result = await srv._handle_invoke_tool(
            {"server": target.server, "tool": target.tool, "arguments": {"q": 1}}
        )

        # Routed to exactly the addressed (server, tool) — never a colliding one.
        assert manager.calls == [(target.server, target.tool, {"q": 1})]
        # Passthrough (compress off) returns the downstream result byte-equal.
        assert result[0].text == "downstream-result"

    asyncio.run(scenario())


async def test_invoke_unknown_pair_is_not_dispatched() -> None:
    index = _build_index({"srv": [_tool("real")]})
    manager = FakeManager()
    server = _make_server(index, manager)

    payload = _payload(
        await server._handle_invoke_tool({"server": "srv", "tool": "missing", "arguments": {}})
    )
    assert payload["error"] == "tool not found"
    assert payload["server"] == "srv"
    assert payload["tool"] == "missing"
    # No dispatch happened for an unknown (server, tool).
    assert manager.calls == []


async def test_invoke_unavailable_server_returns_structured_error() -> None:
    index = _build_index({"srv": [_tool("t")]})
    manager = FakeManager(raise_exc=DownstreamUnavailableError("gone"))
    server = _make_server(index, manager)

    payload = _payload(
        await server._handle_invoke_tool({"server": "srv", "tool": "t", "arguments": {}})
    )
    assert payload["error"] == "server unavailable"
    assert payload["server"] == "srv"


async def test_invoke_timeout_is_classified_as_timeout() -> None:
    index = _build_index({"srv": [_tool("t")]})
    manager = FakeManager(raise_exc=asyncio.TimeoutError())
    server = _make_server(index, manager)

    payload = _payload(
        await server._handle_invoke_tool({"server": "srv", "tool": "t", "arguments": {}})
    )
    assert payload["kind"] == "timeout"
    assert payload["server"] == "srv"


async def test_invoke_downstream_error_is_classified() -> None:
    index = _build_index({"srv": [_tool("t")]})
    manager = FakeManager(raise_exc=RuntimeError("boom"))
    server = _make_server(index, manager)

    payload = _payload(
        await server._handle_invoke_tool({"server": "srv", "tool": "t", "arguments": {}})
    )
    assert payload["kind"] == "downstream_error"
    assert payload["server"] == "srv"


# ===========================================================================
# 7.8 — Property P4: result fidelity
# Validates: Requirements 7.3, 7.4, 7.5
# ===========================================================================


@settings(max_examples=80, deadline=None)
@given(downstream=st.text(max_size=400))
def test_p4a_passthrough_is_byte_equal(downstream: str) -> None:
    async def scenario() -> None:
        index = _build_index({"srv": [_tool("t")]})
        manager = FakeManager(result=downstream)
        # compress_results defaults off -> pure passthrough.
        server = _make_server(index, manager, compress_results=False)

        result = await server._handle_invoke_tool({"server": "srv", "tool": "t", "arguments": {}})
        assert len(result) == 1
        assert result[0].text == downstream

    asyncio.run(scenario())


async def test_p4b_compression_round_trip_retrieves_exact_original() -> None:
    # A moderately sized, non-secret result. compress_min_tokens=1 forces the
    # compression path for any non-empty result.
    original = "row data line\n" * 200
    index = _build_index({"srv": [_tool("bulk")]})
    manager = FakeManager(result=original)
    server = _make_server(index, manager, compress_results=True, compress_min_tokens=1)

    payload = _payload(
        await server._handle_invoke_tool({"server": "srv", "tool": "bulk", "arguments": {}})
    )

    assert payload.get("hash"), "compressed result must carry a non-empty hash"
    entry = get_compression_store().retrieve(payload["hash"])
    assert entry is not None
    # P4b: the stored original round-trips byte-equal.
    assert entry.original_content == original


# ===========================================================================
# 7.9 — Property P12: secret redaction on stored/logged results
# Validates: Requirement 12.7
# ===========================================================================


async def test_p12_stored_result_retrieval_log_redacts_secrets(caplog) -> None:
    secret_key = "sk-abcdef0123456789ABCDEF"
    bearer = "Bearer abcDEF123456ghiJKL789"
    api_line = 'api_key="TOPSECRETVALUE12345"'
    original = f"downstream payload with credentials\n{secret_key}\n{bearer}\n{api_line}\n" + (
        "padding line\n" * 100
    )

    index = _build_index({"srv": [_tool("leaky")]})
    manager = FakeManager(result=original)
    server = _make_server(index, manager, compress_results=True, compress_min_tokens=1)

    payload = _payload(
        await server._handle_invoke_tool({"server": "srv", "tool": "leaky", "arguments": {}})
    )
    assert payload.get("hash")

    store = get_compression_store()
    with caplog.at_level("INFO", logger="headroom.cache.compression_store"):
        entry = store.retrieve(payload["hash"])

    # The original (returned to a retriever) is preserved exactly...
    assert entry is not None
    assert entry.original_content == original

    # ...but the retrieval log emitted by the store carries no raw secrets.
    logged = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "event=headroom_retrieve" in logged
    assert secret_key not in logged
    assert "abcDEF123456ghiJKL789" not in logged
    assert "TOPSECRETVALUE12345" not in logged
    assert "[REDACTED]" in logged


# ===========================================================================
# 7.11 — list_servers inventory + health
# Validates: Requirements 8.1, 8.2, 8.3
# ===========================================================================


async def test_list_servers_reports_health_per_manager_status() -> None:
    index = _build_index({})
    manager = FakeManager(status_map={"healthy-one": "healthy", "sick": "unavailable"})
    server = _make_server(index, manager)

    payload = _payload(await server._handle_list_servers())
    # Sorted by server name.
    assert payload["servers"] == [
        {"server": "healthy-one", "health": "healthy"},
        {"server": "sick", "health": "unavailable"},
    ]


async def test_list_servers_empty_inventory_when_none_configured() -> None:
    index = _build_index({})
    manager = FakeManager(status_map={})
    server = _make_server(index, manager)

    payload = _payload(await server._handle_list_servers())
    assert payload["servers"] == []


# ===========================================================================
# Background retry of failed downstreams
# Validates: Requirement 2.9
#
# These drive a real DownstreamClientManager over a late-binding fake connector
# (no subprocess, no socket) and use a near-zero retry delay so the loop runs at
# test speed instead of the 15s production default.
# ===========================================================================


class RetrySession:
    """A fake downstream session advertising real tool dicts."""

    def __init__(self, name: str, tools: list[dict]) -> None:
        self.name = name
        self._tools = tools
        self.list_calls = 0
        self.calls: list[tuple[str, dict]] = []
        self.closed = False

    async def list_tools(self) -> list[dict]:
        self.list_calls += 1
        return list(self._tools)

    async def call_tool(self, tool: str, arguments: dict) -> str:
        self.calls.append((tool, arguments))
        return f"RESULT::{self.name}::{tool}"

    async def ping(self) -> None:
        return None

    async def aclose(self) -> None:
        self.closed = True


def _late_connector(sessions: dict[str, RetrySession]):
    """Connector that reads ``sessions`` at call time so tests can add entries."""

    async def connector(spec: DownstreamSpec) -> RetrySession:
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


def _retry_config(
    names: list[str],
    *,
    retry: bool = True,
    attempts: int = 3,
    delay: float = 0.0,
) -> GatewayConfigModel:
    return GatewayConfigModel(
        downstreams=[_stdio_downstream(name) for name in names],
        retry_failed_downstreams=retry,
        retry_max_attempts=attempts,
        retry_initial_delay_s=delay,
    )


async def _drain(task: asyncio.Task | None) -> None:
    """Await the retry task to completion (it never raises)."""
    if task is not None:
        await task


async def test_retry_recovers_downstream_and_rebuilds_index() -> None:
    sessions = {"good": RetrySession("good", [_tool("alpha", description="alpha tool")])}
    manager = DownstreamClientManager(connector=_late_connector(sessions))
    server = MCPGatewayServer(_retry_config(["good", "late"]), manager=manager)
    await server.start()

    # The failed downstream's tool is absent from the initial index.
    assert "late::omega" not in server._index
    assert server._retry_task is not None

    # "late" comes up on its own before the first retry pass fires.
    sessions["late"] = RetrySession("late", [_tool("omega", description="omega tool")])
    await _drain(server._retry_task)

    # Recovered: session established, out of the failed set.
    assert set(manager.healthy_servers()) == {"good", "late"}
    assert manager.failed_servers() == ()

    # Its tools are in the rebuilt index, so discovery and description see them.
    assert "late::omega" in server._index
    assert "good::alpha" in server._index
    described = _payload(await server._handle_describe_tool({"name": "late::omega"}))
    assert described["namespaced_id"] == "late::omega"
    found = _payload(await server._handle_find_tools({"query": "omega tool"}))
    assert "late::omega" in {e["namespaced_id"] for e in found["tools"]}
    # ...and it is now invokable through the rebuilt index.
    invoked = await server._handle_invoke_tool({"server": "late", "tool": "omega"})
    assert invoked[0].text == "RESULT::late::omega"

    await server.cleanup()


async def test_retry_does_not_disturb_healthy_sessions() -> None:
    healthy = RetrySession("good", [_tool("alpha", description="alpha tool")])
    sessions = {"good": healthy}
    manager = DownstreamClientManager(connector=_late_connector(sessions))
    server = MCPGatewayServer(_retry_config(["good", "late"]), manager=manager)
    await server.start()

    sessions["late"] = RetrySession("late", [_tool("omega", description="omega tool")])
    await _drain(server._retry_task)

    # Same session object, never closed, never re-listed...
    assert manager._sessions["good"] is healthy
    assert healthy.closed is False
    assert healthy.list_calls == 1
    # ...and still routable through the rebuilt index.
    invoked = await server._handle_invoke_tool({"server": "good", "tool": "alpha"})
    assert invoked[0].text == "RESULT::good::alpha"

    await server.cleanup()


async def test_retry_gives_up_after_max_attempts_and_gateway_keeps_serving() -> None:
    sessions = {"good": RetrySession("good", [_tool("alpha", description="alpha tool")])}
    manager = DownstreamClientManager(connector=_late_connector(sessions))
    server = MCPGatewayServer(_retry_config(["good", "dead"], attempts=2), manager=manager)
    await server.start()

    await _drain(server._retry_task)

    # The task finished on its own without raising; "dead" is still failed.
    assert server._retry_task is not None
    assert server._retry_task.done()
    assert server._retry_task.exception() is None
    assert manager.failed_servers() == ("dead",)

    # The gateway still serves the healthy downstream.
    found = _payload(await server._handle_find_tools({"query": "alpha tool"}))
    assert "good::alpha" in {e["namespaced_id"] for e in found["tools"]}
    invoked = await server._handle_invoke_tool({"server": "good", "tool": "alpha"})
    assert invoked[0].text == "RESULT::good::alpha"

    await server.cleanup()


async def test_permanently_failing_downstream_never_raises_out_of_retry_task() -> None:
    manager = DownstreamClientManager(connector=_late_connector({}))
    server = MCPGatewayServer(_retry_config(["dead"], attempts=3), manager=manager)
    await server.start()

    task = server._retry_task
    assert task is not None
    await _drain(task)

    assert task.done()
    assert task.cancelled() is False
    assert task.exception() is None
    assert manager.failed_servers() == ("dead",)

    await server.cleanup()


async def test_no_retry_task_created_when_nothing_failed() -> None:
    sessions = {"good": RetrySession("good", [_tool("alpha")])}
    manager = DownstreamClientManager(connector=_late_connector(sessions))
    server = MCPGatewayServer(_retry_config(["good"]), manager=manager)
    await server.start()

    assert server._retry_task is None

    await server.cleanup()


async def test_no_retry_task_created_when_retry_disabled_or_zero_attempts() -> None:
    for kwargs in ({"retry": False}, {"attempts": 0}):
        manager = DownstreamClientManager(connector=_late_connector({}))
        server = MCPGatewayServer(_retry_config(["dead"], **kwargs), manager=manager)
        await server.start()

        # A downstream failed, but retry is switched off, so no task exists.
        assert manager.failed_servers() == ("dead",)
        assert server._retry_task is None

        await server.cleanup()


async def test_cleanup_cancels_a_pending_retry_task() -> None:
    manager = DownstreamClientManager(connector=_late_connector({}))
    # A long first delay guarantees the task is still sleeping at cleanup time.
    server = MCPGatewayServer(_retry_config(["dead"], attempts=5, delay=30.0), manager=manager)
    await server.start()

    task = server._retry_task
    assert task is not None
    assert not task.done()

    await server.cleanup()

    # Cancelled and awaited, with nothing left pending to warn about at teardown.
    assert task.done()
    assert task.cancelled()
    assert server._retry_task is None
    # cleanup() is idempotent.
    await server.cleanup()
