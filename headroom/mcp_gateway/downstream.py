"""Downstream MCP client lifecycle for the MCP Gateway.

The gateway is simultaneously an MCP *server* (to the client, over stdio) and
an MCP *client* to N downstream servers. :class:`DownstreamClientManager` owns
that client side: it connects every configured downstream concurrently,
catalogs each one's tools exactly once, routes ``invoke_tool`` calls to the
correct session, reports health, and shuts everything down gracefully.

Design goals (see ``.kiro/specs/mcp-gateway/design.md``):

* **Isolation (Property P5).** One failing, erroring, or timing-out downstream
  must never take down aggregation. Every per-server operation is wrapped in
  its own ``try``/``except`` and an :func:`asyncio.wait_for` timeout, and
  :meth:`DownstreamClientManager.start` uses
  ``asyncio.gather(..., return_exceptions=True)`` so a failure is *partitioned*
  into ``AggregationResult.failed`` rather than raised.
* **Single aggregation listing (Property P8).** ``list_tools`` is called
  exactly once per established connection during aggregation.
* **Trust boundary (Requirement 12).** Downstreams are launched with their own
  configured ``command``/``args``/``env`` (stdio) or ``url``/``headers`` (http),
  unmodified — the same trust boundary as the client launching them directly.
  Those ``env``/``headers`` values are NEVER written to logs.

Testability
-----------
Real transports spawn subprocesses / open sockets, which is impractical for the
property and unit tests (P5/P8) that later tasks add. The manager therefore
takes a **session connector seam**: an injectable
``connector: SessionConnector`` factory that, given a :class:`DownstreamSpec`,
returns an established :class:`DownstreamSession`. Tests inject fake in-process
sessions; production uses :func:`connect_downstream_session`, which drives the
real ``mcp`` client APIs (``mcp.client.stdio.stdio_client`` +
``mcp.ClientSession`` for stdio, ``streamablehttp_client`` for http).

The ``mcp`` package is an optional dependency (installed via
``headroom-ai[proxy]`` / ``[mcp]``); it is imported lazily inside the real
connector so this module imports cleanly — and the fake-session seam works —
even when ``mcp`` is not installed.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, cast, runtime_checkable

from headroom.mcp_gateway.config import DownstreamSpec

if TYPE_CHECKING:  # pragma: no cover - typing only; mcp is optional at runtime
    from mcp.types import Tool

logger = logging.getLogger("headroom.mcp_gateway.downstream")

# --- Timeouts (seconds) -----------------------------------------------------
# Per task 3.1 / Requirements 2.1, 2.2, 8.2: 30s to establish a connection, 30s
# for the single aggregation ``list_tools`` response, 5s for a health check.
# The per-invocation call timeout defaults to the design's ``call_timeout_s``.
DEFAULT_CONNECT_TIMEOUT_S = 30.0
DEFAULT_RESPONSE_TIMEOUT_S = 30.0
DEFAULT_HEALTH_TIMEOUT_S = 5.0
DEFAULT_CALL_TIMEOUT_S = 120.0

# How long to wait for a session's worker task to unwind on shutdown before
# cancelling it outright.
_CLOSE_TIMEOUT_S = 10.0

#: Health status literals returned by :meth:`DownstreamClientManager.status`.
HEALTHY = "healthy"
UNAVAILABLE = "unavailable"


@runtime_checkable
class DownstreamSession(Protocol):
    """A single established downstream MCP session.

    This is the seam that decouples the manager from the concrete transport.
    Production uses :class:`_RealDownstreamSession`; tests inject fakes that
    implement this same surface in-process.
    """

    async def list_tools(self) -> list[Any]:
        """Return the downstream's advertised tools (``mcp.types.Tool`` list)."""
        ...

    async def call_tool(self, tool: str, arguments: dict[str, Any]) -> Any:
        """Invoke ``tool`` with ``arguments`` and return the raw result."""
        ...

    async def ping(self) -> None:
        """Round-trip a liveness check; raise if the session is unhealthy."""
        ...

    async def aclose(self) -> None:
        """Release all resources held by the session. Must be idempotent."""
        ...


#: Factory that establishes a session for one downstream (the injectable seam).
SessionConnector = Callable[[DownstreamSpec], Awaitable[DownstreamSession]]


class DownstreamUnavailableError(RuntimeError):
    """Raised by :meth:`DownstreamClientManager.call` for an unroutable server.

    A server is unroutable when it has no established session — either it was
    never configured/started, or it landed in ``AggregationResult.failed``.
    """


class _AggregationError(Exception):
    """Internal: carries a concise, secret-free failure reason for a server."""


@dataclass
class AggregationResult:
    """Outcome of connecting and cataloging every configured downstream.

    Every configured server appears in *exactly one* of ``ok`` or ``failed``
    (Property P5). A server enters ``ok`` only after its connection is
    established and its tools are listed; an established connection that returns
    an empty tool list still counts as healthy (Requirement 2.8).

    Attributes:
        ok: Healthy servers mapped to their advertised tool list. Tool objects
            are ``mcp.types.Tool`` in production and whatever a fake session
            returns in tests (stored verbatim, unindexed here).
        failed: Failed servers mapped to a concise, secret-free reason string.
    """

    ok: dict[str, list[Tool]] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)

    @property
    def healthy_servers(self) -> tuple[str, ...]:
        """Names of servers that connected and listed tools successfully."""
        return tuple(self.ok)

    @property
    def failed_servers(self) -> tuple[str, ...]:
        """Names of servers that failed to connect or list tools."""
        return tuple(self.failed)

    def server_count(self) -> int:
        """Total configured servers partitioned across ``ok`` and ``failed``."""
        return len(self.ok) + len(self.failed)


class DownstreamClientManager:
    """Owns the lifecycle of the N downstream MCP client sessions.

    Responsibilities:

    * :meth:`start` — connect every downstream concurrently, isolating
      failures, and list each healthy server's tools exactly once.
    * :meth:`retry_failed` — re-attempt only the currently-failed servers,
      without disturbing the sessions that are already established.
    * :meth:`call` — route ``call_tool`` to a single server's session.
    * :meth:`status` — per-server health with a bounded check.
    * :meth:`shutdown` — close every session gracefully.

    The manager is not re-entrant across event loops; create one per gateway
    process and drive it from a single loop.
    """

    def __init__(
        self,
        *,
        connector: SessionConnector | None = None,
        connect_timeout_s: float = DEFAULT_CONNECT_TIMEOUT_S,
        response_timeout_s: float = DEFAULT_RESPONSE_TIMEOUT_S,
        health_timeout_s: float = DEFAULT_HEALTH_TIMEOUT_S,
        call_timeout_s: float = DEFAULT_CALL_TIMEOUT_S,
    ) -> None:
        """Create a manager.

        Args:
            connector: Session factory seam. Defaults to
                :func:`connect_downstream_session` (real ``mcp`` transports).
                Tests inject a fake connector returning in-process sessions.
            connect_timeout_s: Per-server connection establishment timeout.
            response_timeout_s: Per-server ``list_tools`` response timeout.
            health_timeout_s: Per-server health-check timeout for :meth:`status`.
            call_timeout_s: Per-invocation timeout for :meth:`call`.
        """
        self._connector: SessionConnector = connector or connect_downstream_session
        self._connect_timeout_s = connect_timeout_s
        self._response_timeout_s = response_timeout_s
        self._health_timeout_s = health_timeout_s
        self._call_timeout_s = call_timeout_s

        # Established sessions keyed by server name (only healthy servers).
        self._sessions: dict[str, DownstreamSession] = {}
        # Every configured spec keyed by name, for status() inventory.
        self._specs: dict[str, DownstreamSpec] = {}
        # Reasons from the most recent aggregation pass, for failed servers.
        self._failed: dict[str, str] = {}
        # Tool list per established session, retained so a caller can rebuild a
        # complete ToolIndex after :meth:`retry_failed` recovers a server
        # without re-listing the servers that were already healthy (P8).
        self._tools_by_server: dict[str, list[Tool]] = {}

    # -- aggregation ---------------------------------------------------------

    async def start(self, specs: Sequence[DownstreamSpec]) -> AggregationResult:
        """Connect all downstreams concurrently and collect their tool lists.

        Preconditions: server names in ``specs`` are unique; each spec has a
        valid transport.

        Postconditions: returns an :class:`AggregationResult` that partitions
        every spec into exactly one of ``ok``/``failed``; a per-server failure
        (spawn, connect, timeout, or ``list_tools`` error) is captured in
        ``failed`` and never raised (Property P5). ``list_tools`` is invoked
        exactly once per established connection (Property P8).

        Calling ``start`` again first shuts down any previously established
        sessions so the manager can be safely restarted.
        """
        await self.shutdown()

        specs = list(specs)
        self._specs = {spec.name: spec for spec in specs}

        results = await asyncio.gather(
            *(self._connect_and_list(spec) for spec in specs),
            return_exceptions=True,
        )

        aggregation = AggregationResult()
        for spec, result in zip(specs, results):
            if isinstance(result, BaseException):
                reason = _failure_reason(result)
                aggregation.failed[spec.name] = reason
                logger.warning(
                    "event=downstream_unavailable server=%s reason=%s",
                    spec.name,
                    reason,
                )
                continue
            session, tools = result
            self._sessions[spec.name] = session
            self._tools_by_server[spec.name] = tools
            aggregation.ok[spec.name] = tools
            logger.info(
                "event=downstream_ready server=%s transport=%s tools=%d",
                spec.name,
                spec.transport,
                len(tools),
            )

        self._failed = dict(aggregation.failed)
        return aggregation

    async def _connect_and_list(self, spec: DownstreamSpec) -> tuple[DownstreamSession, list[Any]]:
        """Connect one downstream and list its tools once (bounded by timeouts).

        Raises :class:`_AggregationError` with a concise, secret-free reason on
        any failure so :meth:`start` can partition it into ``failed``. Never
        logs ``env``/``headers`` values (Requirement 12.9).
        """
        # 1) Establish the connection within the connect timeout.
        try:
            session = await asyncio.wait_for(self._connector(spec), timeout=self._connect_timeout_s)
        except asyncio.TimeoutError as exc:
            raise _AggregationError(
                f"connect timed out after {self._connect_timeout_s:.0f}s"
            ) from exc
        except _AggregationError:
            raise
        except Exception as exc:  # noqa: BLE001 - isolate every connect failure
            raise _AggregationError(f"connect failed: {type(exc).__name__}: {exc}") from exc

        # 2) List tools exactly once within the response timeout. On any
        #    failure, close the freshly opened session before reporting.
        try:
            tools = await asyncio.wait_for(session.list_tools(), timeout=self._response_timeout_s)
        except asyncio.TimeoutError as exc:
            await _safe_close(session)
            raise _AggregationError(
                f"list_tools timed out after {self._response_timeout_s:.0f}s"
            ) from exc
        except Exception as exc:  # noqa: BLE001 - isolate every listing failure
            await _safe_close(session)
            raise _AggregationError(f"list_tools failed: {type(exc).__name__}: {exc}") from exc

        return session, list(tools)

    async def retry_failed(self) -> AggregationResult:
        """Retry only the currently-failed servers, leaving healthy ones alone.

        Unlike :meth:`start` — which begins by shutting every session down so it
        can rebuild the whole snapshot — this performs a *delta* pass: it
        touches nothing but the servers currently in the failed set, so already
        established sessions keep their transports and stay routable throughout.

        A server that recovers gets its session stored, its tool list retained
        (see :meth:`tools_by_server`), and its entry dropped from the failed
        set. A server that fails again stays failed with an updated reason.

        Returns:
            An :class:`AggregationResult` describing *this pass only*: ``ok``
            holds the servers that recovered (mapped to their tool list) and
            ``failed`` holds the ones that are still failing. Both are empty
            when there was nothing to retry.

        Never raises (except :class:`asyncio.CancelledError`, which propagates
        so a cancelling caller can unwind cleanly). Recovery is logged at INFO;
        a continued failure is logged at DEBUG so repeated attempts against a
        permanently dead downstream do not spam the log. ``env``/``headers``
        values are never logged (Requirement 12.9).
        """
        outcome = AggregationResult()
        # Snapshot the names up front: the failed set is mutated below.
        names = [name for name in self._failed if name in self._specs]
        if not names:
            return outcome
        specs = [self._specs[name] for name in names]

        try:
            results = await asyncio.gather(
                *(self._connect_and_list(spec) for spec in specs),
                return_exceptions=True,
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a retry pass must never propagate
            logger.debug("event=downstream_retry_pass_error", exc_info=True)
            return outcome

        for spec, result in zip(specs, results):
            if isinstance(result, BaseException):
                reason = _failure_reason(result)
                self._failed[spec.name] = reason
                outcome.failed[spec.name] = reason
                logger.debug(
                    "event=downstream_retry_failed server=%s reason=%s",
                    spec.name,
                    reason,
                )
                continue
            session, tools = result
            self._sessions[spec.name] = session
            self._tools_by_server[spec.name] = tools
            self._failed.pop(spec.name, None)
            outcome.ok[spec.name] = tools
            logger.info(
                "event=downstream_recovered server=%s tools=%d",
                spec.name,
                len(tools),
            )

        return outcome

    # -- routing -------------------------------------------------------------

    async def call(self, server: str, tool: str, arguments: dict[str, Any]) -> Any:
        """Route ``call_tool(tool, arguments)`` to ``server``'s session only.

        The target session is selected solely from the explicit ``server``
        argument — never inferred from a bare tool name — so tools that share a
        name across servers can never be misrouted (Property P2 / Property P7).

        Raises:
            DownstreamUnavailableError: ``server`` has no established session.
            asyncio.TimeoutError: the call exceeded ``call_timeout_s``.
            Exception: any error raised by the downstream is propagated.
        """
        session = self._sessions.get(server)
        if session is None:
            raise DownstreamUnavailableError(server)
        return await asyncio.wait_for(
            session.call_tool(tool, arguments), timeout=self._call_timeout_s
        )

    # -- health --------------------------------------------------------------

    async def status(self) -> dict[str, str]:
        """Return per-server health: ``"healthy"`` or ``"unavailable"``.

        Every configured server is reported. A server with no established
        session is ``"unavailable"``; otherwise it is pinged within
        ``health_timeout_s`` and reported ``"unavailable"`` if the ping errors or
        does not return in time (Requirement 8.2). Health checks run
        concurrently and never raise.
        """
        names = list(self._specs) or list(self._sessions)

        async def _check(name: str) -> tuple[str, str]:
            session = self._sessions.get(name)
            if session is None:
                return name, UNAVAILABLE
            try:
                await asyncio.wait_for(session.ping(), timeout=self._health_timeout_s)
            except Exception:  # noqa: BLE001 - any failure => unavailable
                return name, UNAVAILABLE
            return name, HEALTHY

        pairs = await asyncio.gather(*(_check(name) for name in names))
        return dict(pairs)

    # -- teardown ------------------------------------------------------------

    async def shutdown(self) -> None:
        """Close every established session gracefully; never raises.

        Idempotent: safe to call when nothing is connected and safe to call
        multiple times.
        """
        sessions = list(self._sessions.values())
        self._sessions.clear()
        # The retained tool lists describe established sessions, so they die
        # with them; start() repopulates both from its fresh aggregation.
        self._tools_by_server.clear()
        if sessions:
            await asyncio.gather(
                *(_safe_close(session) for session in sessions),
                return_exceptions=True,
            )

    # -- introspection helpers ----------------------------------------------

    def healthy_servers(self) -> tuple[str, ...]:
        """Names of servers with an established session."""
        return tuple(self._sessions)

    def failed_servers(self) -> tuple[str, ...]:
        """Names of servers currently in the failed set."""
        return tuple(self._failed)

    def failure_reason(self, server: str) -> str | None:
        """Reason ``server`` is currently failed, if it is."""
        return self._failed.get(server)

    def tools_by_server(self) -> dict[str, list[Tool]]:
        """Current per-server tool lists across every established session.

        Reflects the last aggregation plus every :meth:`retry_failed` recovery,
        so a caller can rebuild a complete :class:`~headroom.mcp_gateway.index.ToolIndex`
        from it without re-listing tools on already-healthy servers (P8).
        """
        return {name: list(tools) for name, tools in self._tools_by_server.items()}


# --- helpers ----------------------------------------------------------------


def _failure_reason(exc: BaseException) -> str:
    """Render a concise, secret-free failure reason for a partitioned server."""
    if isinstance(exc, _AggregationError):
        return str(exc)
    if isinstance(exc, asyncio.CancelledError):
        return "cancelled"
    return f"{type(exc).__name__}: {exc}"


async def _safe_close(session: DownstreamSession) -> None:
    """Close a session, swallowing and logging any error."""
    try:
        await session.aclose()
    except Exception:  # noqa: BLE001 - shutdown must never propagate
        logger.debug("event=downstream_close_error", exc_info=True)


def _build_child_env(entry_env: Mapping[str, str]) -> dict[str, str]:
    """Compute the environment for a spawned stdio downstream.

    The child receives the gateway process's full environment
    (``PATH``/``HOME``/AWS creds/SSO tokens/...) overlaid by the entry's own ``env``
    overrides, so it is identical to the client launching that server directly
    (Requirement 12.1). Entry values win on key collisions. The returned mapping
    is never logged (Requirement 12.9).
    """
    return {**os.environ, **entry_env}


# --- real transport connector ----------------------------------------------


async def connect_downstream_session(spec: DownstreamSpec) -> DownstreamSession:
    """Establish a real downstream session using the ``mcp`` client APIs.

    This is the default :data:`SessionConnector`. It spawns stdio downstreams
    via ``mcp.client.stdio.stdio_client`` (using the configured
    ``command``/``args``/``env`` unmodified) and connects http downstreams via
    ``mcp.client.streamable_http.streamablehttp_client`` (using the configured
    ``url``/``headers`` unmodified) — the same trust boundary as the client
    launching them directly (Requirement 12.1/12.2).

    Raises:
        _AggregationError: the stdio spec has no runnable command (an empty or
            blank ``command``, e.g. a ``mcp.json`` entry that carries only
            ``disabled``). Guarded here so the empty string is never handed to
            the process spawner (which would surface as an opaque
            ``PermissionError: [Errno 13] Permission denied: ''``); the server
            is partitioned into ``failed`` with a concise reason instead.
        RuntimeError: the ``mcp`` package is not installed.
        ValueError: the spec's transport is unsupported or misconfigured.
        Exception: any error raised while spawning/connecting/initializing.
    """
    if spec.transport == "stdio" and (spec.stdio is None or not spec.stdio.command.strip()):
        # No runnable command: never spawn ``''``. Report a concise, secret-free
        # reason that ``DownstreamClientManager.start`` re-raises verbatim.
        raise _AggregationError("no runnable command")
    session = _RealDownstreamSession(spec)
    await session._connect()
    return session


class _RealDownstreamSession:
    """A real MCP session driven by a dedicated per-connection worker task.

    The ``mcp`` client transports are anyio async context managers whose cancel
    scopes are bound to the task that entered them; entering them in one task
    and exiting in another raises anyio "cancel scope in a different task"
    errors. To stay cancellation- and timeout-safe, this session runs the entire
    connection lifetime inside a single worker task that owns an
    :class:`~contextlib.AsyncExitStack`. Public methods enqueue requests to that
    task and await a future, so callers can wrap them in
    :func:`asyncio.wait_for` freely without corrupting the transport's scopes.
    """

    def __init__(self, spec: DownstreamSpec) -> None:
        self._spec = spec
        self._requests: asyncio.Queue[tuple[str, dict[str, Any], asyncio.Future]] = asyncio.Queue()
        self._worker: asyncio.Task[None] | None = None
        self._closed = False

    async def _connect(self) -> None:
        """Start the worker and block until the session is initialized."""
        loop = asyncio.get_running_loop()
        ready: asyncio.Future[None] = loop.create_future()
        self._worker = loop.create_task(self._run(ready))
        try:
            await ready
        except asyncio.CancelledError:
            # A connect timeout cancels us here; ensure the worker unwinds so no
            # orphaned session survives to (incorrectly) enter the healthy set.
            await self.aclose()
            raise

    async def _run(self, ready: asyncio.Future[None]) -> None:
        """Own the transport for its whole lifetime and service requests."""
        stack = AsyncExitStack()
        try:
            read, write = await self._open_streams(stack)
            from mcp import ClientSession  # lazy: mcp is optional

            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
        except BaseException as exc:  # noqa: BLE001 - report to _connect
            await _aclose_stack(stack)
            if not ready.done():
                ready.set_exception(exc)
            return

        if not ready.done():
            ready.set_result(None)

        try:
            while True:
                op, args, fut = await self._requests.get()
                if op == "close":
                    if not fut.done():
                        fut.set_result(None)
                    break
                try:
                    result = await self._dispatch(session, op, args)
                    if not fut.done():
                        fut.set_result(result)
                except BaseException as exc:  # noqa: BLE001 - relay to caller
                    if not fut.done():
                        fut.set_exception(exc)
        finally:
            await _aclose_stack(stack)

    async def _dispatch(self, session: Any, op: str, args: dict[str, Any]) -> Any:
        if op == "list_tools":
            return list((await session.list_tools()).tools)
        if op == "call_tool":
            return await session.call_tool(args["tool"], args["arguments"])
        if op == "ping":
            await session.send_ping()
            return None
        raise ValueError(f"unknown session op: {op!r}")

    async def _open_streams(self, stack: AsyncExitStack) -> tuple[Any, Any]:
        """Enter the transport context and return its (read, write) streams.

        ``env``/``headers`` are passed through unmodified and never logged.
        """
        spec = self._spec
        if spec.transport == "stdio":
            if spec.stdio is None:
                raise ValueError(f"stdio downstream {spec.name!r} has no stdio spec")
            from mcp import StdioServerParameters  # lazy: mcp is optional
            from mcp.client.stdio import stdio_client

            stdio = spec.stdio
            # Spawn with the gateway's full inherited environment overlaid by the
            # entry's env overrides, so the child's environment is identical to
            # the client launching the server directly (Requirement 12.1). Passing
            # only the entry env (or None) would strip PATH/HOME/AWS/SSO creds and
            # make the downstream hang until the connect timeout. Never logged
            # (Requirement 12.9).
            merged_env = _build_child_env(stdio.env)
            params = StdioServerParameters(
                command=stdio.command,
                args=list(stdio.args),
                env=merged_env,
            )
            read, write = await stack.enter_async_context(stdio_client(params))
            return read, write

        if spec.transport == "http":
            if not spec.url:
                raise ValueError(f"http downstream {spec.name!r} has no url")
            from mcp.client.streamable_http import (  # lazy: mcp is optional
                streamablehttp_client,
            )

            streams = await stack.enter_async_context(
                streamablehttp_client(spec.url, headers=dict(spec.headers) or None)
            )
            # streamablehttp_client yields (read, write, get_session_id).
            return streams[0], streams[1]

        raise ValueError(f"unsupported transport: {spec.transport!r}")

    async def _request(self, op: str, **args: Any) -> Any:
        if self._worker is None or self._worker.done():
            raise DownstreamUnavailableError(self._spec.name)
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Any] = loop.create_future()
        await self._requests.put((op, args, fut))
        return await fut

    async def list_tools(self) -> list[Any]:
        # ``_request`` is typed ``Any`` because its return shape depends on
        # ``op``; the ``list_tools`` worker branch always resolves the future
        # with a list of MCP Tool objects.
        return cast("list[Any]", await self._request("list_tools"))

    async def call_tool(self, tool: str, arguments: dict[str, Any]) -> Any:
        return await self._request("call_tool", tool=tool, arguments=arguments)

    async def ping(self) -> None:
        await self._request("ping")

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        worker = self._worker
        if worker is None:
            return
        if worker.done():
            return
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[None] = loop.create_future()
        try:
            self._requests.put_nowait(("close", {}, fut))
            await asyncio.wait_for(asyncio.shield(worker), timeout=_CLOSE_TIMEOUT_S)
        except Exception:  # noqa: BLE001 - force teardown on any hiccup
            worker.cancel()
            try:
                await worker
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass


async def _aclose_stack(stack: AsyncExitStack) -> None:
    try:
        await stack.aclose()
    except Exception:  # noqa: BLE001 - teardown best effort
        logger.debug("event=downstream_stack_close_error", exc_info=True)


__all__ = [
    "AggregationResult",
    "DownstreamClientManager",
    "DownstreamSession",
    "DownstreamUnavailableError",
    "SessionConnector",
    "connect_downstream_session",
    "HEALTHY",
    "UNAVAILABLE",
]
