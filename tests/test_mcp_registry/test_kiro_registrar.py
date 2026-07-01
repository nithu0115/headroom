"""Tests for the Kiro MCP registrar.

Modeled on ``test_claude_registrar.py`` / ``test_codex_registrar.py``. Every
test points the registrar at a ``tmp_path`` config via the ``config_path=``
constructor argument or the ``KIRO_MCP_CONFIG`` env override — never the real
``~/.kiro``.
"""

from __future__ import annotations

import json
import string
from pathlib import Path

import pytest

from headroom.mcp_registry.base import RegisterStatus, ServerSpec
from headroom.mcp_registry.kiro import (
    KiroRegistrar,
    _entry_to_spec,
    _kiro_config_path,
    _spec_to_entry,
    _specs_equivalent,
    _write_json,
)

_RESOLVED_COMMAND = "/usr/bin/python"
_RESOLVED_ARGS = ("-m", "headroom.cli", "mcp", "serve")


def _make_registrar(tmp_path: Path) -> KiroRegistrar:
    """Build a registrar pointed at ``tmp_path/mcp.json``."""
    return KiroRegistrar(config_path=tmp_path / "mcp.json")


def _spec(env: dict[str, str] | None = None) -> ServerSpec:
    return ServerSpec(
        name="headroom",
        command=_RESOLVED_COMMAND,
        args=_RESOLVED_ARGS,
        env=env or {},
    )


def _config_path(tmp_path: Path) -> Path:
    return tmp_path / "mcp.json"


def _write_config(tmp_path: Path, data: dict) -> Path:
    cfg = _config_path(tmp_path)
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps(data))
    return cfg


# ----------------------------------------------------------------------
# detect()
# ----------------------------------------------------------------------


def test_detect_true_when_cli_on_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "headroom.mcp_registry.kiro.shutil.which",
        lambda name: "/usr/local/bin/kiro" if name == "kiro" else None,
    )
    # Make ~/.kiro / parent-dir checks fail so PATH is the only trigger.
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    reg = KiroRegistrar(config_path=tmp_path / "missing" / "mcp.json")
    assert reg.detect() is True


def test_detect_true_when_parent_dir_exists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("headroom.mcp_registry.kiro.shutil.which", lambda name: None)
    fake_home = tmp_path / "home"
    fake_home.mkdir()  # no .kiro inside
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    settings_dir = tmp_path / "settings"
    settings_dir.mkdir()
    reg = KiroRegistrar(config_path=settings_dir / "mcp.json")
    assert reg.detect() is True


def test_detect_true_when_kiro_home_exists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("headroom.mcp_registry.kiro.shutil.which", lambda name: None)
    fake_home = tmp_path / "home"
    (fake_home / ".kiro").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    reg = KiroRegistrar(config_path=tmp_path / "missing" / "mcp.json")
    assert reg.detect() is True


def test_detect_false_when_nothing_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("headroom.mcp_registry.kiro.shutil.which", lambda name: None)
    fake_home = tmp_path / "home"
    fake_home.mkdir()  # no .kiro inside
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    reg = KiroRegistrar(config_path=tmp_path / "no" / "such" / "dir" / "mcp.json")
    assert reg.detect() is False


# ----------------------------------------------------------------------
# get_server()
# ----------------------------------------------------------------------


def test_get_server_returns_none_when_absent(tmp_path: Path) -> None:
    assert _make_registrar(tmp_path).get_server("headroom") is None


def test_get_server_reads_entry(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        {
            "mcpServers": {
                "headroom": {
                    "command": _RESOLVED_COMMAND,
                    "args": list(_RESOLVED_ARGS),
                    "env": {"HEADROOM_PROXY_URL": "http://127.0.0.1:9000"},
                }
            }
        },
    )
    got = _make_registrar(tmp_path).get_server("headroom")
    assert got is not None
    assert got.command == _RESOLVED_COMMAND
    assert got.args == _RESOLVED_ARGS
    assert got.env == {"HEADROOM_PROXY_URL": "http://127.0.0.1:9000"}


def test_get_server_returns_none_for_non_dict_entry(tmp_path: Path) -> None:
    _write_config(tmp_path, {"mcpServers": {"headroom": "not-an-object"}})
    assert _make_registrar(tmp_path).get_server("headroom") is None


@pytest.mark.parametrize("contents", ["", "not json", "{", "[]"])
def test_get_server_robust_to_bad_json(tmp_path: Path, contents: str) -> None:
    cfg = _config_path(tmp_path)
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(contents)
    assert _make_registrar(tmp_path).get_server("headroom") is None


# ----------------------------------------------------------------------
# register_server() — happy paths
# ----------------------------------------------------------------------


def test_register_on_empty_config_registers(tmp_path: Path) -> None:
    reg = _make_registrar(tmp_path)
    result = reg.register_server(_spec())
    assert result.status == RegisterStatus.REGISTERED
    data = json.loads(_config_path(tmp_path).read_text())
    assert data["mcpServers"]["headroom"]["command"] == _RESOLVED_COMMAND
    assert data["mcpServers"]["headroom"]["args"] == list(_RESOLVED_ARGS)


def test_register_creates_parent_dirs(tmp_path: Path) -> None:
    cfg = tmp_path / "nested" / "settings" / "mcp.json"
    reg = KiroRegistrar(config_path=cfg)
    result = reg.register_server(_spec())
    assert result.status == RegisterStatus.REGISTERED
    assert cfg.exists()


def test_register_twice_is_already_and_byte_identical(tmp_path: Path) -> None:
    reg = _make_registrar(tmp_path)
    assert reg.register_server(_spec()).status == RegisterStatus.REGISTERED
    after_first = _config_path(tmp_path).read_bytes()
    result = reg.register_server(_spec())
    assert result.status == RegisterStatus.ALREADY
    assert _config_path(tmp_path).read_bytes() == after_first


# ----------------------------------------------------------------------
# register_server() — mismatch / force
# ----------------------------------------------------------------------


def test_register_mismatch_no_force_leaves_file_unchanged(tmp_path: Path) -> None:
    cfg = _write_config(
        tmp_path,
        {"mcpServers": {"headroom": {"command": "old-headroom", "args": ["serve"]}}},
    )
    before = cfg.read_bytes()
    result = _make_registrar(tmp_path).register_server(_spec())
    assert result.status == RegisterStatus.MISMATCH
    assert cfg.read_bytes() == before


def test_register_force_overwrites_and_preserves_unmanaged_and_other_servers(
    tmp_path: Path,
) -> None:
    _write_config(
        tmp_path,
        {
            "mcpServers": {
                "headroom": {
                    "command": "old-headroom",
                    "args": ["serve"],
                    "disabled": False,
                    "timeout": 60,
                },
                "aws-docs": {
                    "command": "uvx",
                    "args": ["awslabs.aws-documentation-mcp-server@latest"],
                },
            }
        },
    )
    result = _make_registrar(tmp_path).register_server(_spec(), force=True)
    assert result.status == RegisterStatus.REGISTERED
    data = json.loads(_config_path(tmp_path).read_text())
    headroom = data["mcpServers"]["headroom"]
    # Managed fields updated.
    assert headroom["command"] == _RESOLVED_COMMAND
    assert headroom["args"] == list(_RESOLVED_ARGS)
    # Unmanaged fields retained.
    assert headroom["disabled"] is False
    assert headroom["timeout"] == 60
    # Other servers preserved.
    assert data["mcpServers"]["aws-docs"]["command"] == "uvx"


def test_register_preserves_other_top_level_keys(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        {"someOtherKey": {"a": 1}, "mcpServers": {"other": {"command": "x"}}},
    )
    result = _make_registrar(tmp_path).register_server(_spec())
    assert result.status == RegisterStatus.REGISTERED
    data = json.loads(_config_path(tmp_path).read_text())
    assert data["someOtherKey"] == {"a": 1}
    assert data["mcpServers"]["other"] == {"command": "x"}
    assert data["mcpServers"]["headroom"]["command"] == _RESOLVED_COMMAND


# ----------------------------------------------------------------------
# unregister_server()
# ----------------------------------------------------------------------


def test_unregister_removes_only_headroom(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        {
            "mcpServers": {
                "headroom": {"command": _RESOLVED_COMMAND, "args": list(_RESOLVED_ARGS)},
                "other": {"command": "other"},
            }
        },
    )
    reg = _make_registrar(tmp_path)
    assert reg.unregister_server("headroom") is True
    data = json.loads(_config_path(tmp_path).read_text())
    assert "headroom" not in data["mcpServers"]
    assert data["mcpServers"]["other"] == {"command": "other"}


def test_unregister_returns_false_when_absent(tmp_path: Path) -> None:
    _write_config(tmp_path, {"mcpServers": {"other": {"command": "other"}}})
    assert _make_registrar(tmp_path).unregister_server("headroom") is False


def test_register_then_unregister_round_trip(tmp_path: Path) -> None:
    reg = _make_registrar(tmp_path)
    reg.register_server(_spec())
    assert reg.get_server("headroom") is not None
    assert reg.unregister_server("headroom") is True
    assert reg.get_server("headroom") is None


# ----------------------------------------------------------------------
# _write_json format & config-path resolution
# ----------------------------------------------------------------------


def test_write_json_uses_indent_2_and_trailing_newline(tmp_path: Path) -> None:
    cfg = tmp_path / "out.json"
    _write_json(cfg, {"mcpServers": {"headroom": {"command": "x"}}})
    text = cfg.read_text()
    assert text.endswith("\n")
    # indent=2 → nested keys are indented by two spaces.
    assert '\n  "mcpServers"' in text
    assert '\n    "headroom"' in text


def test_kiro_config_path_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("KIRO_MCP_CONFIG", raising=False)
    fake_home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    assert _kiro_config_path() == fake_home / ".kiro" / "settings" / "mcp.json"


def test_kiro_config_path_env_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    override = tmp_path / "custom" / "mcp.json"
    monkeypatch.setenv("KIRO_MCP_CONFIG", str(override))
    assert _kiro_config_path() == override.absolute()


def test_registrar_uses_env_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    override = tmp_path / "custom" / "mcp.json"
    monkeypatch.setenv("KIRO_MCP_CONFIG", str(override))
    reg = KiroRegistrar()
    result = reg.register_server(_spec())
    assert result.status == RegisterStatus.REGISTERED
    assert override.exists()
    data = json.loads(override.read_text())
    assert data["mcpServers"]["headroom"]["command"] == _RESOLVED_COMMAND


# ----------------------------------------------------------------------
# Error handling
# ----------------------------------------------------------------------


def test_register_writes_fresh_over_unparseable_file(tmp_path: Path) -> None:
    cfg = _config_path(tmp_path)
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text("this is not json {{{")
    result = _make_registrar(tmp_path).register_server(_spec())
    assert result.status == RegisterStatus.REGISTERED
    data = json.loads(cfg.read_text())
    assert data["mcpServers"]["headroom"]["command"] == _RESOLVED_COMMAND


def test_register_failed_when_path_unwritable(tmp_path: Path) -> None:
    # Make the config's parent a regular file so mkdir/write raises OSError.
    blocker = tmp_path / "afile"
    blocker.write_text("blocker")
    reg = KiroRegistrar(config_path=blocker / "mcp.json")
    result = reg.register_server(_spec())
    assert result.status == RegisterStatus.FAILED


# ----------------------------------------------------------------------
# Property-based tests (Hypothesis) — tasks 6.2 / 6.3 / 6.4
# ----------------------------------------------------------------------

# Server-name alphabet that never collides with the managed "headroom" key.
_NAME_ALPHABET = string.ascii_letters + string.digits + "-_"
# Keys for unmanaged fields must avoid the three managed keys.
_MANAGED_KEYS = {"command", "args", "env"}


def test_property_idempotence_of_registration() -> None:
    """Property 1: register then register again → ALREADY, file byte-identical.

    **Validates: Requirement 4, design Correctness Property 1**
    """
    pytest.importorskip("hypothesis")
    from hypothesis import given, settings
    from hypothesis import strategies as st

    other_names = st.text(alphabet=_NAME_ALPHABET, min_size=1, max_size=12).filter(
        lambda s: s != "headroom"
    )
    entry_values = st.one_of(st.text(max_size=8), st.integers(), st.booleans())
    other_entries = st.dictionaries(
        keys=st.text(alphabet=string.ascii_letters, min_size=1, max_size=8),
        values=entry_values,
        max_size=4,
    )
    starting_servers = st.dictionaries(keys=other_names, values=other_entries, max_size=4)
    spec_strategy = st.builds(
        ServerSpec,
        name=st.just("headroom"),
        command=st.text(min_size=1, max_size=12),
        args=st.lists(st.text(max_size=8), max_size=4).map(tuple),
        env=st.dictionaries(
            st.text(alphabet=string.ascii_uppercase + "_", min_size=1, max_size=6),
            st.text(max_size=8),
            max_size=3,
        ),
    )

    import tempfile

    @settings(max_examples=50, deadline=None)
    @given(servers=starting_servers, spec=spec_strategy)
    def _check(servers: dict, spec: ServerSpec) -> None:
        cfg = Path(tempfile.mkdtemp(prefix="kiro")) / "mcp.json"
        cfg.write_text(json.dumps({"mcpServers": dict(servers)}))
        reg = KiroRegistrar(config_path=cfg)
        first = reg.register_server(spec)
        assert first.status in (RegisterStatus.REGISTERED, RegisterStatus.ALREADY)
        after_first = cfg.read_bytes()
        second = reg.register_server(spec)
        assert second.status == RegisterStatus.ALREADY
        assert cfg.read_bytes() == after_first

    _check()


def test_property_round_trip_entry_mapping() -> None:
    """Property 7: _entry_to_spec(s.name, _spec_to_entry(s)) ~ s.

    **Validates: Requirement 8, design Correctness Property 7**
    """
    pytest.importorskip("hypothesis")
    from hypothesis import given, settings
    from hypothesis import strategies as st

    spec_strategy = st.builds(
        ServerSpec,
        name=st.text(min_size=1, max_size=12),
        command=st.text(min_size=1, max_size=12),
        args=st.lists(st.text(max_size=8), max_size=5).map(tuple),
        env=st.dictionaries(
            st.text(min_size=1, max_size=6), st.text(max_size=8), max_size=3
        ),
    )

    @settings(max_examples=100, deadline=None)
    @given(spec=spec_strategy)
    def _check(spec: ServerSpec) -> None:
        round_tripped = _entry_to_spec(spec.name, _spec_to_entry(spec))
        assert _specs_equivalent(round_tripped, spec)

    _check()


def test_property_unknown_field_preservation() -> None:
    """Property 4: force re-register updates managed fields, retains unmanaged.

    **Validates: Requirement 7, design Correctness Property 4**
    """
    pytest.importorskip("hypothesis")
    from hypothesis import given, settings
    from hypothesis import strategies as st

    unmanaged_keys = st.text(alphabet=string.ascii_letters + "_", min_size=1, max_size=10).filter(
        lambda k: k not in _MANAGED_KEYS
    )
    unmanaged_values = st.one_of(st.booleans(), st.integers(), st.text(max_size=8))
    unmanaged = st.dictionaries(keys=unmanaged_keys, values=unmanaged_values, max_size=5)
    spec_strategy = st.builds(
        ServerSpec,
        name=st.just("headroom"),
        command=st.text(min_size=1, max_size=12),
        args=st.lists(st.text(max_size=8), max_size=4).map(tuple),
        env=st.dictionaries(
            st.text(alphabet=string.ascii_uppercase + "_", min_size=1, max_size=6),
            st.text(max_size=8),
            max_size=3,
        ),
    )

    import tempfile

    @settings(max_examples=50, deadline=None)
    @given(extra=unmanaged, spec=spec_strategy)
    def _check(extra: dict, spec: ServerSpec) -> None:
        cfg = Path(tempfile.mkdtemp(prefix="kiro")) / "mcp.json"
        starting_entry = {"command": "old-command", "args": ["old"], **extra}
        cfg.write_text(json.dumps({"mcpServers": {"headroom": starting_entry}}))
        reg = KiroRegistrar(config_path=cfg)
        result = reg.register_server(spec, force=True)
        assert result.status == RegisterStatus.REGISTERED
        data = json.loads(cfg.read_text())
        entry = data["mcpServers"]["headroom"]
        assert entry["command"] == spec.command
        if spec.args:
            assert entry["args"] == list(spec.args)
        else:
            assert "args" not in entry
        # Every unmanaged key/value survives the force rewrite.
        for key, value in extra.items():
            assert entry[key] == value

    _check()
