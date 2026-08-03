"""Unit tests for the ``headroom mcp gateway`` CLI subgroup (task 9.2).

These exercise the ``gateway`` click commands wired in
:mod:`headroom.cli.mcp` (``gateway_serve``, ``gateway_install``,
``gateway_uninstall``, ``gateway_status``, ``gateway_version``) at the CLI
seam, using click's
:class:`~click.testing.CliRunner` (the same harness as
``tests/test_cli/test_mcp.py``).

Collaborators are monkeypatched so no real I/O, stdio session, subprocess, or
config write ever happens:

* ``serve`` — a fake ``MCPGatewayServer`` whose ``start`` / ``run_stdio`` /
  ``cleanup`` are no-op coroutines and whose ``_handle_list_servers`` reports a
  chosen downstream-health payload, plus a stubbed ``resolve_gateway_config``.
* ``install`` / ``uninstall`` / ``status`` — ``GatewayRegistrar`` methods and
  ``resolve_gateway_config`` are patched to return controlled values or raise.
* ``version`` — needs no patching; it only reads in-process version/path values.

Coverage (Requirements 11.1, 11.6, 11.7, 11.8, 11.9):

* 11.1 — the ``gateway`` group exposes
  ``serve``/``install``/``uninstall``/``status``/``version``.
* 11.6 — ``serve`` reports each unreachable downstream to stderr without terminating.
* 11.7 — ``install`` when already present reports already-installed with exit 0.
* 11.8 — a failed config write during ``install``/``uninstall`` aborts non-zero.
* 11.9 — ``status`` errors (rather than returning a partial status) when only one
  of the inventory / registration state can be determined.

The config-unchanged guarantee behind 11.8 is enforced and verified at the
registrar layer (``tests/test_mcp_registry_gateway.py``); here we assert the CLI
seam surfaces the failure as a non-zero exit with an error message.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from click.testing import CliRunner

from headroom.cli.main import main
from headroom.mcp_registry import GATEWAY_SERVER_NAME, GatewayRegistrar
from headroom.mcp_registry.base import RegisterResult, RegisterStatus, ServerSpec

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _combined(result: Any) -> str:
    """Return stdout + stderr as one string, tolerant of click's split streams.

    click >= 8.2 keeps stdout and stderr separate on the result object; older
    releases merge them into ``output``. Concatenating covers both so an
    assertion does not depend on which stream a line landed on.
    """
    text = result.output or ""
    try:
        stderr = result.stderr
    except (ValueError, AttributeError):
        stderr = ""
    return text + (stderr or "")


class _FakeGatewayServer:
    """Stand-in for ``MCPGatewayServer`` with no-op async lifecycle methods.

    Instances register themselves in :data:`_FakeGatewayServer.instances` so a
    test can assert the serve flow drove ``start`` / ``run_stdio`` / ``cleanup``.
    ``_handle_list_servers`` returns the health payload configured via the
    class-level :data:`health_payload`.
    """

    instances: list[_FakeGatewayServer] = []
    health_payload: dict[str, Any] = {"servers": []}

    def __init__(self, config: Any) -> None:
        self.config = config
        self.started = False
        self.ran = False
        self.cleaned = False
        _FakeGatewayServer.instances.append(self)

    async def start(self) -> None:
        self.started = True

    async def _handle_list_servers(self) -> list[SimpleNamespace]:
        return [SimpleNamespace(text=json.dumps(type(self).health_payload))]

    async def run_stdio(self) -> None:
        self.ran = True

    async def cleanup(self) -> None:
        self.cleaned = True


@pytest.fixture
def fake_gateway_serve(monkeypatch: pytest.MonkeyPatch):
    """Patch the serve collaborators and return the fake-server class.

    ``resolve_gateway_config`` is stubbed to a lightweight object (the fake
    server ignores it) and ``MCPGatewayServer`` is replaced with
    :class:`_FakeGatewayServer`. Tests set ``health_payload`` before invoking.
    """
    _FakeGatewayServer.instances = []
    _FakeGatewayServer.health_payload = {"servers": []}

    monkeypatch.setattr(
        "headroom.mcp_gateway.config.resolve_gateway_config",
        lambda: SimpleNamespace(downstreams=[]),
    )
    monkeypatch.setattr(
        "headroom.mcp_gateway.server.MCPGatewayServer",
        _FakeGatewayServer,
    )
    return _FakeGatewayServer


# ---------------------------------------------------------------------------
# 11.1 — subcommand presence
# ---------------------------------------------------------------------------


def test_gateway_group_exposes_all_subcommands() -> None:
    """`headroom mcp gateway` advertises serve/install/uninstall/status/version."""
    runner = CliRunner()
    result = runner.invoke(main, ["mcp", "gateway", "--help"])

    assert result.exit_code == 0
    for sub in ("serve", "install", "uninstall", "status", "version"):
        assert sub in result.output


@pytest.mark.parametrize("sub", ["serve", "install", "uninstall", "status", "version"])
def test_gateway_subcommands_are_invocable(sub: str) -> None:
    """Each advertised subcommand resolves (its --help exits cleanly)."""
    runner = CliRunner()
    result = runner.invoke(main, ["mcp", "gateway", sub, "--help"])

    assert result.exit_code == 0
    assert "Usage:" in result.output


def test_gateway_version_reports_build_and_location() -> None:
    """`headroom mcp gateway version` reports version, interpreter, package dir."""
    from headroom._version import __version__

    runner = CliRunner()
    result = runner.invoke(main, ["mcp", "gateway", "version"])

    assert result.exit_code == 0
    assert __version__ in result.output
    assert "python" in result.output
    assert "package" in result.output


# ---------------------------------------------------------------------------
# 11.6 — serve reports unreachable downstreams without terminating
# ---------------------------------------------------------------------------


def test_serve_reports_unreachable_downstream_without_terminating(
    fake_gateway_serve,
) -> None:
    """An unhealthy downstream is warned about; the serve flow still completes."""
    fake_gateway_serve.health_payload = {
        "servers": [
            {"server": "reachable-one", "health": "healthy"},
            {"server": "broken-two", "health": "unavailable"},
        ]
    }

    runner = CliRunner()
    result = runner.invoke(main, ["mcp", "gateway", "serve"])

    assert result.exit_code == 0
    combined = _combined(result)
    assert "unreachable downstream" in combined.lower()
    assert "broken-two" in combined
    # The healthy server is not reported as unreachable.
    assert "reachable-one" not in combined

    # Serve ran to completion (start -> run_stdio -> cleanup) rather than crashing.
    assert len(fake_gateway_serve.instances) == 1
    server = fake_gateway_serve.instances[0]
    assert server.started is True
    assert server.ran is True
    assert server.cleaned is True


def test_serve_with_all_reachable_emits_no_unreachable_warning(
    fake_gateway_serve,
) -> None:
    """When every downstream is healthy, serve emits no unreachable warning."""
    fake_gateway_serve.health_payload = {"servers": [{"server": "ok-one", "health": "healthy"}]}

    runner = CliRunner()
    result = runner.invoke(main, ["mcp", "gateway", "serve"])

    assert result.exit_code == 0
    assert "unreachable downstream" not in _combined(result).lower()
    assert fake_gateway_serve.instances[0].ran is True


# ---------------------------------------------------------------------------
# 11.7 — install when already present reports already-installed (no error)
# ---------------------------------------------------------------------------


def test_install_already_present_reports_already_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pre-existing matching entry yields an already-registered message, exit 0."""
    monkeypatch.setattr(
        GatewayRegistrar,
        "register",
        lambda self, *, force=False: RegisterResult(RegisterStatus.ALREADY, "matches"),
    )

    runner = CliRunner()
    result = runner.invoke(main, ["mcp", "gateway", "install"])

    assert result.exit_code == 0
    assert "already registered" in result.output.lower()


def test_install_success_reports_registration(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fresh registration reports success and exits 0."""
    monkeypatch.setattr(
        GatewayRegistrar,
        "register",
        lambda self, *, force=False: RegisterResult(RegisterStatus.REGISTERED, "via Kiro"),
    )

    runner = CliRunner()
    result = runner.invoke(main, ["mcp", "gateway", "install"])

    assert result.exit_code == 0
    assert "registered" in result.output.lower()


# ---------------------------------------------------------------------------
# 11.8 — install/uninstall write-failure aborts non-zero
# ---------------------------------------------------------------------------


def test_install_write_failure_aborts_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed config write during install exits non-zero with an error message."""
    monkeypatch.setattr(
        GatewayRegistrar,
        "register",
        lambda self, *, force=False: RegisterResult(RegisterStatus.FAILED, "permission denied"),
    )

    runner = CliRunner()
    result = runner.invoke(main, ["mcp", "gateway", "install"])

    assert result.exit_code != 0
    combined = _combined(result).lower()
    assert "failed" in combined
    assert "permission denied" in combined


def test_uninstall_write_failure_aborts_nonzero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed removal (entry present, write fails) exits non-zero with an error."""
    spec = ServerSpec(
        name=GATEWAY_SERVER_NAME,
        command="headroom",
        args=("mcp", "gateway", "serve"),
    )
    monkeypatch.setattr(GatewayRegistrar, "get_server", lambda self, *a, **k: spec)
    monkeypatch.setattr(GatewayRegistrar, "unregister_server", lambda self, *a, **k: False)

    runner = CliRunner()
    result = runner.invoke(main, ["mcp", "gateway", "uninstall"])

    assert result.exit_code != 0
    combined = _combined(result).lower()
    assert "failed to remove" in combined
    assert "unchanged" in combined


def test_uninstall_not_installed_is_a_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the gateway is absent, uninstall reports nothing to do and exits 0."""
    monkeypatch.setattr(GatewayRegistrar, "get_server", lambda self, *a, **k: None)

    runner = CliRunner()
    result = runner.invoke(main, ["mcp", "gateway", "uninstall"])

    assert result.exit_code == 0
    assert "nothing to uninstall" in result.output.lower()


# ---------------------------------------------------------------------------
# 11.9 — status errors rather than returning a partial status
# ---------------------------------------------------------------------------


def test_status_errors_when_only_inventory_undetermined(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inventory undeterminable + known registration -> error, not partial status."""

    def _boom() -> Any:
        raise RuntimeError("config unreadable")

    monkeypatch.setattr("headroom.mcp_gateway.config.resolve_gateway_config", _boom)
    monkeypatch.setattr(GatewayRegistrar, "get_server", lambda self, *a, **k: None)

    runner = CliRunner()
    result = runner.invoke(main, ["mcp", "gateway", "status"])

    assert result.exit_code != 0
    combined = _combined(result).lower()
    assert "could not be fully determined" in combined
    assert "config unreadable" in combined
    # A partial status body is NOT emitted.
    assert "registration:" not in combined


def test_status_errors_when_only_registration_undetermined(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Known inventory + undeterminable registration -> error, not partial status."""
    monkeypatch.setattr(
        "headroom.mcp_gateway.config.resolve_gateway_config",
        lambda: SimpleNamespace(downstreams=[SimpleNamespace(name="alpha")]),
    )

    def _boom(self: Any, *a: Any, **k: Any) -> Any:
        raise RuntimeError("registry unreadable")

    monkeypatch.setattr(GatewayRegistrar, "get_server", _boom)

    runner = CliRunner()
    result = runner.invoke(main, ["mcp", "gateway", "status"])

    assert result.exit_code != 0
    combined = _combined(result).lower()
    assert "could not be fully determined" in combined
    assert "registry unreadable" in combined
    assert "downstreams:" not in combined


def test_status_reports_full_state_when_both_determinable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both signals available -> the full status body is reported, exit 0."""
    monkeypatch.setattr(
        "headroom.mcp_gateway.config.resolve_gateway_config",
        lambda: SimpleNamespace(
            downstreams=[SimpleNamespace(name="alpha"), SimpleNamespace(name="beta")]
        ),
    )
    spec = ServerSpec(name=GATEWAY_SERVER_NAME, command="headroom", args=())
    monkeypatch.setattr(GatewayRegistrar, "get_server", lambda self, *a, **k: spec)

    runner = CliRunner()
    result = runner.invoke(main, ["mcp", "gateway", "status"])

    assert result.exit_code == 0
    assert "registered" in result.output.lower()
    assert "alpha" in result.output
    assert "beta" in result.output
