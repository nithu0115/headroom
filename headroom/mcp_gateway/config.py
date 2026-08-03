"""Configuration models for the MCP Gateway.

The gateway aggregates N downstream MCP servers behind a single stdio server.
This module defines the config data models:

* :class:`DownstreamSpec` — one downstream server entry. It *composes* the
  existing :class:`~headroom.mcp_registry.base.ServerSpec` for stdio transport
  (reusing ``command``/``args``/``env`` verbatim) rather than redefining it,
  and adds ``url``/``headers`` for http transport.
* :class:`GatewayConfigModel` — the resolved gateway configuration plus
  behavior toggles.

Config resolution (:func:`resolve_gateway_config`) selects the first available
source in a fixed priority order and reuses :mod:`headroom.mcp_registry.kiro`'s
JSON read helpers and ``mcpServers`` entry shape so stdio downstream entries map
onto the existing schema without a second parser.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from headroom.mcp_registry.base import ServerSpec
from headroom.mcp_registry.gateway import GATEWAY_SERVER_NAME
from headroom.mcp_registry.kiro import _entry_to_spec, _kiro_config_path, _read_json

#: Upper bound on the background retry backoff. The delay doubles after each
#: attempt and is clamped here, so a long-lived session never waits absurdly
#: long between attempts. Deliberately a constant rather than a fourth knob.
RETRY_MAX_DELAY_S = 120.0


@dataclass
class DownstreamSpec:
    """A single downstream MCP server to aggregate.

    ``ServerSpec`` already covers stdio (``command``/``args``/``env``); http
    downstreams need a URL, so this type composes ``ServerSpec`` rather than
    mutating it.

    Attributes:
        name: Downstream server name.
        transport: ``"stdio"`` or ``"http"``.
        stdio: Reused verbatim for ``transport == "stdio"``; ``None`` otherwise.
        url: Endpoint for ``transport == "http"``; ``None`` otherwise.
        headers: HTTP headers for ``transport == "http"``.
    """

    name: str
    transport: str
    stdio: ServerSpec | None = None
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class GatewayConfigModel:
    """Resolved gateway configuration and behavior toggles.

    Attributes:
        downstreams: Downstream servers to aggregate.
        include: If set, only these server names are kept.
        exclude: Server names to drop (always includes the gateway itself).
        find_limit_default: Default ``limit`` for ``find_tools``.
        compress_results: Opt-in compression of large downstream results.
        compress_min_tokens: Only compress results above this token count.
        connect_timeout_s: Per-downstream connect timeout (seconds).
        call_timeout_s: Per-invocation call timeout (seconds).
        retry_failed_downstreams: Retry downstreams that failed aggregation in
            the background, so one that becomes healthy later (cold ``uv``/``npx``
            fetch, a container started after launch, refreshed creds) is picked
            up without restarting the gateway.
        retry_max_attempts: Maximum number of background retry passes. ``0``
            disables retry as surely as ``retry_failed_downstreams=False``.
        retry_initial_delay_s: Delay before the first retry pass. Doubles after
            each attempt, clamped by :data:`RETRY_MAX_DELAY_S`.
    """

    downstreams: list[DownstreamSpec]
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    find_limit_default: int = 5
    compress_results: bool = False
    compress_min_tokens: int = 1000
    connect_timeout_s: float = 30.0
    call_timeout_s: float = 120.0
    retry_failed_downstreams: bool = True
    retry_max_attempts: int = 3
    retry_initial_delay_s: float = 15.0


def _candidate_config_paths() -> list[Path]:
    """Return the config source candidates in resolution priority order.

    Priority:

    1. The path named by the ``HEADROOM_GATEWAY_CONFIG`` environment variable
       (a value with at least one non-whitespace character), ``~``-expanded.
    2. The dedicated ``~/.kiro/settings/headroom-gateway.json``.
    3. The client's Kiro ``mcp.json``, resolved via
       :func:`~headroom.mcp_registry.kiro._kiro_config_path` so the
       ``KIRO_MCP_CONFIG`` override is honored exactly as ``KiroRegistrar``
       honors it.
    """
    paths: list[Path] = []
    env_path = os.environ.get("HEADROOM_GATEWAY_CONFIG", "").strip()
    if env_path:
        paths.append(Path(env_path).expanduser().absolute())
    paths.append(Path.home() / ".kiro" / "settings" / "headroom-gateway.json")
    paths.append(_kiro_config_path())
    return paths


def _select_source(candidates: list[Path]) -> Path | None:
    """Return the first candidate that exists on disk, or ``None``."""
    for path in candidates:
        if path.exists():
            return path
    return None


def _entry_to_downstream(name: str, entry: dict[str, Any]) -> DownstreamSpec | None:
    """Map one ``mcpServers`` entry to a :class:`DownstreamSpec`.

    stdio entries (the common Kiro/Claude shape) are read with the same
    :func:`~headroom.mcp_registry.kiro._entry_to_spec` helper ``KiroRegistrar``
    uses, so ``command``/``args``/``env`` parse identically. Entries carrying a
    ``url`` (and no ``command``) are treated as forward-looking http downstreams
    per the design's :class:`DownstreamSpec` shape.

    Entries are skipped (return ``None``) only when they are:

    * non-dict entries, or
    * unlaunchable — they have neither a runnable ``command`` nor a ``url``
      (spawning an empty command surfaces as an opaque
      ``PermissionError: [Errno 13] Permission denied: ''``).

    A ``disabled`` flag from Kiro does **not** exclude a server: the gateway is
    designed to front servers the user has disabled in the client's own context
    (they disable the downstream in ``mcp.json`` to drop it from Kiro's context
    while the gateway keeps fronting it), so a disabled-but-launchable entry
    still resolves to a downstream.
    """
    if not isinstance(entry, dict):
        return None

    url_value = entry.get("url")
    has_command = bool(str(entry.get("command", "")).strip())
    if isinstance(url_value, str) and url_value and not has_command:
        headers_value = entry.get("headers", {})
        headers: dict[str, str] = {}
        if isinstance(headers_value, dict):
            headers = {str(k): str(v) for k, v in headers_value.items()}
        return DownstreamSpec(name=name, transport="http", url=url_value, headers=headers)

    # No runnable command and no url: unlaunchable, so skip rather than exec ''.
    if not has_command:
        return None

    return DownstreamSpec(
        name=name,
        transport="stdio",
        stdio=_entry_to_spec(name, entry),
    )


def _names_from(value: Any) -> tuple[str, ...]:
    """Coerce a config value into a tuple of server-name strings."""
    if isinstance(value, (list, tuple)):
        return tuple(str(x) for x in value)
    return ()


def resolve_gateway_config() -> GatewayConfigModel:
    """Resolve the gateway configuration from the first available source.

    Resolution order (see :func:`_candidate_config_paths`):
    ``HEADROOM_GATEWAY_CONFIG`` env path -> ``headroom-gateway.json`` ->
    the client's ``mcp.json``. The first source that exists on disk is selected
    and used **exclusively**; later sources are never merged in.

    The selected source is parsed with :func:`_read_json` (the same helper
    ``KiroRegistrar`` uses). Because that helper returns an empty mapping for an
    unreadable or unparseable file, a selected-but-broken source yields an empty
    downstream list rather than raising — the gateway can still serve its
    meta-tools (Requirement 9.6).

    Downstreams are drawn from the ``mcpServers`` mapping. The
    ``headroom-gateway`` self entry is always excluded to prevent recursion,
    even if an ``include`` filter names it (Requirement 9.2). Optional top-level
    ``include`` / ``exclude`` filters are applied in that order — ``include``
    first, then ``exclude`` — and filter names not present in the source are
    ignored (Requirements 9.3-9.5). Optional behavior toggles present in the
    source override the :class:`GatewayConfigModel` defaults.

    Returns:
        A :class:`GatewayConfigModel`. Never raises: a missing source yields an
        empty downstream list, as does a selected source that cannot be parsed.
    """
    source = _select_source(_candidate_config_paths())
    if source is None:
        return GatewayConfigModel(downstreams=[])

    data = _read_json(source)

    include = _names_from(data.get("include"))
    exclude = _names_from(data.get("exclude"))

    servers = data.get("mcpServers", {})
    if not isinstance(servers, dict):
        servers = {}

    include_set = set(include)
    exclude_set = set(exclude)

    downstreams: list[DownstreamSpec] = []
    for name, entry in servers.items():
        server_name = str(name)
        # Always exclude the gateway self entry (anti-recursion), even if an
        # include filter names it.
        if server_name == GATEWAY_SERVER_NAME:
            continue
        # Apply include first, then exclude; names absent from the source are
        # naturally ignored since we only iterate entries that exist.
        if include_set and server_name not in include_set:
            continue
        if server_name in exclude_set:
            continue
        spec = _entry_to_downstream(server_name, entry)
        if spec is not None:
            downstreams.append(spec)

    kwargs: dict[str, Any] = {"downstreams": downstreams, "include": include, "exclude": exclude}

    # Optional behavior toggles override GatewayConfigModel defaults when present.
    if isinstance(data.get("find_limit_default"), int):
        kwargs["find_limit_default"] = data["find_limit_default"]
    if isinstance(data.get("compress_results"), bool):
        kwargs["compress_results"] = data["compress_results"]
    if isinstance(data.get("compress_min_tokens"), int):
        kwargs["compress_min_tokens"] = data["compress_min_tokens"]
    if isinstance(data.get("connect_timeout_s"), (int, float)):
        kwargs["connect_timeout_s"] = float(data["connect_timeout_s"])
    if isinstance(data.get("call_timeout_s"), (int, float)):
        kwargs["call_timeout_s"] = float(data["call_timeout_s"])
    if isinstance(data.get("retry_failed_downstreams"), bool):
        kwargs["retry_failed_downstreams"] = data["retry_failed_downstreams"]
    # Out-of-range retry values fall back to the defaults rather than raising:
    # a nonsensical attempt count or delay must not stop the gateway serving.
    attempts = data.get("retry_max_attempts")
    if isinstance(attempts, int) and not isinstance(attempts, bool) and attempts >= 0:
        kwargs["retry_max_attempts"] = attempts
    delay = data.get("retry_initial_delay_s")
    if isinstance(delay, (int, float)) and not isinstance(delay, bool) and delay >= 0:
        kwargs["retry_initial_delay_s"] = float(delay)

    return GatewayConfigModel(**kwargs)


__all__ = [
    "RETRY_MAX_DELAY_S",
    "DownstreamSpec",
    "GatewayConfigModel",
    "resolve_gateway_config",
]
