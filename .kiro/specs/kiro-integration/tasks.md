# Implementation Plan: Kiro Integration (MCP-native)

## Overview

This plan re-scopes the Kiro integration from the defunct proxy/base-URL wrap
path to an MCP-native registrar. Work proceeds bottom-up and destructive-first:
remove the non-functional proxy-era Kiro code so the tree is clean, then add the
new `KiroRegistrar`, wire it into the MCP registrar fleet, cover it with unit and
Hypothesis property tests, and finally update the README.

All code is Python, tested with pytest and Hypothesis, matching the repo's
existing stack. The new registrar mirrors `headroom/mcp_registry/opencode.py`
(direct JSON edit under a top-level key) combined with the `mcpServers` entry
shape used by `headroom/mcp_registry/claude.py`.

## Tasks

- [x] 1. Remove the defunct proxy/base-URL Kiro integration
  - [x] 1.1 Delete the `headroom/providers/kiro/` package
    - Delete `headroom/providers/kiro/__init__.py`, `headroom/providers/kiro/install.py`, `headroom/providers/kiro/runtime.py`, and the now-empty `headroom/providers/kiro/` directory
    - _Requirements: 13.5_

  - [x] 1.2 Remove Kiro dispatch from `headroom/providers/install_registry.py`
    - Remove the three `headroom.providers.kiro.install` imports (`_apply_kiro_provider_scope`, `_build_kiro_install_env`, `_revert_kiro_provider_scope`)
    - Remove the `"kiro": _build_kiro_install_env` entry from `_ENV_BUILDERS`
    - Remove the `"kiro": (_apply_kiro_provider_scope, _revert_kiro_provider_scope)` entry from `_PROVIDER_SCOPE_HANDLERS`
    - Leave all other registry entries untouched
    - _Requirements: 13.3_

  - [x] 1.3 Remove the Kiro CLI paths from `headroom/cli/wrap.py`
    - Remove the four Kiro runtime/install imports (`apply_provider_scope`, `revert_provider_scope`, `build_launch_env`, `render_setup_lines`)
    - Remove the `from headroom.install.paths import kiro_settings_path` import
    - Remove the `_kiro_inject_manifest` helper
    - Remove the `wrap kiro` command (function and all its options)
    - Remove the `unwrap kiro` command (`unwrap_kiro`)
    - Remove the now-unused `ToolTarget` import only if it is not used elsewhere in the file
    - _Requirements: 13.1, 13.2_

  - [x] 1.4 Remove the Kiro settings-path resolver from `headroom/install/paths.py`
    - Remove `kiro_settings_path()` and its private helper `_kiro_home()`
    - _Requirements: 13.5_

  - [x] 1.5 Remove `ToolTarget.KIRO` from `headroom/install/models.py`
    - Delete the `KIRO = "kiro"` member from the `ToolTarget` enum
    - _Requirements: 13.4_

  - [x] 1.6 Delete the proxy-era Kiro tests
    - Delete `tests/test_provider_kiro_runtime_properties.py`, `tests/test_provider_kiro_env_properties.py`, `tests/test_providers_kiro_install_property.py`, `tests/test_providers_kiro_install_idempotence.py`, `tests/test_providers_kiro_install_unit.py`, `tests/test_provider_registry_kiro.py`, `tests/test_cli/test_wrap_kiro.py`, `tests/test_cli/test_unwrap_kiro.py`
    - Remove the Kiro cases appended to `tests/test_install/test_paths.py` (the `kiro_settings_path` tests)
    - _Requirements: 13.6_

  - [x] 1.7 Verify removal is clean
    - Confirm imports still resolve (`headroom.cli.wrap`, `headroom.providers.install_registry`, `headroom.install.paths`, `headroom.install.models`)
    - Run the wrap/registry/paths/models test suites and confirm non-Kiro behavior is unchanged
    - Confirm no residual `kiro` references remain in the edited locations (`install_registry.py`, `wrap.py`, `paths.py`, `models.py`)
    - _Requirements: 13.7_

- [x] 2. Checkpoint - defunct integration removed
  - Ensure all tests pass, ask the user if questions arise.

- [x] 3. Implement the KiroRegistrar (`headroom/mcp_registry/kiro.py` NEW)
  - [x] 3.1 Implement config resolution and JSON IO helpers
    - Create `headroom/mcp_registry/kiro.py` importing `MCPRegistrar`, `ServerSpec`, `RegisterResult`, `RegisterStatus` from `headroom/mcp_registry/base.py`
    - Implement `_kiro_config_path()`: honor a non-whitespace `KIRO_MCP_CONFIG` override (expand leading `~`, return absolute), else `Path.home() / ".kiro" / "settings" / "mcp.json"`
    - Implement `_read_json(path)` (tolerant read → `{}` when absent/unparseable) and `_write_json(path, data)` (`mkdir` parents, `indent=2`, trailing newline), matching the convention in `opencode.py`
    - _Requirements: 3.1, 3.2, 3.3, 12.1, 12.4_

  - [x] 3.2 Implement the entry <-> spec mapping helpers
    - `_entry_to_spec(name, entry)`: read `command`/`args`/`env` into a `ServerSpec`, ignoring unmanaged keys
    - `_spec_to_entry(spec, existing=None)`: shallow-copy `existing` first so unmanaged keys survive, overwrite `command`/`args`/`env`, and omit `args`/`env` when empty (round-trip stable)
    - `_specs_equivalent(a, b)`: equal `name`, `command`, `args`, `env`
    - `_diff_specs(existing, requested)`: human-readable field-level diff for MISMATCH detail
    - _Requirements: 7.3, 8.1, 8.2, 8.3_

  - [x] 3.3 Implement the `KiroRegistrar` class
    - Define `KiroRegistrar(MCPRegistrar)` with `name = "kiro"`, `display_name = "Kiro"`, and `__init__(self, *, config_path=None)`
    - `detect()`: True iff `kiro`/`kiro-cli` on PATH, or `~/.kiro` exists, or the config parent dir exists
    - `get_server(name)`: return parsed `ServerSpec` when the `mcpServers` entry is a dict, else `None`; never raise on missing/unparseable file
    - `register_server(spec, *, force=False)`: `ALREADY` (no write) when equivalent; `MISMATCH` (no write) when different and not `force`; otherwise `_write_entry`
    - `_write_entry(spec)`: `mkdir` parents, read data, `setdefault("mcpServers", {})` (reset when not a dict), merge via `_spec_to_entry(spec, existing=prior)`, write; `FAILED` on `OSError`; preserve every other server and every top-level key
    - `unregister_server(name)`: remove only the named entry, `True` on removal, `False` when absent or unwritable, preserving all other entries/keys
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 4.1, 4.2, 5.1, 5.2, 6.1, 7.1, 7.2, 7.3, 9.1, 9.2, 9.3, 9.4, 10.1, 10.2, 10.3, 10.4, 10.5, 12.2, 12.3, 12.4_

- [x] 4. Wire the KiroRegistrar into the fleet
  - [x] 4.1 Register in `headroom/mcp_registry/install.py`
    - Add `from .kiro import KiroRegistrar` and append `KiroRegistrar()` to the list returned by `get_all_registrars()`
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 11.1, 11.3_

  - [x] 4.2 Export from `headroom/mcp_registry/__init__.py`
    - Import `KiroRegistrar` and add it to `__all__`, mirroring the other registrars
    - _Requirements: 11.2_

- [x] 5. Checkpoint - registrar wired into the fleet
  - Ensure all tests pass, ask the user if questions arise.

- [x] 6. Test the KiroRegistrar
  - [x]* 6.1 Write unit tests (`tests/test_mcp_registry/test_kiro_registrar.py` NEW)
    - Model on `tests/test_mcp_registry/test_claude_registrar.py`, pointing the registrar at a `tmp_path` via `config_path=`/`KIRO_MCP_CONFIG`
    - Cover: `detect()` true/false across PATH and `~/.kiro`/parent-dir presence; register on empty → `REGISTERED` writing `mcpServers.headroom`; register twice → `ALREADY` with file unchanged (Property 1); pre-existing different spec + no force → `MISMATCH` with no write (Property 2); `force=True` overwrite preserving unmanaged keys and other servers (Properties 3, 4); `get_server` `None` for absent/non-dict/unparseable; `unregister_server` removes only `headroom` and register→unregister round trip (Properties 3, 6); `_write_json` output uses `indent=2` with a trailing newline
    - _Requirements: 2.3, 2.4, 4.1, 4.2, 5.1, 5.2, 6.1, 7.1, 7.2, 7.3, 9.1, 9.2, 9.3, 9.4, 10.1, 10.2, 10.3, 10.5, 12.1, 12.2, 12.3_

  - [x]* 6.2 Write property test for idempotent registration
    - **Property 1: Idempotence of registration**
    - Over arbitrary starting `mcpServers` objects and `ServerSpec`s, assert a second `register_server` returns `ALREADY` and leaves the file byte-for-byte identical
    - **Validates: Requirement 4, design Correctness Property 1**

  - [x]* 6.3 Write property test for round-trip mapping
    - **Property 7: Round-trip stability of entry mapping**
    - Over arbitrary `ServerSpec`s, assert `_entry_to_spec(s.name, _spec_to_entry(s))` is `_specs_equivalent` to `s`
    - **Validates: Requirement 8, design Correctness Property 7**

  - [x]* 6.4 Write property test for unknown-field preservation
    - **Property 4: Unknown-field preservation**
    - Over entries carrying random unmanaged keys, assert a `force=True` re-register updates `command`/`args`/`env` while retaining each unmanaged key and value
    - **Validates: Requirement 7, design Correctness Property 4**

  - [x]* 6.5 Update fleet wiring tests (`tests/test_mcp_registry/test_install.py` EDIT)
    - Assert `KiroRegistrar` appears exactly once in `get_all_registrars()` (by `name == "kiro"`) and that `install_everywhere(agents=["kiro"], ...)` dispatches to it
    - _Requirements: 1.1, 1.2, 11.1, 11.3_

- [x] 7. Update README documentation
  - [x] 7.1 Re-scope Kiro in `README.md`
    - Remove `kiro` from the `headroom wrap` supported-tools feature list (~line 50)
    - Change the compatibility-matrix Kiro row (~line 209) to MCP-native, e.g. `headroom mcp install (--agent kiro) → ~/.kiro/settings/mcp.json`
    - Remove `kiro` from the `headroom unwrap` supported-tools list (~line 213)
    - Document Kiro under the `headroom mcp install` section as an MCP-native agent that uses the standard `mcpServers` configuration
    - _Requirements: 14.1, 14.2, 14.3, 14.4_

- [x] 8. Final checkpoint - full suite green
  - Run the full Kiro MCP test suite plus a regression sweep of `tests/test_mcp_registry/`, the wrap/registry/paths tests, and confirm no residual `kiro` references remain in the removed locations
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional test sub-tasks and can be skipped for a faster MVP; core implementation and removal tasks are never optional.
- Each task references the specific requirements (and, where applicable, the design correctness property) it satisfies for traceability.
- The build is destructive-first then bottom-up: removal clears the defunct path, then `kiro.py` is built helpers-up, then the fleet is wired, then tests, then the README.
- `3.1`, `3.2`, and `3.3` all write `headroom/mcp_registry/kiro.py`, and `6.1`–`6.4` all write `tests/test_mcp_registry/test_kiro_registrar.py`, so each set is scheduled across separate waves to avoid write conflicts.
- Property tests use Hypothesis; unit and fleet tests use pytest, matching the repo's existing stack.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "1.2", "1.3", "1.4", "1.5", "1.6", "7.1"] },
    { "id": 1, "tasks": ["1.7"] },
    { "id": 2, "tasks": ["3.1"] },
    { "id": 3, "tasks": ["3.2"] },
    { "id": 4, "tasks": ["3.3"] },
    { "id": 5, "tasks": ["4.1", "4.2"] },
    { "id": 6, "tasks": ["6.1", "6.5"] },
    { "id": 7, "tasks": ["6.2"] },
    { "id": 8, "tasks": ["6.3"] },
    { "id": 9, "tasks": ["6.4"] }
  ]
}
```
