"""Tests for the :mod:`headroom.mcp_gateway.config` data models.

These cover the plain construction contract of the config dataclasses (task 1.2):

* :class:`DownstreamSpec` *composes* the existing
  :class:`~headroom.mcp_registry.base.ServerSpec` for stdio downstreams and
  holds ``url``/``headers`` for http downstreams, rather than redefining the
  stdio fields.
* :class:`GatewayConfigModel` default field values match the design
  (Requirements 4.2, 9.7).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from headroom.mcp_gateway import DownstreamSpec, GatewayConfigModel
from headroom.mcp_gateway.config import resolve_gateway_config
from headroom.mcp_registry.base import ServerSpec
from headroom.mcp_registry.gateway import GATEWAY_SERVER_NAME


def test_downstream_spec_composes_server_spec_for_stdio() -> None:
    stdio = ServerSpec(name="asana", command="npx", args=("-y", "asana-mcp"), env={"TOKEN": "x"})
    spec = DownstreamSpec(name="asana", transport="stdio", stdio=stdio)

    assert spec.name == "asana"
    assert spec.transport == "stdio"
    # The stdio ServerSpec is composed verbatim, not flattened/redefined.
    assert spec.stdio is stdio
    assert spec.stdio.command == "npx"
    assert spec.stdio.args == ("-y", "asana-mcp")
    assert spec.stdio.env == {"TOKEN": "x"}
    # http-only fields stay at their defaults for a stdio downstream.
    assert spec.url is None
    assert spec.headers == {}


def test_downstream_spec_holds_url_and_headers_for_http() -> None:
    spec = DownstreamSpec(
        name="remote",
        transport="http",
        url="https://example.com/mcp",
        headers={"Authorization": "Bearer t"},
    )

    assert spec.name == "remote"
    assert spec.transport == "http"
    assert spec.url == "https://example.com/mcp"
    assert spec.headers == {"Authorization": "Bearer t"}
    # stdio composition is absent for an http downstream.
    assert spec.stdio is None


def test_downstream_spec_headers_default_is_independent_per_instance() -> None:
    # field(default_factory=dict) must not share a single mutable default.
    a = DownstreamSpec(name="a", transport="http", url="https://a")
    b = DownstreamSpec(name="b", transport="http", url="https://b")
    a.headers["k"] = "v"
    assert b.headers == {}


def test_gateway_config_model_defaults_match_design() -> None:
    model = GatewayConfigModel(downstreams=[])

    assert model.downstreams == []
    assert model.include == ()
    assert model.exclude == ()
    assert model.find_limit_default == 5
    assert model.compress_results is False
    assert model.compress_min_tokens == 1000
    assert model.connect_timeout_s == 30.0
    assert model.call_timeout_s == 120.0
    assert model.retry_failed_downstreams is True
    assert model.retry_max_attempts == 3
    assert model.retry_initial_delay_s == 15.0


def test_gateway_config_model_defaults_have_expected_types() -> None:
    model = GatewayConfigModel(downstreams=[])

    assert isinstance(model.include, tuple)
    assert isinstance(model.exclude, tuple)
    assert isinstance(model.find_limit_default, int)
    assert isinstance(model.compress_results, bool)
    assert isinstance(model.compress_min_tokens, int)
    assert isinstance(model.connect_timeout_s, float)
    assert isinstance(model.call_timeout_s, float)
    assert isinstance(model.retry_failed_downstreams, bool)
    assert isinstance(model.retry_max_attempts, int)
    assert isinstance(model.retry_initial_delay_s, float)


def test_retry_backoff_cap_is_a_module_constant() -> None:
    """The backoff cap is a constant, deliberately not a fourth config knob."""
    from headroom.mcp_gateway.config import RETRY_MAX_DELAY_S

    assert RETRY_MAX_DELAY_S == 120.0
    assert not hasattr(GatewayConfigModel(downstreams=[]), "retry_max_delay_s")


# ---------------------------------------------------------------------------
# Config resolution tests (tasks 2.2, 2.3)
#
# These drive source selection deterministically and hermetically: the real
# ``~/.kiro`` is never consulted because ``Path.home`` is redirected to a temp
# directory and the ``HEADROOM_GATEWAY_CONFIG`` / ``KIRO_MCP_CONFIG`` env vars
# are controlled per test.
# ---------------------------------------------------------------------------


def _write_json(path: Path, obj: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")


def _stdio_entry(command: str = "cmd") -> dict[str, str]:
    return {"command": command}


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    """Redirect ``Path.home`` to a temp dir and clear the config env overrides.

    This makes both the dedicated ``headroom-gateway.json`` path and the default
    ``mcp.json`` fallback resolve under ``tmp_path``, so no test depends on the
    developer's real ``~/.kiro``.
    """
    home = tmp_path / "home"
    (home / ".kiro" / "settings").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.delenv("HEADROOM_GATEWAY_CONFIG", raising=False)
    monkeypatch.delenv("KIRO_MCP_CONFIG", raising=False)
    return home


# --- Task 2.2: Property P10 — config resolution filtering and self-exclusion ---

_NAME = st.sampled_from([GATEWAY_SERVER_NAME, "asana", "slack", "aws", "github", "notion", "jira"])


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    servers=st.dictionaries(_NAME, st.just(_stdio_entry()), max_size=8),
    include=st.lists(_NAME, max_size=5),
    exclude=st.lists(_NAME, max_size=5),
)
def test_p10_resolution_filters_and_self_exclusion(
    servers, include, exclude, tmp_path, monkeypatch
):
    """Property P10: resolved downstreams honor include/exclude and never self-reference.

    **Validates: Requirements 9.2, 9.3, 9.4**

    For any server map + include/exclude filters read from a source, the resolved
    downstream list:

    * ALWAYS excludes ``headroom-gateway`` (even when ``include`` names it),
    * is a subset of ``include`` when an ``include`` filter is set, and
    * contains none of the ``exclude`` names.
    """
    source = tmp_path / "gateway-config.json"
    _write_json(source, {"mcpServers": servers, "include": include, "exclude": exclude})
    monkeypatch.setenv("HEADROOM_GATEWAY_CONFIG", str(source))

    resolved = resolve_gateway_config()
    names = [d.name for d in resolved.downstreams]

    # Self-exclusion: anti-recursion holds even if include names the gateway.
    assert GATEWAY_SERVER_NAME not in names
    # Every resolved name came from the source's mcpServers map.
    assert set(names) <= set(servers)
    # include acts as an allowlist when set.
    include_set = set(include)
    if include_set:
        assert set(names) <= include_set
    # exclude drops names.
    assert set(names).isdisjoint(set(exclude))
    # Resolved names are unique (one entry per source key).
    assert len(names) == len(set(names))


# --- Task 2.3: Unit tests for source precedence and invalid config ---


def test_env_override_wins_over_dedicated_and_mcp(isolated_home, tmp_path, monkeypatch):
    """HEADROOM_GATEWAY_CONFIG is the exclusive source when present (Req 9.1)."""
    env_file = tmp_path / "env.json"
    _write_json(env_file, {"mcpServers": {"env-server": _stdio_entry("e")}})
    _write_json(
        isolated_home / ".kiro" / "settings" / "headroom-gateway.json",
        {"mcpServers": {"dedicated-server": _stdio_entry("d")}},
    )
    _write_json(
        isolated_home / ".kiro" / "settings" / "mcp.json",
        {"mcpServers": {"mcp-server": _stdio_entry("m")}},
    )
    monkeypatch.setenv("HEADROOM_GATEWAY_CONFIG", str(env_file))

    resolved = resolve_gateway_config()

    assert [d.name for d in resolved.downstreams] == ["env-server"]


def test_dedicated_file_wins_over_mcp(isolated_home):
    """headroom-gateway.json beats mcp.json when no env override is set (Req 9.1)."""
    _write_json(
        isolated_home / ".kiro" / "settings" / "headroom-gateway.json",
        {"mcpServers": {"dedicated-server": _stdio_entry("d")}},
    )
    _write_json(
        isolated_home / ".kiro" / "settings" / "mcp.json",
        {"mcpServers": {"mcp-server": _stdio_entry("m")}},
    )

    resolved = resolve_gateway_config()

    assert [d.name for d in resolved.downstreams] == ["dedicated-server"]


def test_mcp_json_used_when_it_is_the_only_source(isolated_home):
    """The client's mcp.json is the last-resort source (Req 9.1)."""
    _write_json(
        isolated_home / ".kiro" / "settings" / "mcp.json",
        {"mcpServers": {"mcp-server": _stdio_entry("m")}},
    )

    resolved = resolve_gateway_config()

    assert [d.name for d in resolved.downstreams] == ["mcp-server"]


def test_mcp_json_honored_via_kiro_mcp_config_override(isolated_home, tmp_path, monkeypatch):
    """The mcp.json fallback follows the KIRO_MCP_CONFIG override (Req 9.1, 9.7)."""
    custom_mcp = tmp_path / "custom-mcp.json"
    _write_json(custom_mcp, {"mcpServers": {"override-server": _stdio_entry("o")}})
    monkeypatch.setenv("KIRO_MCP_CONFIG", str(custom_mcp))

    resolved = resolve_gateway_config()

    assert [d.name for d in resolved.downstreams] == ["override-server"]


def test_invalid_selected_env_source_yields_empty_without_raising(
    isolated_home, tmp_path, monkeypatch
):
    """An existing-but-unparseable selected source resolves to empty (Req 9.6)."""
    env_file = tmp_path / "broken.json"
    env_file.write_text("{ this is not valid json ", encoding="utf-8")
    monkeypatch.setenv("HEADROOM_GATEWAY_CONFIG", str(env_file))

    resolved = resolve_gateway_config()

    assert resolved.downstreams == []


def test_invalid_mcp_json_source_yields_empty_without_raising(isolated_home):
    """An unparseable mcp.json (last-resort source) resolves to empty (Req 9.6)."""
    mcp = isolated_home / ".kiro" / "settings" / "mcp.json"
    mcp.write_text("not json at all", encoding="utf-8")

    resolved = resolve_gateway_config()

    assert resolved.downstreams == []


def test_no_source_present_yields_empty(isolated_home):
    """With no source on disk, resolution returns an empty downstream list (Req 9.6)."""
    resolved = resolve_gateway_config()

    assert resolved.downstreams == []


def test_retry_toggles_are_read_from_the_source(isolated_home, tmp_path, monkeypatch):
    """Valid retry toggles in the selected source override the defaults."""
    source = tmp_path / "gateway-config.json"
    _write_json(
        source,
        {
            "mcpServers": {},
            "retry_failed_downstreams": False,
            "retry_max_attempts": 7,
            "retry_initial_delay_s": 2,
        },
    )
    monkeypatch.setenv("HEADROOM_GATEWAY_CONFIG", str(source))

    resolved = resolve_gateway_config()

    assert resolved.retry_failed_downstreams is False
    assert resolved.retry_max_attempts == 7
    assert resolved.retry_initial_delay_s == 2.0
    assert isinstance(resolved.retry_initial_delay_s, float)


@pytest.mark.parametrize(
    "overrides",
    [
        {"retry_failed_downstreams": "yes"},
        {"retry_max_attempts": -1},
        {"retry_max_attempts": "3"},
        {"retry_max_attempts": 1.5},
        {"retry_max_attempts": True},
        {"retry_initial_delay_s": -5},
        {"retry_initial_delay_s": "15"},
        {"retry_initial_delay_s": None},
    ],
)
def test_invalid_retry_values_fall_back_to_defaults(
    overrides, isolated_home, tmp_path, monkeypatch
):
    """An invalid retry value falls back to its default rather than raising."""
    source = tmp_path / "gateway-config.json"
    _write_json(source, {"mcpServers": {}, **overrides})
    monkeypatch.setenv("HEADROOM_GATEWAY_CONFIG", str(source))

    resolved = resolve_gateway_config()

    defaults = GatewayConfigModel(downstreams=[])
    assert resolved.retry_failed_downstreams == defaults.retry_failed_downstreams
    assert resolved.retry_max_attempts == defaults.retry_max_attempts
    assert resolved.retry_initial_delay_s == defaults.retry_initial_delay_s


def test_disabled_launchable_entry_is_still_fronted(isolated_home, tmp_path, monkeypatch):
    """A disabled-but-launchable entry is still fronted by the gateway.

    Disabling a server in Kiro's ``mcp.json`` drops it from Kiro's context but
    must NOT drop it from the gateway — the gateway is designed to front exactly
    those servers, so a ``"disabled": true`` entry with a valid command resolves.
    """
    source = tmp_path / "gateway-config.json"
    _write_json(
        source,
        {"mcpServers": {"off": {"command": "cmd", "disabled": True}}},
    )
    monkeypatch.setenv("HEADROOM_GATEWAY_CONFIG", str(source))

    resolved = resolve_gateway_config()

    assert [d.name for d in resolved.downstreams] == ["off"]
    assert resolved.downstreams[0].transport == "stdio"


def test_entry_without_command_or_url_is_excluded(isolated_home, tmp_path, monkeypatch):
    """An entry with empty/missing command and no url is unlaunchable, so skipped."""
    source = tmp_path / "gateway-config.json"
    _write_json(
        source,
        {
            "mcpServers": {
                "empty-command": {"command": "   "},
                "no-command": {"args": ["-y", "x"]},
            }
        },
    )
    monkeypatch.setenv("HEADROOM_GATEWAY_CONFIG", str(source))

    resolved = resolve_gateway_config()

    assert [d.name for d in resolved.downstreams] == []


def test_disabled_launchable_entry_resolves_alongside_plain_one(
    isolated_home, tmp_path, monkeypatch
):
    """Both a plain entry and a disabled-but-launchable one resolve, order preserved."""
    source = tmp_path / "gateway-config.json"
    _write_json(
        source,
        {
            "mcpServers": {
                "live": {"command": "cmd"},
                "off": {"command": "cmd", "disabled": True},
            }
        },
    )
    monkeypatch.setenv("HEADROOM_GATEWAY_CONFIG", str(source))

    resolved = resolve_gateway_config()

    assert [d.name for d in resolved.downstreams] == ["live", "off"]
    assert all(d.transport == "stdio" for d in resolved.downstreams)
