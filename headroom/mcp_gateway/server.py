"""The MCP Gateway server and its four meta-tools.

The gateway is a single Headroom MCP **server** (to the client, over stdio) that
is simultaneously an MCP **client** to N downstream servers. It aggregates every
downstream tool *out of context* into a :class:`~headroom.mcp_gateway.index.ToolIndex`
and exposes only a fixed four-member meta-tool set:

* ``find_tools(query, limit)`` — ranked search returning the matching tools with
  their full, byte-equal input schemas.
* ``invoke_tool(server, tool, arguments)`` — routes the call to exactly the named
  downstream session and (optionally) compresses large results through the shared
  :class:`~headroom.cache.compression_store.CompressionStore`.
* ``describe_tool(name)`` — full schema for one downstream tool (by
  ``namespaced_id`` or a unique bare name).
* ``list_servers()`` — downstream inventory + health.

This module is modeled directly on :class:`~headroom.ccr.mcp_server.HeadroomMCPServer`
(stdio ``Server``, ``Tool``, ``TextContent``, the shared ``get_compression_store()``
singleton, client attribution, and the durable ``savings_ledger``). It reuses —
never reimplements — :mod:`headroom.compress`, the compression store, the
:class:`~headroom.mcp_gateway.downstream.DownstreamClientManager`, the tool index,
and :class:`~headroom.mcp_gateway.search.ToolSearch`.

``mcp`` is an optional dependency. As in ``ccr/mcp_server.py`` it is imported at
module top inside a ``try``/``except`` so importing this module never hard-fails
when ``mcp`` is absent; :class:`MCPGatewayServer.__init__` raises a clear
``ImportError`` only when actually instantiated without ``mcp`` installed. The
``_handle_*`` methods operate purely on injected instance state (``_index`` /
``_search`` / ``_manager``), so tests can drive them with fakes and never need
real stdio.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Mapping
from typing import Any

from headroom import savings_ledger
from headroom.cache.compression_store import (
    _redact_retrieval_log_payload,
    get_compression_store,
)
from headroom.mcp_gateway.config import RETRY_MAX_DELAY_S, GatewayConfigModel
from headroom.mcp_gateway.downstream import (
    DownstreamClientManager,
    DownstreamUnavailableError,
)
from headroom.mcp_gateway.index import ToolIndex
from headroom.mcp_gateway.search import ToolSearch

# Optional MCP SDK. Mirror headroom/ccr/mcp_server.py: import at module top and
# guard with a flag so this module imports cleanly even when ``mcp`` is absent
# (tests stub it). __init__ raises only when the class is actually constructed.
try:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import TextContent, Tool

    MCP_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only when mcp is uninstalled
    MCP_AVAILABLE = False
    Server = None  # type: ignore[assignment,misc]
    stdio_server = None  # type: ignore[assignment]
    TextContent = None  # type: ignore[assignment,misc]
    Tool = None  # type: ignore[assignment,misc]

logger = logging.getLogger("headroom.mcp_gateway.server")

# Meta-tool names — the entire client-facing surface (Property P1).
FIND_TOOLS = "find_tools"
INVOKE_TOOL = "invoke_tool"
DESCRIBE_TOOL = "describe_tool"
LIST_SERVERS = "list_servers"
META_TOOL_NAMES = (FIND_TOOLS, INVOKE_TOOL, DESCRIBE_TOOL, LIST_SERVERS)

# Session-scale TTL for stored originals, matching the MCP server convention.
GATEWAY_RESULT_TTL = 3600

# Model used only for token counting / context sizing during result compression
# (mirrors HeadroomMCPServer._compress_content).
_COMPRESS_MODEL = "claude-sonnet-4-5-20250929"

# Lazily-initialized token counter shared across invocations. Optional: falls
# back to a cheap char heuristic when tiktoken is unavailable (offline base
# install), so the compress-threshold decision never forces a heavy dependency.
_token_counter: Any = None
_token_counter_tried = False


def _estimate_tokens(text: str) -> int:
    """Estimate the token size of ``text`` for the compress threshold.

    Uses the existing :class:`TiktokenCounter` when available and falls back to
    a ``len // 4`` character heuristic otherwise. Never raises.
    """
    global _token_counter, _token_counter_tried
    if not _token_counter_tried:
        _token_counter_tried = True
        try:
            from headroom.tokenizers.tiktoken_counter import TiktokenCounter

            _token_counter = TiktokenCounter()
        except Exception:  # noqa: BLE001 - tiktoken is strictly optional
            _token_counter = None
    if _token_counter is not None:
        try:
            return int(_token_counter.count_text(text))
        except Exception:  # noqa: BLE001 - degrade to heuristic on any failure
            pass
    return max(1, len(text) // 4)


def _safe_redact(text: str) -> str | None:
    """Redact secrets/auth/API-keys for logging, reusing the store's redactor.

    Returns the redacted string, or ``None`` if redaction could not be applied
    (Requirement 12.8) so the caller can exclude the content from logs/storage.
    """
    try:
        return _redact_retrieval_log_payload(text)
    except Exception:  # noqa: BLE001 - never leak on a redaction failure
        return None


def _result_to_text(result: Any) -> str:
    """Render a downstream ``call_tool`` result to a deterministic text form.

    The same rendering is used for passthrough (P4a) and for the stored original
    (P4b), so a compressed result's ``retrieve(hash)`` round-trips byte-equal to
    what passthrough would have returned.

    * ``str`` → returned unchanged.
    * an object with a ``.content`` list (``mcp.types.CallToolResult``) →
      text blocks concatenated by newline; non-text blocks JSON-encoded.
    * ``dict`` / ``list`` → compact JSON.
    * anything else → ``str(result)``.
    """
    if isinstance(result, str):
        return result

    content = getattr(result, "content", None)
    if isinstance(content, (list, tuple)):
        parts: list[str] = []
        for block in content:
            text = getattr(block, "text", None)
            if isinstance(text, str):
                parts.append(text)
            elif isinstance(block, Mapping) and isinstance(block.get("text"), str):
                parts.append(block["text"])
            else:
                parts.append(json.dumps(block, ensure_ascii=False, default=str))
        return "\n".join(parts)

    if isinstance(result, (dict, list)):
        return json.dumps(result, ensure_ascii=False, default=str)
    return str(result)


class MCPGatewayServer:
    """The MCP server exposed to the client; owns the four meta-tools.

    Lifecycle:

    * :meth:`start` — connect downstreams (isolating failures) then build the
      :class:`ToolIndex` and its :class:`ToolSearch`; if anything failed and
      retry is enabled, spawn the background retry task.
    * :meth:`run_stdio` — serve the meta-tools over stdio (mirrors
      ``HeadroomMCPServer.run_stdio``).
    * :meth:`cleanup` — cancel the retry task, then shut down every downstream
      session.

    The ``_handle_*`` meta-tool handlers read only ``_index`` / ``_search`` /
    ``_manager`` / ``_config``, so tests can construct the server, assign those
    attributes with fakes, and drive the handlers directly without stdio.
    """

    def __init__(
        self,
        config: GatewayConfigModel,
        *,
        manager: DownstreamClientManager | None = None,
    ) -> None:
        """Create a gateway server.

        Args:
            config: The resolved gateway configuration.
            manager: Optional pre-built downstream manager (test seam). When
                omitted, :meth:`start` constructs one from ``config`` timeouts.
        """
        self._config = config
        self._manager: DownstreamClientManager | None = manager
        self._index: ToolIndex | None = None
        self._search: ToolSearch | None = None
        self._local_store: Any = None
        self._retry_task: asyncio.Task[None] | None = None

        if not MCP_AVAILABLE or Server is None:
            raise ImportError("MCP SDK not installed. Install with: pip install mcp")

        self.server: Server = Server("headroom-gateway")
        self._setup_handlers()

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        """Connect downstreams (isolating failures) and build the tool index.

        A per-server failure is partitioned into the aggregation's ``failed``
        set by the manager and never raises here (Property P5); only the
        healthy ``ok`` mapping is indexed, so unavailable servers' tools are
        naturally absent from discovery and routing.

        When downstreams did fail and retry is enabled, a background task is
        spawned to re-attempt just those servers. Readiness is never gated on
        it: this method returns as soon as the index over the healthy servers is
        built, so a dead downstream can neither block nor delay serving
        (Requirement 3 / Property P5).
        """
        if self._manager is None:
            self._manager = DownstreamClientManager(
                connect_timeout_s=self._config.connect_timeout_s,
                call_timeout_s=self._config.call_timeout_s,
            )
        aggregation = await self._manager.start(self._config.downstreams)

        index = ToolIndex()
        index.build(aggregation.ok)
        if index.errors:
            for message in index.errors:
                logger.warning("event=tool_index_excluded detail=%s", message)
        self._index = index
        self._search = ToolSearch(index)

        logger.info(
            "event=gateway_started healthy=%d failed=%d tools=%d",
            len(aggregation.ok),
            len(aggregation.failed),
            len(index),
        )

        self._maybe_start_retry(bool(aggregation.failed))

    # -- background retry of failed downstreams ------------------------------

    def _maybe_start_retry(self, has_failures: bool) -> None:
        """Spawn the background retry task, but only when it has work to do.

        No task is created when nothing failed, when retry is switched off, or
        when the attempt budget is below one — so the common all-healthy case
        (and every test that disables retry) leaves no task to leak.
        """
        if not has_failures:
            return
        if not self._config.retry_failed_downstreams:
            return
        if self._config.retry_max_attempts < 1:
            return
        self._retry_task = asyncio.create_task(self._retry_failed_downstreams())

    async def _retry_failed_downstreams(self) -> None:
        """Re-attempt failed downstreams with bounded attempts and backoff.

        Each pass sleeps *first* — a downstream that just failed will not have
        become healthy in the same instant, and an immediate retry would only
        double the startup cost. The delay then doubles per attempt, clamped by
        :data:`~headroom.mcp_gateway.config.RETRY_MAX_DELAY_S`.

        The loop exits early once the failed set is empty and otherwise stops
        after ``retry_max_attempts`` passes. It never lets an exception escape:
        this task is fire-and-forget, so a raise here would surface only as an
        unretrieved-exception warning and could not help anyone. Cancellation
        (from :meth:`cleanup`) propagates so the task unwinds cleanly.
        """
        manager = self._manager
        if manager is None:
            return

        delay = max(0.0, float(self._config.retry_initial_delay_s))
        attempts = int(self._config.retry_max_attempts)
        try:
            for attempt in range(1, attempts + 1):
                await asyncio.sleep(delay)
                delay = min(delay * 2.0, RETRY_MAX_DELAY_S)

                try:
                    outcome = await manager.retry_failed()
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - one bad pass must not end the loop
                    logger.debug(
                        "event=downstream_retry_pass_error attempt=%d", attempt, exc_info=True
                    )
                    continue

                if outcome.ok:
                    self._rebuild_index(manager, outcome.healthy_servers)

                if not manager.failed_servers():
                    logger.debug("event=downstream_retry_complete attempt=%d", attempt)
                    return
        except asyncio.CancelledError:
            # cleanup() cancels us; unwind without touching manager state.
            raise
        except Exception:  # noqa: BLE001 - a background task must never crash the gateway
            logger.debug("event=downstream_retry_loop_error", exc_info=True)

    def _rebuild_index(self, manager: DownstreamClientManager, recovered: tuple[str, ...]) -> None:
        """Rebuild the index over every healthy server and swap it in atomically.

        Both the new :class:`ToolIndex` and its new :class:`ToolSearch` are built
        completely *before* either attribute is reassigned, and there is NO
        ``await`` between the two assignments — so a concurrent ``find_tools`` /
        ``describe_tool`` / ``invoke_tool`` observes either the entirely old or
        the entirely new pair, never a half-updated one. The live index is never
        mutated in place, which is what makes that guarantee hold.
        """
        index = ToolIndex()
        index.build(manager.tools_by_server())
        if index.errors:
            for message in index.errors:
                logger.warning("event=tool_index_excluded detail=%s", message)
        search = ToolSearch(index)

        # Atomic swap: fully-built objects, no await between the assignments.
        self._index = index
        self._search = search

        logger.info(
            "event=tool_index_rebuilt recovered=%s tools=%d",
            ",".join(sorted(recovered)),
            len(index),
        )

    async def run_stdio(self) -> None:
        """Serve the meta-tools to the client over stdio."""
        async with stdio_server() as (read_stream, write_stream):
            logger.info("event=gateway_serving transport=stdio")
            await self.server.run(
                read_stream,
                write_stream,
                self.server.create_initialization_options(),
            )

    async def cleanup(self) -> None:
        """Cancel the retry task, then shut down all downstream sessions.

        The retry task is cancelled and awaited *before* the manager shuts down,
        so it can never outlive the sessions it touches and never leaves a
        pending task behind at loop teardown. Never raises.
        """
        task = self._retry_task
        self._retry_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 - teardown must never propagate
                logger.debug("event=gateway_retry_task_error", exc_info=True)

        if self._manager is not None:
            await self._manager.shutdown()

    # -- shared store --------------------------------------------------------

    def _get_local_store(self) -> Any:
        """Return the shared :class:`CompressionStore` singleton (lazy init)."""
        if self._local_store is None:
            self._local_store = get_compression_store()
        return self._local_store

    def _current_client(self) -> str:
        """Best-effort name of the MCP client driving this session."""
        override = os.environ.get("HEADROOM_MCP_CLIENT")
        if override:
            return override
        try:
            params = self.server.request_context.session.client_params
            info = getattr(params, "clientInfo", None) if params else None
            name = getattr(info, "name", None)
            if name:
                return str(name)
        except Exception:  # noqa: BLE001 - attribution is best-effort
            pass
        return "unknown"

    # -- handler registration ------------------------------------------------

    def _setup_handlers(self) -> None:
        """Register the ``list_tools`` (meta-only) and ``call_tool`` handlers."""

        @self.server.list_tools()
        async def list_tools() -> list[Tool]:
            # Property P1: exactly the four meta-tools, regardless of how many
            # (or zero) downstream tools are aggregated. No downstream schema
            # ever leaks into the always-on client context.
            return [
                Tool(
                    name=FIND_TOOLS,
                    description=(
                        "Search for the downstream tools you need and get back only the "
                        "matching ones, each with its full input schema. CALL THIS FIRST at "
                        "the start of a task instead of assuming a tool exists: describe what "
                        "you want to do (e.g. 'create a task', 'search documents') and "
                        "a small ranked set of relevant tools is returned. Then call "
                        "invoke_tool with the server + tool you picked."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": (
                                    "Natural-language description of the capability you need "
                                    "(matched against tool names and descriptions)."
                                ),
                            },
                            "limit": {
                                "type": "integer",
                                "minimum": 1,
                                "description": (
                                    "Maximum number of tools to return. Defaults to the "
                                    "gateway's configured find_limit_default when omitted."
                                ),
                            },
                        },
                        "required": ["query"],
                    },
                ),
                Tool(
                    name=INVOKE_TOOL,
                    description=(
                        "Invoke a specific downstream tool by explicit server and tool name "
                        "(as returned by find_tools/describe_tool) with its arguments. Routing "
                        "is by the explicit server+tool, never guessed from a bare name, so "
                        "tools that share a name across servers never collide. Large results "
                        "may be compressed and returned with a hash you can pass to "
                        "headroom_retrieve for the full original."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "server": {
                                "type": "string",
                                "description": "Downstream server name (from find_tools).",
                            },
                            "tool": {
                                "type": "string",
                                "description": "Downstream tool name (from find_tools).",
                            },
                            "arguments": {
                                "type": "object",
                                "description": "Arguments object passed to the downstream tool.",
                            },
                        },
                        "required": ["server", "tool"],
                    },
                ),
                Tool(
                    name=DESCRIBE_TOOL,
                    description=(
                        "Return the full description and input schema for one downstream tool. "
                        "Accepts a namespaced id ('server::tool') or a bare tool name when it is "
                        "unambiguous; an ambiguous bare name returns the matching namespaced ids "
                        "so you can disambiguate."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "name": {
                                "type": "string",
                                "description": (
                                    "A namespaced id 'server::tool' or a unique bare tool name."
                                ),
                            },
                        },
                        "required": ["name"],
                    },
                ),
                Tool(
                    name=LIST_SERVERS,
                    description=(
                        "List every configured downstream server and its health "
                        "('healthy' or 'unavailable'). Useful to see what the gateway is "
                        "fronting and which servers are currently reachable."
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {},
                        "required": [],
                    },
                ),
            ]

        @self.server.call_tool()
        async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
            started = time.perf_counter()
            logger.info("event=gateway_tool_call_received tool=%s", name)
            try:
                if name == FIND_TOOLS:
                    result = await self._handle_find_tools(arguments)
                elif name == INVOKE_TOOL:
                    result = await self._handle_invoke_tool(arguments)
                elif name == DESCRIBE_TOOL:
                    result = await self._handle_describe_tool(arguments)
                elif name == LIST_SERVERS:
                    result = await self._handle_list_servers()
                else:
                    result = _error(f"Unknown tool: {name}")
                logger.info(
                    "event=gateway_tool_call_completed tool=%s duration_ms=%.2f",
                    name,
                    (time.perf_counter() - started) * 1000.0,
                )
                return result
            except Exception as exc:  # noqa: BLE001 - never crash the gateway
                logger.error("event=gateway_tool_call_error tool=%s", name, exc_info=True)
                return _error(str(exc))

    # -- meta-tool handlers --------------------------------------------------

    async def _handle_find_tools(self, args: dict[str, Any]) -> list[TextContent]:
        """Ranked search returning matching tools with byte-equal schemas.

        Applies ``find_limit_default`` when ``limit`` is omitted, returns a
        structured error on an invalid ``limit`` (Requirement 5.7), and renders
        each entry with all of ``namespaced_id`` / ``server`` / ``tool`` /
        ``description`` / ``input_schema`` (Properties P9, P3). Tools from
        unavailable servers are already absent from the index, so they are
        naturally omitted (Requirement 3.3).
        """
        if self._search is None:
            return _error("gateway not started")

        query = args.get("query")
        if not isinstance(query, str):
            query = "" if query is None else str(query)

        limit = args.get("limit")
        if limit is None:
            limit = self._config.find_limit_default

        try:
            entries = self._search.search(query, limit)
        except ValueError as exc:
            return _error(f"invalid limit: {exc}", field="limit")

        results = [
            {
                "namespaced_id": entry.namespaced_id,
                "server": entry.server,
                "tool": entry.tool,
                "description": entry.description,
                "input_schema": entry.input_schema,
            }
            for entry in entries
        ]
        return _json({"tools": results, "count": len(results)})

    async def _handle_describe_tool(self, args: dict[str, Any]) -> list[TextContent]:
        """Return one tool's description + byte-equal schema.

        Resolves an exact ``namespaced_id`` first, then a unique bare tool name.
        Empty/whitespace names are rejected before any lookup (Requirement 6.5);
        an ambiguous bare name returns a disambiguation error listing the
        matching namespaced ids (6.3); no match returns a not-found error (6.4).
        """
        if self._index is None:
            return _error("gateway not started")

        name = args.get("name")
        if not isinstance(name, str) or not name.strip():
            return _error("a tool name is required", field="name")

        entry = self._index.get(name)
        if entry is None:
            matches = [e for e in self._index.entries() if e.tool == name]
            if len(matches) > 1:
                return _json(
                    {
                        "error": "ambiguous tool name; specify a namespaced_id",
                        "name": name,
                        "matches": sorted(e.namespaced_id for e in matches),
                    }
                )
            if len(matches) == 1:
                entry = matches[0]
            else:
                return _json({"error": "tool not found", "name": name})

        return _json(
            {
                "namespaced_id": entry.namespaced_id,
                "server": entry.server,
                "tool": entry.tool,
                "description": entry.description,
                "input_schema": entry.input_schema,
            }
        )

    async def _handle_invoke_tool(self, args: dict[str, Any]) -> list[TextContent]:
        """Route ``(server, tool)`` to its session only; optionally compress.

        The target session is taken from the explicit ``server``+``tool`` and
        never inferred from a bare name (Property P2). Passthrough is byte-equal
        when compression is off or the result is below ``compress_min_tokens``
        (P4a); otherwise the original is stored in the shared
        :class:`CompressionStore` and the compressed content is returned with a
        non-empty ``hash`` (P4b). Unknown tools, unavailable servers, downstream
        errors, and timeouts are all returned as structured ``{error, server,
        tool}`` content — never raised — so other sessions are unaffected
        (Requirements 7.6, 7.7, 3.5).
        """
        if self._index is None or self._manager is None:
            return _error("gateway not started")

        server = args.get("server")
        tool = args.get("tool")
        if not isinstance(server, str) or not server:
            return _json({"error": "server is required", "server": server, "tool": tool})
        if not isinstance(tool, str) or not tool:
            return _json({"error": "tool is required", "server": server, "tool": tool})

        arguments = args.get("arguments")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            return _json(
                {
                    "error": "arguments must be an object",
                    "server": server,
                    "tool": tool,
                }
            )

        namespaced_id = f"{server}::{tool}"
        if namespaced_id not in self._index:
            # Requirement 7.6: unknown (server, tool) — no dispatch, no raise.
            return _json({"error": "tool not found", "server": server, "tool": tool})

        try:
            result = await self._manager.call(server, tool, arguments)
        except DownstreamUnavailableError:
            # Requirement 3.5: target server unavailable.
            return _json({"error": "server unavailable", "server": server, "tool": tool})
        except asyncio.TimeoutError:
            # Requirement 7.7: classified as a timeout.
            return _json({"error": "timeout", "kind": "timeout", "server": server, "tool": tool})
        except Exception as exc:  # noqa: BLE001 - downstream errors don't crash us
            # Requirement 7.7: classified as a downstream error.
            return _json(
                {
                    "error": f"downstream error: {type(exc).__name__}: {exc}",
                    "kind": "downstream_error",
                    "server": server,
                    "tool": tool,
                }
            )

        return self._render_invocation_result(server, tool, result)

    def _render_invocation_result(self, server: str, tool: str, result: Any) -> list[TextContent]:
        """Passthrough or compress a successful downstream result."""
        text = _result_to_text(result)

        compress_on = bool(self._config.compress_results)
        if not compress_on or _estimate_tokens(text) < self._config.compress_min_tokens:
            # P4a: byte-equal passthrough. Log only a redacted preview.
            redacted = _safe_redact(text)
            if redacted is not None:
                logger.info(
                    "event=gateway_invoke_passthrough server=%s tool=%s chars=%d",
                    server,
                    tool,
                    len(text),
                )
            return [TextContent(type="text", text=text)]

        # Requirement 12.8: if the result cannot be redacted, exclude it from
        # logs and stored output entirely rather than risk leaking a secret.
        if _safe_redact(text) is None:
            return _json(
                {
                    "error": "result excluded: could not be redacted for safe storage",
                    "server": server,
                    "tool": tool,
                }
            )

        try:
            payload = self._compress_and_store(text, server, tool)
        except Exception:  # noqa: BLE001 - fall back to passthrough on failure
            logger.debug("event=gateway_compress_failed server=%s tool=%s", server, tool)
            return [TextContent(type="text", text=text)]

        try:
            self._record_savings(payload)
        except Exception:  # noqa: BLE001 - savings bookkeeping never breaks the tool
            logger.debug("durable savings recording failed", exc_info=True)

        return _json(payload)

    def _compress_and_store(self, text: str, server: str, tool: str) -> dict[str, Any]:
        """Compress ``text`` and store the verbatim original for retrieval.

        Mirrors ``HeadroomMCPServer._compress_content``: the original is stored
        byte-equal in the shared :class:`CompressionStore` so ``retrieve(hash)``
        round-trips exactly (P4b); the store's own retrieval logging applies the
        secret redaction (Requirement 12.7).
        """
        from headroom.compress import compress

        messages = [{"role": "tool", "content": text}]
        result = compress(messages, model=_COMPRESS_MODEL)

        compressed_content = result.messages[0].get("content", text)
        if not isinstance(compressed_content, str):
            compressed_content = json.dumps(compressed_content, ensure_ascii=False)

        input_tokens = result.tokens_before
        output_tokens = result.tokens_after

        store = self._get_local_store()
        hash_key = store.store(
            original=text,
            compressed=compressed_content,
            original_tokens=input_tokens,
            compressed_tokens=output_tokens,
            tool_name=f"{server}::{tool}",
            compression_strategy="gateway_invoke",
            ttl=GATEWAY_RESULT_TTL,
        )

        savings_pct = round((1 - output_tokens / input_tokens) * 100, 1) if input_tokens > 0 else 0
        return {
            "server": server,
            "tool": tool,
            "compressed": compressed_content,
            "hash": hash_key,
            "original_tokens": input_tokens,
            "compressed_tokens": output_tokens,
            "tokens_saved": max(0, input_tokens - output_tokens),
            "savings_percent": savings_pct,
            "note": (
                f"Result compressed; original stored with hash={hash_key}. "
                "Use headroom_retrieve to get the full content."
            ),
        }

    def _record_savings(self, payload: dict[str, Any]) -> None:
        """Append a durable savings event for a compressed result."""
        try:
            before = int(payload.get("original_tokens", 0) or 0)
            after = int(payload.get("compressed_tokens", 0) or 0)
        except (TypeError, ValueError):
            return
        if before <= after:
            return
        savings_ledger.record_savings_event(
            tokens_before=before,
            tokens_after=after,
            model=os.environ.get("HEADROOM_MCP_MODEL"),
            client=self._current_client(),
            source="mcp",
        )

    async def _handle_list_servers(self) -> list[TextContent]:
        """Return the downstream inventory with health (Requirement 8).

        Uses the manager's 5s-bounded health check. Returns an empty inventory
        when no downstreams are configured (Requirement 8.3).
        """
        if self._manager is None:
            return _json({"servers": []})

        status = await self._manager.status()
        servers = [{"server": name, "health": status[name]} for name in sorted(status)]
        return _json({"servers": servers})


def _json(payload: dict[str, Any]) -> list[TextContent]:
    """Wrap a structured payload as a single JSON ``TextContent`` block."""
    return [TextContent(type="text", text=json.dumps(payload, indent=2, default=str))]


def _error(message: str, *, field: str | None = None) -> list[TextContent]:
    """Return a structured ``{error: ...}`` ``TextContent`` block."""
    payload: dict[str, Any] = {"error": message}
    if field is not None:
        payload["field"] = field
    return _json(payload)


__all__ = ["MCPGatewayServer", "META_TOOL_NAMES"]
