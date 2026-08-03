"""Tests for :mod:`headroom.mcp_registry.gateway`."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from headroom.mcp_registry.base import RegisterStatus
from headroom.mcp_registry.gateway import (
    GATEWAY_SERVER_NAME,
    GatewayRegistrar,
    build_gateway_spec,
)
from headroom.mcp_registry.kiro import KiroRegistrar


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


def _registrar(tmp_path: Path) -> GatewayRegistrar:
    return GatewayRegistrar(config_path=tmp_path / "mcp.json")


# ---------------------------------------------------------------------------
# build_gateway_spec
# ---------------------------------------------------------------------------


def test_build_gateway_spec_name_and_args() -> None:
    """The spec is named headroom-gateway and args end with the ordered tokens."""
    spec = build_gateway_spec()
    assert spec.name == GATEWAY_SERVER_NAME == "headroom-gateway"
    assert spec.command  # non-empty resolved command
    assert spec.args[-3:] == ("mcp", "gateway", "serve")


def test_build_gateway_spec_reexported_from_install() -> None:
    """build_gateway_spec is re-exported from install alongside build_headroom_spec."""
    from headroom.mcp_registry.install import build_gateway_spec as reexported

    assert reexported is build_gateway_spec


# ---------------------------------------------------------------------------
# Registration outcomes
# ---------------------------------------------------------------------------


def test_register_writes_single_entry(tmp_path: Path) -> None:
    """Registering writes exactly one headroom-gateway entry and no others."""
    registrar = _registrar(tmp_path)
    result = registrar.register()
    assert result.status == RegisterStatus.REGISTERED

    data = json.loads((tmp_path / "mcp.json").read_text())
    assert list(data["mcpServers"].keys()) == [GATEWAY_SERVER_NAME]


def test_register_already_when_matching(tmp_path: Path) -> None:
    """A matching pre-existing entry yields ALREADY and is left unchanged."""
    registrar = _registrar(tmp_path)
    registrar.register()
    before = (tmp_path / "mcp.json").read_text()

    result = registrar.register()
    assert result.status == RegisterStatus.ALREADY
    assert (tmp_path / "mcp.json").read_text() == before


def test_register_mismatch_left_unchanged(tmp_path: Path) -> None:
    """A differing entry without force yields MISMATCH and is untouched."""
    _write_json(
        tmp_path / "mcp.json",
        {"mcpServers": {GATEWAY_SERVER_NAME: {"command": "old", "args": ["serve"]}}},
    )
    before = (tmp_path / "mcp.json").read_text()
    registrar = _registrar(tmp_path)

    result = registrar.register()
    assert result.status == RegisterStatus.MISMATCH
    assert result.detail  # identifies the differing spec
    assert (tmp_path / "mcp.json").read_text() == before


def test_register_force_overrides_mismatch(tmp_path: Path) -> None:
    """force=True replaces a mismatched entry with the gateway spec."""
    _write_json(
        tmp_path / "mcp.json",
        {"mcpServers": {GATEWAY_SERVER_NAME: {"command": "old", "args": ["serve"]}}},
    )
    registrar = _registrar(tmp_path)

    result = registrar.register(force=True)
    assert result.status == RegisterStatus.REGISTERED
    written = registrar.get_server()
    assert written is not None
    assert written.args[-3:] == ("mcp", "gateway", "serve")


def test_register_preserves_unmanaged_keys(tmp_path: Path) -> None:
    """Unmanaged Kiro keys survive a force override (Property P11)."""
    _write_json(
        tmp_path / "mcp.json",
        {
            "mcpServers": {
                GATEWAY_SERVER_NAME: {
                    "command": "old",
                    "args": ["serve"],
                    "disabled": True,
                    "timeout": 42,
                    "autoApprove": ["find_tools"],
                }
            }
        },
    )
    registrar = _registrar(tmp_path)
    registrar.register(force=True)

    entry = json.loads((tmp_path / "mcp.json").read_text())["mcpServers"][GATEWAY_SERVER_NAME]
    assert entry["disabled"] is True
    assert entry["timeout"] == 42
    assert entry["autoApprove"] == ["find_tools"]
    assert entry["args"][-3:] == ["mcp", "gateway", "serve"]


def test_register_reports_write_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed delegated write yields FAILED and leaves config unchanged."""
    registrar = _registrar(tmp_path)

    def _fail_write(*args: Any, **kwargs: Any) -> None:
        msg = "permission denied"
        raise OSError(msg)

    monkeypatch.setattr("headroom.mcp_registry.kiro._write_json", _fail_write)
    result = registrar.register()
    assert result.status == RegisterStatus.FAILED
    assert result.detail
    assert not (tmp_path / "mcp.json").exists()


def test_register_delegates_to_injected_registrar(tmp_path: Path) -> None:
    """The registrar delegates writes to the composed KiroRegistrar."""
    kiro = KiroRegistrar(config_path=tmp_path / "mcp.json")
    registrar = GatewayRegistrar(registrar=kiro)
    registrar.register()
    # The delegate can read back the same entry it wrote.
    assert kiro.get_server(GATEWAY_SERVER_NAME) is not None


def test_unregister_removes_entry(tmp_path: Path) -> None:
    """unregister removes the gateway entry."""
    registrar = _registrar(tmp_path)
    registrar.register()
    assert registrar.unregister_server() is True
    assert registrar.get_server() is None
