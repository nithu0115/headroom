"""Kiro MCP registrar.

Kiro (both the IDE and ``kiro-cli``) reads Model Context Protocol server
definitions from ``~/.kiro/settings/mcp.json`` under the standard
``{"mcpServers": {"<name>": {"command", "args", "env", ...}}}`` schema — the
same object shape Claude Code and OpenCode use. This registrar edits that JSON
file directly, mirroring :mod:`headroom.mcp_registry.opencode` (direct JSON
edit under a top-level key) combined with the ``mcpServers`` entry shape used by
:mod:`headroom.mcp_registry.claude`.

Kiro entries may carry extra, Kiro-specific keys (``disabled``, ``timeout``,
``autoApprove``, ...) that this registrar preserves but does not manage.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any

from .base import MCPRegistrar, RegisterResult, RegisterStatus, ServerSpec

logger = logging.getLogger(__name__)


def _kiro_config_path() -> Path:
    """Return the active Kiro MCP config path.

    Honors the ``KIRO_MCP_CONFIG`` override (a value with at least one
    non-whitespace character), expanding ``~``; otherwise the fixed global
    path ``~/.kiro/settings/mcp.json``.
    """
    env_path = os.environ.get("KIRO_MCP_CONFIG", "").strip()
    if env_path:
        return Path(env_path).expanduser().absolute()
    return Path.home() / ".kiro" / "settings" / "mcp.json"


def _read_json(path: Path) -> dict[str, Any]:
    """Read a JSON file, returning empty dict if absent or unparseable."""
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def _write_json(path: Path, data: dict[str, Any]) -> None:
    """Write JSON with indent=2 and a trailing newline; mkdir parents."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def _entry_to_spec(name: str, entry: dict[str, Any]) -> ServerSpec:
    """Read command/args/env from a Kiro mcpServers entry into a ServerSpec.

    Unknown keys (disabled, timeout, ...) are ignored for the spec but are
    NOT destroyed on write — see :func:`_spec_to_entry`.
    """
    args_value = entry.get("args", [])
    if isinstance(args_value, list):
        args = tuple(str(x) for x in args_value)
    else:
        args = ()
    env_value = entry.get("env", {})
    env: dict[str, str] = {}
    if isinstance(env_value, dict):
        env = {str(k): str(v) for k, v in env_value.items()}
    return ServerSpec(
        name=name,
        command=str(entry.get("command", "")),
        args=args,
        env=env,
    )


def _spec_to_entry(spec: ServerSpec, existing: dict[str, Any] | None = None) -> dict[str, Any]:
    """Serialize a ServerSpec to a Kiro mcpServers entry.

    When ``existing`` is given, start from a shallow copy of it so unmanaged
    keys (disabled/timeout/autoApprove/...) survive, then overwrite
    command/args/env. ``args``/``env`` are dropped when empty so the on-disk
    shape matches what :func:`_entry_to_spec` reads back (round-trip stable).
    """
    entry: dict[str, Any] = dict(existing) if isinstance(existing, dict) else {}
    entry["command"] = spec.command
    if spec.args:
        entry["args"] = list(spec.args)
    else:
        entry.pop("args", None)
    if spec.env:
        entry["env"] = dict(spec.env)
    else:
        entry.pop("env", None)
    return entry


def _specs_equivalent(a: ServerSpec, b: ServerSpec) -> bool:
    """True when name, command, args, and env are all equal."""
    return (
        a.name == b.name
        and a.command == b.command
        and tuple(a.args) == tuple(b.args)
        and dict(a.env) == dict(b.env)
    )


def _diff_specs(existing: ServerSpec, requested: ServerSpec) -> str:
    """Render the difference between two specs for human consumption."""
    parts: list[str] = []
    if existing.command != requested.command:
        parts.append(f"command {existing.command!r} -> {requested.command!r}")
    if tuple(existing.args) != tuple(requested.args):
        parts.append(f"args {list(existing.args)} -> {list(requested.args)}")
    if dict(existing.env) != dict(requested.env):
        parts.append(f"env {dict(existing.env)} -> {dict(requested.env)}")
    if not parts:
        return "spec differs in unidentified field(s)"
    return "; ".join(parts)


class KiroRegistrar(MCPRegistrar):
    """Register MCP servers with Kiro."""

    name = "kiro"
    display_name = "Kiro"

    def __init__(self, *, config_path: Path | None = None) -> None:
        self._config_path = config_path or _kiro_config_path()

    def detect(self) -> bool:
        if shutil.which("kiro") or shutil.which("kiro-cli"):
            return True
        if (Path.home() / ".kiro").is_dir():
            return True
        return self._config_path.parent.is_dir()

    def get_server(self, server_name: str) -> ServerSpec | None:
        data = _read_json(self._config_path)
        servers = data.get("mcpServers", {})
        if not isinstance(servers, dict):
            return None
        entry = servers.get(server_name)
        if not isinstance(entry, dict):
            return None
        return _entry_to_spec(server_name, entry)

    def register_server(self, spec: ServerSpec, *, force: bool = False) -> RegisterResult:
        existing = self.get_server(spec.name)

        if existing is not None and _specs_equivalent(existing, spec):
            return RegisterResult(RegisterStatus.ALREADY, "matches current configuration")

        if existing is not None and not force:
            return RegisterResult(
                RegisterStatus.MISMATCH,
                _diff_specs(existing, spec),
            )

        return self._write_entry(spec)

    def unregister_server(self, server_name: str) -> bool:
        data = _read_json(self._config_path)
        servers = data.get("mcpServers", {})
        if not isinstance(servers, dict) or server_name not in servers:
            return False
        del servers[server_name]
        if not servers:
            data.pop("mcpServers", None)
        try:
            _write_json(self._config_path, data)
        except OSError:
            return False
        return True

    def _write_entry(self, spec: ServerSpec) -> RegisterResult:
        try:
            self._config_path.parent.mkdir(parents=True, exist_ok=True)
            data = _read_json(self._config_path)
            servers = data.setdefault("mcpServers", {})
            if not isinstance(servers, dict):
                servers = {}
                data["mcpServers"] = servers
            prior = servers.get(spec.name) if isinstance(servers.get(spec.name), dict) else None
            servers[spec.name] = _spec_to_entry(spec, existing=prior)
            _write_json(self._config_path, data)
        except OSError as exc:
            return RegisterResult(
                RegisterStatus.FAILED, f"could not write {self._config_path}: {exc}"
            )
        return RegisterResult(RegisterStatus.REGISTERED, f"wrote to {self._config_path}")


__all__ = ["KiroRegistrar"]
