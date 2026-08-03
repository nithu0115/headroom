"""Tests for :mod:`headroom.mcp_gateway.downstream`.

These exercise the :class:`DownstreamClientManager` entirely through the
injectable session-connector seam with fake in-process sessions, so no
subprocess or socket is spawned and the ``mcp`` package is not required.

Covers:
* Property P5 (isolation): a random subset of failing downstreams is
  partitioned into ``failed`` without raising; healthy servers stay routable.
* Property P8 (single aggregation listing): ``list_tools`` is called exactly
  once per healthy server.
* Routing (Property P2 groundwork): ``call`` reaches only the addressed
  server's session, even when tool names collide across servers.
* Background retry (Requirement 2.9): ``retry_failed`` recovers a downstream
  that became healthy after aggregation, retains its tools, and leaves the
  already-established sessions untouched.
"""

from __future__ import annotations

import asyncio

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from headroom.mcp_gateway.config import DownstreamSpec
from headroom.mcp_gateway.downstream import (
    HEALTHY,
    UNAVAILABLE,
    AggregationResult,
    DownstreamClientManager,
    DownstreamUnavailableError,
)
from headroom.mcp_registry.base import ServerSpec


class FakeSession:
    """A fake in-process downstream session for tests."""

    def __init__(
        self,
        name: str,
        *,
        tools: list[str] | None = None,
        fail_on_list: bool = False,
        ping_ok: bool = True,
    ) -> None:
        self.name = name
        self._tools = list(tools or [])
        self._fail_on_list = fail_on_list
        self._ping_ok = ping_ok
        self.list_calls = 0
        self.calls: list[tuple[str, dict]] = []
        self.closed = False

    async def list_tools(self) -> list[str]:
        self.list_calls += 1
        if self._fail_on_list:
            raise RuntimeError("boom-list")
        return list(self._tools)

    async def call_tool(self, tool: str, arguments: dict) -> dict:
        self.calls.append((tool, arguments))
        return {"server": self.name, "tool": tool, "arguments": arguments}

    async def ping(self) -> None:
        if not self._ping_ok:
            raise RuntimeError("ping-failed")

    async def aclose(self) -> None:
        self.closed = True


def _stdio_spec(name: str) -> DownstreamSpec:
    return DownstreamSpec(
        name=name,
        transport="stdio",
        stdio=ServerSpec(name=name, command="cmd", args=(), env={}),
    )


def _connector_from(sessions: dict[str, FakeSession]):
    async def connector(spec: DownstreamSpec) -> FakeSession:
        session = sessions.get(spec.name)
        if session is None:
            raise RuntimeError(f"connect refused for {spec.name}")
        return session

    return connector


async def test_start_partitions_ok_and_failed_without_raising() -> None:
    sessions = {
        "ok1": FakeSession("ok1", tools=["a", "b"]),
        "ok2": FakeSession("ok2", tools=[]),  # empty list still healthy
        "bad_list": FakeSession("bad_list", fail_on_list=True),
    }
    specs = [
        _stdio_spec("ok1"),
        _stdio_spec("ok2"),
        _stdio_spec("bad_list"),
        _stdio_spec("no_connect"),  # not in sessions => connect fails
    ]
    mgr = DownstreamClientManager(connector=_connector_from(sessions))
    result = await mgr.start(specs)

    assert isinstance(result, AggregationResult)
    # every server partitioned into exactly one set
    assert set(result.ok) | set(result.failed) == {"ok1", "ok2", "bad_list", "no_connect"}
    assert not (set(result.ok) & set(result.failed))
    assert result.ok["ok1"] == ["a", "b"]
    assert result.ok["ok2"] == []  # established + empty => healthy
    assert "bad_list" in result.failed
    assert "no_connect" in result.failed
    await mgr.shutdown()


async def test_list_tools_called_exactly_once_per_healthy_server() -> None:
    sessions = {"s": FakeSession("s", tools=["x"])}
    mgr = DownstreamClientManager(connector=_connector_from(sessions))
    await mgr.start([_stdio_spec("s")])
    assert sessions["s"].list_calls == 1
    await mgr.shutdown()


async def test_call_routes_to_named_session_only() -> None:
    # two servers exposing the SAME tool name must not collide
    sessions = {
        "alpha": FakeSession("alpha", tools=["search"]),
        "beta": FakeSession("beta", tools=["search"]),
    }
    mgr = DownstreamClientManager(connector=_connector_from(sessions))
    await mgr.start([_stdio_spec("alpha"), _stdio_spec("beta")])

    out = await mgr.call("beta", "search", {"q": 1})
    assert out["server"] == "beta"
    assert sessions["beta"].calls == [("search", {"q": 1})]
    assert sessions["alpha"].calls == []
    await mgr.shutdown()


async def test_call_unknown_server_raises_unavailable() -> None:
    mgr = DownstreamClientManager(connector=_connector_from({}))
    await mgr.start([])
    with pytest.raises(DownstreamUnavailableError):
        await mgr.call("nope", "t", {})


async def test_status_reports_health() -> None:
    sessions = {
        "healthy": FakeSession("healthy", tools=[]),
        "sick": FakeSession("sick", tools=[], ping_ok=False),
    }
    mgr = DownstreamClientManager(connector=_connector_from(sessions))
    await mgr.start([_stdio_spec("healthy"), _stdio_spec("sick"), _stdio_spec("gone")])
    status = await mgr.status()
    assert status["healthy"] == HEALTHY
    assert status["sick"] == UNAVAILABLE  # ping raised
    assert status["gone"] == UNAVAILABLE  # never connected
    await mgr.shutdown()


async def test_shutdown_closes_all_sessions() -> None:
    sessions = {"a": FakeSession("a", tools=[]), "b": FakeSession("b", tools=[])}
    mgr = DownstreamClientManager(connector=_connector_from(sessions))
    await mgr.start([_stdio_spec("a"), _stdio_spec("b")])
    await mgr.shutdown()
    assert sessions["a"].closed and sessions["b"].closed


# --- Property P5: isolation over random failure subsets ---------------------


@settings(max_examples=60, deadline=None)
@given(
    servers=st.lists(
        st.tuples(
            st.text(
                alphabet=st.characters(min_codepoint=97, max_codepoint=122), min_size=1, max_size=6
            ),
            st.sampled_from(["ok", "fail_connect", "fail_list"]),
        ),
        min_size=0,
        max_size=6,
        unique_by=lambda t: t[0],
    )
)
def test_p5_isolation_partition(servers: list[tuple[str, str]]) -> None:
    async def scenario() -> None:
        sessions: dict[str, FakeSession] = {}
        for name, kind in servers:
            if kind == "ok":
                sessions[name] = FakeSession(name, tools=["t"])
            elif kind == "fail_list":
                sessions[name] = FakeSession(name, fail_on_list=True)
            # fail_connect => absent from sessions => connector raises

        mgr = DownstreamClientManager(connector=_connector_from(sessions))
        specs = [_stdio_spec(name) for name, _ in servers]
        result = await mgr.start(specs)

        all_names = {name for name, _ in servers}
        # exactly-one-set partition, never raises
        assert set(result.ok) | set(result.failed) == all_names
        assert not (set(result.ok) & set(result.failed))
        # only "ok" servers are healthy and routable
        for name, kind in servers:
            if kind == "ok":
                assert name in result.ok
                out = await mgr.call(name, "t", {})
                assert out["server"] == name
            else:
                assert name in result.failed
                with pytest.raises(DownstreamUnavailableError):
                    await mgr.call(name, "t", {})
        await mgr.shutdown()

    asyncio.run(scenario())


# --- _build_child_env: full-environment inheritance for spawned stdio -------


def test_build_child_env_inherits_parent_environment(monkeypatch) -> None:
    """The child env contains the gateway process's own environment (Req 12.1)."""
    from headroom.mcp_gateway.downstream import _build_child_env

    monkeypatch.setenv("HR_SENTINEL", "1")

    env = _build_child_env({})

    assert env.get("HR_SENTINEL") == "1"
    # An empty entry env still yields the inherited (non-empty) environment.
    assert env


def test_build_child_env_entry_overrides_win(monkeypatch) -> None:
    """Entry env values override inherited ones on key collisions (Req 12.1)."""
    from headroom.mcp_gateway.downstream import _build_child_env

    monkeypatch.setenv("HR_SENTINEL", "1")

    env = _build_child_env({"HR_SENTINEL": "override"})

    assert env["HR_SENTINEL"] == "override"


# --- retry_failed: recovering a downstream that became healthy later --------


def _mutable_connector(sessions: dict[str, FakeSession]):
    """A connector that reads ``sessions`` at call time, so tests can add to it.

    Mirrors :func:`_connector_from` but is deliberately late-binding: a test can
    register a session for a name *after* the first aggregation refused it,
    standing in for a downstream that becomes healthy on its own (a container
    started, a cold ``uv``/``npx`` fetch finishing, creds refreshed).
    """

    async def connector(spec: DownstreamSpec) -> FakeSession:
        session = sessions.get(spec.name)
        if session is None:
            raise RuntimeError(f"connect refused for {spec.name}")
        return session

    return connector


async def test_retry_failed_recovers_server_and_retains_tools() -> None:
    sessions = {"good": FakeSession("good", tools=["a"])}
    mgr = DownstreamClientManager(connector=_mutable_connector(sessions))

    first = await mgr.start([_stdio_spec("good"), _stdio_spec("late")])
    assert set(first.ok) == {"good"}
    assert "late" in first.failed
    assert mgr.tools_by_server() == {"good": ["a"]}

    # "late" comes up on its own between aggregation and the retry pass.
    sessions["late"] = FakeSession("late", tools=["x", "y"])
    outcome = await mgr.retry_failed()

    assert set(outcome.ok) == {"late"}
    assert outcome.ok["late"] == ["x", "y"]
    assert outcome.failed == {}
    # It left the failed set and is now routable.
    assert mgr.failed_servers() == ()
    assert mgr.failure_reason("late") is None
    assert set(mgr.healthy_servers()) == {"good", "late"}
    routed = await mgr.call("late", "x", {"k": 1})
    assert routed["server"] == "late"
    # The retained tool map now covers both servers, so a caller can rebuild a
    # complete index without re-listing "good" (P8).
    assert mgr.tools_by_server() == {"good": ["a"], "late": ["x", "y"]}
    assert sessions["good"].list_calls == 1

    await mgr.shutdown()


async def test_retry_failed_does_not_disturb_healthy_sessions() -> None:
    healthy = FakeSession("good", tools=["a"])
    sessions = {"good": healthy}
    mgr = DownstreamClientManager(connector=_mutable_connector(sessions))
    await mgr.start([_stdio_spec("good"), _stdio_spec("late")])

    sessions["late"] = FakeSession("late", tools=["x"])
    await mgr.retry_failed()

    # Same session object still installed, never closed, never re-listed.
    assert mgr._sessions["good"] is healthy
    assert healthy.closed is False
    assert healthy.list_calls == 1
    # ...and still routable.
    out = await mgr.call("good", "a", {})
    assert out["server"] == "good"

    await mgr.shutdown()


async def test_retry_failed_keeps_permanently_failing_server_failed() -> None:
    mgr = DownstreamClientManager(connector=_mutable_connector({}))
    await mgr.start([_stdio_spec("dead")])
    assert mgr.failed_servers() == ("dead",)

    outcome = await mgr.retry_failed()

    assert outcome.ok == {}
    assert "dead" in outcome.failed
    assert mgr.failed_servers() == ("dead",)
    assert mgr.failure_reason("dead") is not None
    assert mgr.tools_by_server() == {}

    # Repeated passes stay stable and never raise.
    again = await mgr.retry_failed()
    assert "dead" in again.failed
    assert mgr.failed_servers() == ("dead",)


async def test_retry_failed_is_a_noop_with_no_failures() -> None:
    sessions = {"good": FakeSession("good", tools=["a"])}
    mgr = DownstreamClientManager(connector=_mutable_connector(sessions))
    await mgr.start([_stdio_spec("good")])

    outcome = await mgr.retry_failed()

    assert outcome.ok == {}
    assert outcome.failed == {}
    assert sessions["good"].list_calls == 1  # no extra aggregation listing

    await mgr.shutdown()


async def test_retry_failed_recovers_only_a_subset() -> None:
    sessions = {"good": FakeSession("good", tools=["a"])}
    mgr = DownstreamClientManager(connector=_mutable_connector(sessions))
    await mgr.start([_stdio_spec("good"), _stdio_spec("late"), _stdio_spec("dead")])
    assert set(mgr.failed_servers()) == {"late", "dead"}

    sessions["late"] = FakeSession("late", tools=["x"])
    outcome = await mgr.retry_failed()

    assert set(outcome.ok) == {"late"}
    assert set(outcome.failed) == {"dead"}
    assert mgr.failed_servers() == ("dead",)
    assert set(mgr.healthy_servers()) == {"good", "late"}

    await mgr.shutdown()


async def test_shutdown_clears_retained_tool_map() -> None:
    sessions = {"good": FakeSession("good", tools=["a"])}
    mgr = DownstreamClientManager(connector=_mutable_connector(sessions))
    await mgr.start([_stdio_spec("good")])
    assert mgr.tools_by_server() == {"good": ["a"]}

    await mgr.shutdown()

    assert mgr.tools_by_server() == {}
