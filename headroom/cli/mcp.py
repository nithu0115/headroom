"""MCP (Model Context Protocol) CLI commands for Claude Code integration.

Provides commands to configure and run the Headroom MCP server, enabling
Claude Code subscription users to use CCR (Compress-Cache-Retrieve) without
needing API key access.
"""

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import click

from headroom._subprocess import run

from .main import main

# Default paths
CLAUDE_CONFIG_DIR = Path.home() / ".claude"
MCP_CONFIG_PATH = CLAUDE_CONFIG_DIR / "mcp.json"
DEFAULT_PROXY_URL = "http://127.0.0.1:8787"


def get_headroom_command() -> list[str]:
    """Get the command to run headroom MCP server.

    Returns the CLI invocation used by Claude Code config.
    """
    return ["headroom", "mcp", "serve"]


def load_mcp_config() -> dict[str, Any]:
    """Load existing MCP config or return empty structure."""
    if MCP_CONFIG_PATH.exists():
        try:
            with open(MCP_CONFIG_PATH, encoding="utf-8") as f:
                result: dict[str, Any] = json.load(f)
                return result
        except (json.JSONDecodeError, OSError):
            return {"mcpServers": {}}
    return {"mcpServers": {}}


def save_mcp_config(config: dict) -> None:
    """Save MCP config, creating directory if needed."""
    CLAUDE_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(MCP_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)
        f.write("\n")  # Trailing newline


@main.group()
def mcp() -> None:
    """MCP server for Claude Code integration.

    \b
    The MCP server exposes headroom_retrieve as a tool that Claude Code
    can use to retrieve compressed content. This enables CCR (Compress-
    Cache-Retrieve) for subscription users who don't have API access.

    \b
    Quick Start:
        headroom mcp install    # Configure Claude Code
        headroom proxy          # Start the proxy (in another terminal)
        ANTHROPIC_BASE_URL=http://127.0.0.1:8787 claude

    \b
    The MCP server provides on-demand tools (compress, retrieve, stats).
    For automatic compression of ALL traffic, also set ANTHROPIC_BASE_URL
    to route through the proxy.

    \b
    How it works:
        1. ANTHROPIC_BASE_URL routes all requests through the proxy
        2. The proxy compresses large tool outputs (file listings, search results)
        3. Claude sees compressed summaries with hash markers
        4. When Claude needs full details, it calls headroom_retrieve
        5. The MCP server fetches original content from the proxy

    \b
    Note on tool naming: MCP clients display tools as
    `mcp__<server>__<tool>`. Our server is named "headroom" and our
    tools are named headroom_retrieve / headroom_compress / etc., so
    Claude Code shows them as `mcp__headroom__headroom_retrieve`. The
    "headroom" doubling is normal MCP namespacing — not a bug. The
    proxy's compression markers (and any docs/prompts) reference the
    bare tool name `headroom_retrieve`.
    """
    pass


@mcp.command("install")
@click.option(
    "--proxy-url",
    default=DEFAULT_PROXY_URL,
    help=f"Headroom proxy URL (default: {DEFAULT_PROXY_URL})",
)
@click.option(
    "--agent",
    "agents",
    multiple=True,
    help="Restrict installation to specific agents (default: every detected agent).",
)
@click.option(
    "--force",
    is_flag=True,
    help="Overwrite existing headroom config in case of mismatch.",
)
def mcp_install(proxy_url: str, agents: tuple[str, ...], force: bool) -> None:
    """Install the Headroom MCP server into every detected coding agent.

    \b
    By default this installs into every agent that has a registrar and is
    detected on this system (Claude Code today; Cursor / Codex / Continue /
    others added in subsequent releases). Pass ``--agent NAME`` one or more
    times to restrict the installation.

    \b
    Examples:
        headroom mcp install                            # every detected agent
        headroom mcp install --agent claude             # Claude Code only
        headroom mcp install --proxy-url http://localhost:9000
    """
    try:
        import mcp  # noqa: F401
    except ImportError:
        click.echo("Error: MCP SDK not installed.", err=True)
        click.echo("Install with: pip install 'headroom-ai[mcp]'", err=True)
        raise SystemExit(1) from None

    from headroom.mcp_registry import any_succeeded, format_results, install_everywhere

    results = install_everywhere(
        proxy_url=proxy_url,
        agents=list(agents) if agents else None,
        force=force,
    )

    if not results:
        click.echo("No agents matched the requested filter.")
        raise SystemExit(1)

    click.echo("Installing Headroom MCP server...")
    for line in format_results(
        results,
        verbose=True,
        overwrite_hint=f"headroom mcp install --proxy-url {proxy_url} --force",
    ):
        click.echo(line)

    if not any_succeeded(results):
        raise SystemExit(1)

    click.echo(
        f"\nNext steps:\n"
        f"  1. Start the Headroom proxy (if not running): headroom proxy\n"
        f"  2. Start your agent (e.g.) ANTHROPIC_BASE_URL={proxy_url} claude\n"
        f"  3. Restart any agent that was already running so it picks up the new MCP server.\n"
    )


@mcp.command("uninstall")
def mcp_uninstall() -> None:
    """Remove Headroom MCP server from Claude Code config.

    \b
    Removes headroom from both the claude CLI registry (Claude Code CLI >=2.x)
    and ~/.claude/mcp.json if present. Other MCP servers are preserved.
    """
    removed = False

    # Remove from claude CLI registry (Claude Code CLI >=2.x)
    claude_cli = shutil.which("claude")
    if claude_cli:
        check = subprocess.run(
            [claude_cli, "mcp", "get", "headroom"],
            capture_output=True,
        )
        if check.returncode == 0:
            rm = run(
                [claude_cli, "mcp", "remove", "headroom", "-s", "user"],
                capture_output=True,
                text=True,
            )
            if rm.returncode == 0:
                click.echo("✓ Headroom MCP server removed (via claude mcp remove)")
                removed = True
            else:
                click.echo(
                    f"Warning: 'claude mcp remove' failed ({rm.stderr.strip()}).",
                    err=True,
                )

    # Also remove codebase-memory-mcp if registered (installed by --code-graph)
    if claude_cli:
        cbm_check = subprocess.run(
            [claude_cli, "mcp", "get", "codebase-memory-mcp"],
            capture_output=True,
        )
        if cbm_check.returncode == 0:
            cbm_rm = run(
                [claude_cli, "mcp", "remove", "codebase-memory-mcp", "-s", "user"],
                capture_output=True,
                text=True,
            )
            if cbm_rm.returncode == 0:
                click.echo("✓ codebase-memory-mcp MCP server removed")
                removed = True

    # Also remove from mcp.json fallback config if present
    if MCP_CONFIG_PATH.exists():
        config = load_mcp_config()
        changed = False
        for server_name in ("headroom", "codebase-memory-mcp"):
            if server_name in config.get("mcpServers", {}):
                del config["mcpServers"][server_name]
                changed = True
        if changed:
            save_mcp_config(config)
            click.echo(f"✓ MCP servers removed from {MCP_CONFIG_PATH}")
            removed = True

    if not removed:
        if MCP_CONFIG_PATH.exists():
            click.echo("Headroom MCP is not configured. Nothing to uninstall.")
        else:
            click.echo("No MCP config found. Nothing to uninstall.")


@mcp.command("status")
def mcp_status() -> None:
    """Check Headroom MCP configuration status.

    \b
    Shows whether headroom is configured in Claude Code and if
    the proxy is reachable.
    """
    click.echo("Headroom MCP Status")
    click.echo("=" * 40)

    # Check MCP SDK
    try:
        import mcp  # noqa: F401

        click.echo("MCP SDK:        ✓ Installed")
    except ImportError:
        click.echo("MCP SDK:        ✗ Not installed")
        click.echo("                pip install 'headroom-ai[mcp]'")

    # Check config
    if MCP_CONFIG_PATH.exists():
        config = load_mcp_config()
        if "headroom" in config.get("mcpServers", {}):
            server_config = config["mcpServers"]["headroom"]
            click.echo("Claude Config:  ✓ Configured")
            click.echo(f"                {MCP_CONFIG_PATH}")

            # Show proxy URL
            env = server_config.get("env", {})
            proxy_url = env.get("HEADROOM_PROXY_URL", DEFAULT_PROXY_URL)
            click.echo(f"Proxy URL:      {proxy_url}")
        else:
            click.echo("Claude Config:  ✗ Not configured")
            click.echo("                Run: headroom mcp install")
    else:
        click.echo("Claude Config:  ✗ No config file")
        click.echo("                Run: headroom mcp install")

    # Check proxy connectivity
    try:
        import httpx

        config = load_mcp_config()
        env = config.get("mcpServers", {}).get("headroom", {}).get("env", {})
        proxy_url = env.get("HEADROOM_PROXY_URL", DEFAULT_PROXY_URL)

        try:
            response = httpx.get(f"{proxy_url}/health", timeout=2.0)
            if response.status_code == 200:
                click.echo(f"Proxy Status:   ✓ Running at {proxy_url}")
            else:
                click.echo(f"Proxy Status:   ✗ Unhealthy (status {response.status_code})")
        except httpx.ConnectError:
            click.echo("Proxy Status:   ✗ Not running")
            click.echo("                Run: headroom proxy")
        except httpx.TimeoutException:
            click.echo("Proxy Status:   ✗ Timeout")
        except httpx.HTTPError as e:
            # Catch the rest (InvalidURL, UnsupportedProtocol, ProtocolError, …)
            # so a malformed configured HEADROOM_PROXY_URL can't crash `status`.
            click.echo(f"Proxy Status:   ✗ Unreachable ({proxy_url}: {e})")
    except ImportError:
        click.echo("Proxy Status:   ? (httpx not installed)")


@mcp.command("serve")
@click.option(
    "--proxy-url",
    default=None,
    envvar="HEADROOM_PROXY_URL",
    help=f"Headroom proxy URL (default: {DEFAULT_PROXY_URL})",
)
@click.option(
    "--direct",
    is_flag=True,
    help="(Deprecated, ignored) Direct CompressionStore access is no longer supported",
)
@click.option(
    "--debug",
    is_flag=True,
    help="Enable debug logging",
)
def mcp_serve(proxy_url: str | None, direct: bool, debug: bool) -> None:
    """Start the MCP server (called by Claude Code).

    \b
    This command is typically invoked by Claude Code via the MCP config,
    not run directly. It starts the MCP server with stdio transport.

    \b
    For manual testing:
        headroom mcp serve --debug
    """
    import asyncio
    import logging

    # Check for MCP SDK
    try:
        from headroom.ccr.mcp_server import create_ccr_mcp_server
    except ImportError as e:
        click.echo(f"Error: MCP dependencies not installed: {e}", err=True)
        click.echo("Install with: pip install 'headroom-ai[mcp]'", err=True)
        raise SystemExit(1) from None

    if debug:
        logging.basicConfig(
            level=logging.DEBUG,
            format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        )
    else:
        # Minimal logging for MCP (stdout is used for protocol)
        logging.basicConfig(
            level=logging.WARNING,
            format="%(levelname)s: %(message)s",
        )

    # Use default if not specified
    effective_proxy_url = proxy_url or DEFAULT_PROXY_URL

    if direct:
        click.echo(
            "Warning: --direct is deprecated and ignored; MCP retrieval uses the proxy URL.",
            err=True,
        )

    server = create_ccr_mcp_server(proxy_url=effective_proxy_url)

    async def run() -> None:
        try:
            await server.run_stdio()
        finally:
            await server.cleanup()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass  # Clean exit on Ctrl+C


@mcp.group("gateway")
def gateway() -> None:
    """MCP Gateway: front many downstream MCP servers behind four meta-tools.

    \b
    The gateway is a single Headroom MCP server that a client (Kiro, Claude
    Code, Cursor, ...) connects to *instead* of the many individual downstream
    MCP servers. It aggregates every downstream tool out of the model context
    and exposes only four meta-tools (find_tools, invoke_tool, describe_tool,
    list_servers), so a client that would otherwise load hundreds of downstream
    tool schemas instead sees a handful of meta-tools.

    \b
    Quick Start:
        headroom mcp gateway install   # register headroom-gateway in your client
        headroom mcp gateway status    # inspect downstreams + registration
        # then restart your client so it connects to the gateway

    \b
    The gateway resolves its downstream inventory from (in priority order):
        1. $HEADROOM_GATEWAY_CONFIG
        2. ~/.kiro/settings/headroom-gateway.json
        3. the client's ~/.kiro/settings/mcp.json
    Disable individual downstreams via an `exclude` list in that source; the
    gateway self entry is always excluded to prevent recursion.
    """
    pass


@gateway.command("serve")
@click.option(
    "--debug",
    is_flag=True,
    help="Enable debug logging",
)
def gateway_serve(debug: bool) -> None:
    """Start the MCP gateway (called by the client).

    \b
    This command is typically invoked by the client via the MCP config, not run
    directly. It resolves the downstream inventory, connects to every reachable
    downstream (isolating failures), builds the tool index, and serves the four
    meta-tools over stdio. Unreachable downstreams are reported to stderr and do
    not terminate the serve session.

    \b
    For manual testing:
        headroom mcp gateway serve --debug
    """
    import asyncio
    import json as _json
    import logging

    from headroom.mcp_gateway.config import resolve_gateway_config

    if debug:
        logging.basicConfig(
            level=logging.DEBUG,
            format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        )
    else:
        # Minimal logging for MCP (stdout is the protocol channel; log to stderr).
        logging.basicConfig(
            level=logging.WARNING,
            format="%(levelname)s: %(message)s",
        )

    config = resolve_gateway_config()

    # `mcp` is optional: MCPGatewayServer.__init__ raises ImportError when the
    # SDK is absent. Mirror `mcp serve` and fail gracefully with a clear message.
    try:
        from headroom.mcp_gateway.server import MCPGatewayServer

        server = MCPGatewayServer(config)
    except ImportError as e:
        click.echo(f"Error: MCP dependencies not installed: {e}", err=True)
        click.echo("Install with: pip install 'headroom-ai[mcp]'", err=True)
        raise SystemExit(1) from None

    async def run() -> None:
        await server.start()
        # Requirement 11.6: surface each unreachable downstream to stderr without
        # terminating. The server already isolates failures; the list_servers
        # status handler reports per-downstream health built from the aggregation.
        try:
            inventory = await server._handle_list_servers()
            data = _json.loads(inventory[0].text) if inventory else {}
            unreachable = [
                str(entry.get("server"))
                for entry in data.get("servers", [])
                if entry.get("health") != "healthy"
            ]
            if unreachable:
                click.echo(
                    "Warning: unreachable downstream server(s): " + ", ".join(unreachable),
                    err=True,
                )
        except Exception:  # noqa: BLE001 - status reporting must never break serve
            logging.getLogger("headroom.cli.mcp").debug(
                "gateway downstream status reporting failed", exc_info=True
            )

        try:
            await server.run_stdio()
        finally:
            await server.cleanup()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass  # Clean exit on Ctrl+C


@gateway.command("install")
@click.option(
    "--force",
    is_flag=True,
    help="Overwrite an existing headroom-gateway entry whose config differs.",
)
def gateway_install(force: bool) -> None:
    """Register the gateway as a single headroom-gateway entry in the client config.

    \b
    Writes exactly one `headroom-gateway` server entry (delegated to the Kiro
    registrar so unmanaged keys like disabled/timeout/autoApprove are preserved).
    If the entry already exists with a matching spec it is left unchanged and
    reported as already installed; a differing entry is reported as a mismatch
    unless --force is passed. A write failure aborts and leaves the config
    unchanged.
    """
    from headroom.mcp_registry import (
        GATEWAY_SERVER_NAME,
        GatewayRegistrar,
        format_result,
    )

    result = GatewayRegistrar().register(force=force)
    line = format_result(
        GATEWAY_SERVER_NAME,
        result,
        label=GATEWAY_SERVER_NAME,
        verbose=True,
        overwrite_hint="headroom mcp gateway install --force",
    )
    if line is not None:
        click.echo(line)

    if not result.ok:
        raise SystemExit(1)

    click.echo("\nNext step: restart your client so it connects to the headroom-gateway server.")


@gateway.command("uninstall")
def gateway_uninstall() -> None:
    """Remove the headroom-gateway entry from the client config.

    \b
    Other MCP servers are preserved. If the gateway is not installed there is
    nothing to remove; if the config write fails the operation aborts and the
    config is left unchanged.
    """
    from headroom.mcp_registry import GATEWAY_SERVER_NAME, GatewayRegistrar

    registrar = GatewayRegistrar()
    if registrar.get_server() is None:
        click.echo(f"{GATEWAY_SERVER_NAME} is not installed. Nothing to uninstall.")
        return

    if registrar.unregister_server():
        click.echo(f"✓ {GATEWAY_SERVER_NAME} removed")
    else:
        click.echo(
            f"Error: failed to remove {GATEWAY_SERVER_NAME} (client config unchanged).",
            err=True,
        )
        raise SystemExit(1)


@gateway.command("status")
def gateway_status() -> None:
    """Report the resolved downstream inventory and registration state.

    \b
    Shows the downstream servers the gateway would front and whether the
    headroom-gateway self entry is registered in the client config. If only one
    of these can be determined, this errors rather than returning a partial
    status.
    """
    from headroom.mcp_registry import GATEWAY_SERVER_NAME, GatewayRegistrar

    # Resolve the downstream inventory.
    inventory: list[str] | None
    inventory_error: str | None = None
    try:
        from headroom.mcp_gateway.config import resolve_gateway_config

        config = resolve_gateway_config()
        inventory = [downstream.name for downstream in config.downstreams]
    except Exception as exc:  # noqa: BLE001 - undetermined inventory handled below
        inventory = None
        inventory_error = str(exc)

    # Resolve the Self_Entry registration state.
    registered: bool | None
    registration_error: str | None = None
    try:
        registered = GatewayRegistrar().get_server() is not None
    except Exception as exc:  # noqa: BLE001 - undetermined state handled below
        registered = None
        registration_error = str(exc)

    # Requirement 11.9: never return a partial status.
    if inventory is None or registered is None:
        parts: list[str] = []
        if inventory is None:
            parts.append(f"downstream inventory ({inventory_error})")
        if registered is None:
            parts.append(f"registration state ({registration_error})")
        click.echo(
            "Error: gateway status could not be fully determined: " + "; ".join(parts),
            err=True,
        )
        raise SystemExit(1)

    click.echo("Headroom MCP Gateway Status")
    click.echo("=" * 40)
    state = "registered" if registered else "not registered"
    click.echo(f"Registration:   {state} ({GATEWAY_SERVER_NAME})")
    if inventory:
        click.echo(f"Downstreams:    {len(inventory)} configured")
        for name in inventory:
            click.echo(f"                - {name}")
    else:
        click.echo("Downstreams:    none configured")


@gateway.command("version")
def gateway_version() -> None:
    """Show the Headroom build serving the gateway.

    \b
    Reports the version plus the interpreter and package directory actually in
    use. The version alone cannot distinguish two installs that report the same
    version, so the resolved paths are what confirm a fresh wheel is live after
    a --force-reinstall.
    """
    import sys
    from pathlib import Path

    import headroom
    from headroom._version import __version__

    module_file = getattr(headroom, "__file__", None)
    package = str(Path(module_file).resolve().parent) if module_file else "unknown"

    click.echo(f"headroom-gateway {__version__}")
    click.echo(f"python           {sys.executable}")
    click.echo(f"package          {package}")
