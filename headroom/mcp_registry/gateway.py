"""Gateway MCP registrar.

The MCP Gateway (``headroom mcp gateway serve``) fronts N downstream MCP
servers and exposes only a handful of meta-tools to the connecting client. To
use it, a client registers a *single* ``headroom-gateway`` server entry in its
MCP config in place of the many downstream entries.

This module owns that registration. It does **not** reimplement JSON reading,
writing, or the already/mismatch/force idempotency logic — instead it delegates
every config write to :class:`~headroom.mcp_registry.kiro.KiroRegistrar`, which
already:

* preserves unmanaged Kiro keys (``disabled``, ``timeout``, ``autoApprove``,
  ...) on any pre-existing entry (see ``_spec_to_entry``), and
* reports :class:`~headroom.mcp_registry.base.RegisterStatus` outcomes
  (``ALREADY`` / ``MISMATCH`` / ``REGISTERED`` / ``FAILED``) with force-override
  support.

:func:`build_gateway_spec` builds the canonical ``headroom-gateway``
:class:`ServerSpec`; it is re-exported from
:mod:`headroom.mcp_registry.install` alongside ``build_headroom_spec``.
"""

from __future__ import annotations

from pathlib import Path

from headroom.install.runtime import resolve_headroom_command

from .base import MCPRegistrar, RegisterResult, ServerSpec
from .kiro import KiroRegistrar

#: The single server name the gateway registers under.
GATEWAY_SERVER_NAME = "headroom-gateway"


def build_gateway_spec() -> ServerSpec:
    """Construct the canonical :class:`ServerSpec` for the MCP gateway.

    The command is taken from :func:`resolve_headroom_command` (the same
    resolver ``build_headroom_spec`` uses) and the args always **end** with the
    ordered tokens ``mcp``, ``gateway``, ``serve`` so the entry launches
    ``headroom mcp gateway serve`` regardless of how headroom itself is invoked
    (a resolved binary, or ``python -m headroom.cli``).
    """
    command = resolve_headroom_command()
    return ServerSpec(
        name=GATEWAY_SERVER_NAME,
        command=command[0],
        args=(*command[1:], "mcp", "gateway", "serve"),
    )


class GatewayRegistrar(MCPRegistrar):
    """Register the MCP gateway as a single ``headroom-gateway`` entry.

    Writes are delegated to :class:`KiroRegistrar` so unmanaged Kiro keys
    survive round-trips and the already/mismatch/force idempotency logic is not
    duplicated. The registrar only ever writes the one ``headroom-gateway``
    entry — it adds no additional server entries.
    """

    name = "kiro"
    display_name = "Kiro"

    def __init__(
        self,
        *,
        registrar: KiroRegistrar | None = None,
        config_path: Path | None = None,
    ) -> None:
        """Create a gateway registrar.

        Args:
            registrar: Inject a pre-built writer (test seam). When omitted a
                :class:`KiroRegistrar` is constructed.
            config_path: Passed through to the default :class:`KiroRegistrar`
                when ``registrar`` is not supplied.
        """
        self._delegate = registrar or KiroRegistrar(config_path=config_path)

    def detect(self) -> bool:
        """Return True if Kiro appears to be installed (delegated)."""
        return self._delegate.detect()

    def get_server(self, server_name: str = GATEWAY_SERVER_NAME) -> ServerSpec | None:
        """Return the registered gateway :class:`ServerSpec`, or ``None``."""
        return self._delegate.get_server(server_name)

    def register_server(
        self, spec: ServerSpec | None = None, *, force: bool = False
    ) -> RegisterResult:
        """Idempotently register the gateway entry, delegating the write.

        ``spec`` defaults to :func:`build_gateway_spec`; callers rarely pass it.
        The delegate handles every outcome — already-registered (byte-for-byte
        unchanged), mismatch (left untouched, differing spec reported),
        force-override (replace while preserving unmanaged keys), and
        write-failure (client config unchanged) — returning a
        :class:`RegisterResult`.
        """
        target = spec if spec is not None else build_gateway_spec()
        return self._delegate.register_server(target, force=force)

    def register(self, *, force: bool = False) -> RegisterResult:
        """Convenience wrapper: register the canonical gateway spec."""
        return self.register_server(build_gateway_spec(), force=force)

    def unregister_server(self, server_name: str = GATEWAY_SERVER_NAME) -> bool:
        """Remove the gateway entry. Returns True on success (delegated)."""
        return self._delegate.unregister_server(server_name)


__all__ = ["GATEWAY_SERVER_NAME", "GatewayRegistrar", "build_gateway_spec"]
