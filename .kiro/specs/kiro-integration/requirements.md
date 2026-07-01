# Requirements Document

## Introduction

Headroom's original Kiro integration treated Kiro like Cursor: it started the
local Headroom proxy and pointed Kiro at it by injecting a custom model base URL
into Kiro settings. That approach cannot work — Kiro (the IDE and `kiro-cli`)
routes model traffic directly to its own backend using AWS authentication and
exposes no configurable base URL, so the proxy never sees Kiro's traffic. Kiro's
actual, documented extension surface is the Model Context Protocol (MCP), read
from `~/.kiro/settings/mcp.json` under the standard `mcpServers` schema.

This feature re-scopes the Kiro integration to be MCP-native. It adds one
concrete registrar — the Kiro_Registrar — to Headroom's existing MCP registrar
fleet. The Kiro_Registrar reads and writes `~/.kiro/settings/mcp.json` under the
`mcpServers` key so that `headroom mcp install` (and `headroom mcp install
--agent kiro`) install the Headroom_Server into Kiro with no other CLI change.
The change also removes the defunct proxy/base-URL Kiro integration in full and
updates the README accordingly.

These requirements are derived directly from the approved design document and are
traceable to its Correctness Properties 1 through 7.

## Glossary

- **Headroom_CLI**: The `headroom` command-line interface, including the
  `headroom mcp install` and `headroom mcp serve` subcommands and any remaining
  `wrap`/`unwrap` subcommands for non-Kiro tools.
- **MCP_Installer**: The orchestration layer in `headroom/mcp_registry/install.py`,
  namely `install_everywhere` and its supporting functions
  (`get_all_registrars`, `build_headroom_spec`), reached from the CLI via
  `headroom mcp install`.
- **MCP_Registry_Fleet**: The list of `MCPRegistrar` instances returned by
  `get_all_registrars()` that the MCP_Installer iterates over.
- **Kiro_Registrar**: The concrete `MCPRegistrar` subclass in
  `headroom/mcp_registry/kiro.py` with `name = "kiro"` and
  `display_name = "Kiro"` that owns Kiro's MCP config schema and write path.
- **MCPRegistrar_Contract**: The abstract base contract in
  `headroom/mcp_registry/base.py` defining the `detect`, `get_server`,
  `register_server`, and `unregister_server` methods.
- **Kiro_MCP_Config**: Kiro's global MCP configuration file, resolved to
  `~/.kiro/settings/mcp.json` unless overridden by `KIRO_MCP_CONFIG`.
- **Config_Path_Resolver**: The `_kiro_config_path()` helper in
  `headroom/mcp_registry/kiro.py` that resolves the active Kiro_MCP_Config path.
- **mcpServers**: The top-level JSON object in the Kiro_MCP_Config whose keys are
  server names and whose values are entries with `command`, `args`, `env`, and
  optional unmanaged fields.
- **Server_Spec**: The universal `ServerSpec` type from
  `headroom/mcp_registry/base.py`, carrying `name`, `command`, `args`, and `env`.
- **Headroom_Server**: The Headroom MCP server launched by `headroom mcp serve`
  over stdio, described by the canonical Server_Spec that
  `build_headroom_spec(proxy_url)` produces.
- **Managed_Entry**: The single `mcpServers` entry whose key equals the
  registered server name (for the Headroom_Server, `"headroom"`).
- **Unmanaged_Field**: A per-entry key the Kiro_Registrar preserves but does not
  set, such as `disabled`, `timeout`, or `autoApprove`.
- **RegisterStatus**: The outcome enumeration from
  `headroom/mcp_registry/base.py` with values `REGISTERED`, `ALREADY`,
  `MISMATCH`, `FAILED`, `NOT_DETECTED`, and `NO_SDK`.
- **Default_Proxy_URL**: The value `http://127.0.0.1:8787` used when no proxy URL
  is supplied to `build_headroom_spec`.
- **ToolTarget**: The `ToolTarget` enumeration in `headroom/install/models.py`
  that previously included a `KIRO` member.
- **Install_Registry**: The dispatch tables in
  `headroom/providers/install_registry.py` (`_ENV_BUILDERS`,
  `_PROVIDER_SCOPE_HANDLERS`) that previously held `kiro` entries.

## Requirements

### Requirement 1: Install the Headroom MCP server into Kiro

**User Story:** As a Kiro user, I want `headroom mcp install` to register Headroom's MCP server into Kiro, so that Kiro can call Headroom's compression tools on demand.

#### Acceptance Criteria

1. WHEN a user runs `headroom mcp install`, THE MCP_Installer SHALL include the Kiro_Registrar in the MCP_Registry_Fleet it iterates over.
2. WHEN a user runs `headroom mcp install --agent kiro`, THE MCP_Installer SHALL select only the Kiro_Registrar for registration.
3. WHEN the Kiro_Registrar registers the Headroom_Server, THE Kiro_Registrar SHALL write a Managed_Entry keyed `headroom` under mcpServers whose `command` and `args` launch `headroom mcp serve`.
4. WHERE the supplied proxy URL differs from the Default_Proxy_URL, THE Kiro_Registrar SHALL include a `HEADROOM_PROXY_URL` value in the Managed_Entry `env`.
5. WHEN the Kiro_Registrar detects that Kiro is not installed, THE MCP_Installer SHALL record a `NOT_DETECTED` RegisterStatus for the Kiro_Registrar and SHALL make no write to the Kiro_MCP_Config.

### Requirement 2: Kiro_Registrar contract conformance

**User Story:** As a Headroom maintainer, I want the Kiro_Registrar to implement the same contract as every other registrar, so that the fleet treats all agents uniformly.

#### Acceptance Criteria

1. THE Kiro_Registrar SHALL implement `detect`, `get_server`, `register_server`, and `unregister_server` with parameter lists and return types identical to the MCPRegistrar_Contract.
2. THE Kiro_Registrar SHALL expose the identifier `kiro` as its `name` and `Kiro` as its `display_name`.
3. WHEN `get_server` is called with a server name whose mcpServers value is a JSON object, THE Kiro_Registrar SHALL return the parsed Server_Spec for that entry.
4. IF `get_server` is called with a server name that is absent or whose mcpServers value is not a JSON object, THEN THE Kiro_Registrar SHALL return no Server_Spec.

### Requirement 3: Kiro_MCP_Config path resolution

**User Story:** As a Kiro user, I want Headroom to find my Kiro MCP config automatically while letting me override it, so that installation works without manual path configuration.

#### Acceptance Criteria

1. WHERE the `KIRO_MCP_CONFIG` environment variable is set to a value containing at least one non-whitespace character, THE Config_Path_Resolver SHALL return that value as the Kiro_MCP_Config path, expanding a leading `~` to the user home directory.
2. WHERE the `KIRO_MCP_CONFIG` override is unset or contains only whitespace, THE Config_Path_Resolver SHALL return `~/.kiro/settings/mcp.json` under the user home directory.
3. THE Config_Path_Resolver SHALL return an absolute path.

### Requirement 4: Idempotent registration

**User Story:** As a Kiro user, I want re-running the install to be safe, so that repeated installs never corrupt my Kiro MCP config.

#### Acceptance Criteria

1. WHEN `register_server` is called with a Server_Spec and an equivalent Managed_Entry already exists in the Kiro_MCP_Config, THE Kiro_Registrar SHALL return the `ALREADY` RegisterStatus and SHALL perform no write.
2. WHEN `register_server` is called a second time with the same Server_Spec after a successful first registration, THE Kiro_Registrar SHALL leave the Kiro_MCP_Config byte-for-byte identical to its state after the first registration.

*Validates: Requirement 1, and design Correctness Property 1*

### Requirement 5: MISMATCH-safety without force

**User Story:** As a Kiro user, I want Headroom to refuse to overwrite a differing existing entry, so that I never lose a configuration I set intentionally.

#### Acceptance Criteria

1. IF a Managed_Entry for the requested server name already exists with a spec different from the requested Server_Spec AND `force` is False, THEN THE Kiro_Registrar SHALL return the `MISMATCH` RegisterStatus.
2. WHEN the Kiro_Registrar returns the `MISMATCH` RegisterStatus, THE Kiro_Registrar SHALL leave the Kiro_MCP_Config unchanged.

*Validates: design Correctness Property 2*

### Requirement 6: Force overwrite

**User Story:** As a Kiro user, I want a way to force Headroom to replace a differing entry, so that I can update the registration when I choose to.

#### Acceptance Criteria

1. WHEN `register_server` is called with `force` set to True AND a Managed_Entry for the requested server name already exists with a different spec, THE Kiro_Registrar SHALL overwrite the Managed_Entry `command`, `args`, and `env` and SHALL return the `REGISTERED` RegisterStatus.

*Validates: design Correctness Property 2*

### Requirement 7: Preservation of other servers and unmanaged fields

**User Story:** As a Kiro user, I want Headroom to touch only its own entry, so that my other MCP servers and their settings survive every install.

#### Acceptance Criteria

1. WHEN the Kiro_Registrar writes the Kiro_MCP_Config, THE Kiro_Registrar SHALL preserve every mcpServers entry other than the Managed_Entry with its value unchanged.
2. WHEN the Kiro_Registrar writes the Kiro_MCP_Config, THE Kiro_Registrar SHALL preserve every top-level key other than `mcpServers` with its value unchanged.
3. WHEN the Kiro_Registrar overwrites an existing Managed_Entry with `force` set to True, THE Kiro_Registrar SHALL retain each Unmanaged_Field and its value on that entry.

*Validates: design Correctness Properties 3 and 4*

### Requirement 8: Round-trip stability of entry mapping

**User Story:** As a Headroom maintainer, I want serializing and re-reading an entry to be lossless, so that idempotence and equivalence checks behave correctly.

#### Acceptance Criteria

1. WHEN a Server_Spec is serialized to an mcpServers entry and then parsed back into a Server_Spec, THE Kiro_Registrar SHALL produce a Server_Spec whose `name`, `command`, `args`, and `env` are equal to those of the original Server_Spec.
2. WHEN a Server_Spec has empty `args`, THE Kiro_Registrar SHALL omit the `args` key from the serialized mcpServers entry.
3. WHEN a Server_Spec has empty `env`, THE Kiro_Registrar SHALL omit the `env` key from the serialized mcpServers entry.

*Validates: design Correctness Property 7*

### Requirement 9: Detect accuracy

**User Story:** As a Kiro user, I want Headroom to install into Kiro only when Kiro is present, so that the fleet reports accurate detection.

#### Acceptance Criteria

1. WHERE `kiro` or `kiro-cli` is on the executable search PATH, THE Kiro_Registrar SHALL return True from `detect`.
2. WHERE neither `kiro` nor `kiro-cli` is on PATH AND the `~/.kiro` directory exists, THE Kiro_Registrar SHALL return True from `detect`.
3. WHERE neither `kiro` nor `kiro-cli` is on PATH AND `~/.kiro` does not exist AND the parent directory of the Kiro_MCP_Config exists, THE Kiro_Registrar SHALL return True from `detect`.
4. WHERE none of the detection conditions hold, THE Kiro_Registrar SHALL return False from `detect`.

*Validates: design Correctness Property 5*

### Requirement 10: Unregister behavior and reversibility

**User Story:** As a Kiro user, I want `unregister` to remove only Headroom's entry, so that I can cleanly reverse an install.

#### Acceptance Criteria

1. WHEN `unregister_server` is called with a server name present under mcpServers, THE Kiro_Registrar SHALL remove only that entry and SHALL return True.
2. WHEN the Kiro_Registrar removes an entry, THE Kiro_Registrar SHALL preserve every other mcpServers entry and every top-level key unchanged.
3. IF `unregister_server` is called with a server name that is absent from mcpServers, THEN THE Kiro_Registrar SHALL return False and SHALL make no write.
4. IF `unregister_server` cannot write the Kiro_MCP_Config, THEN THE Kiro_Registrar SHALL return False.
5. WHEN `register_server` for the Headroom_Server is followed by `unregister_server` for the same name, THE Kiro_Registrar SHALL yield an mcpServers object with no entry for that name and with every other entry equal to the pre-registration state.

*Validates: design Correctness Properties 3 and 6*

### Requirement 11: Fleet wiring and agent selection

**User Story:** As a Headroom maintainer, I want the Kiro_Registrar wired into the fleet with no CLI change, so that `--agent kiro` works purely by virtue of the registrar's name.

#### Acceptance Criteria

1. THE MCP_Registry_Fleet returned by `get_all_registrars` SHALL include exactly one Kiro_Registrar instance.
2. THE `headroom/mcp_registry` package SHALL export the Kiro_Registrar in its public interface.
3. WHEN `install_everywhere` is invoked with the agent set restricted to `kiro`, THE MCP_Installer SHALL match the value `kiro` to the Kiro_Registrar `name` and dispatch registration to the Kiro_Registrar.

### Requirement 12: Error handling

**User Story:** As a Kiro user, I want safe, predictable behavior when my config is missing or malformed, so that Headroom never leaves a partially written or corrupted file.

#### Acceptance Criteria

1. IF the Kiro_MCP_Config contains invalid JSON, THEN THE Kiro_Registrar SHALL treat the file as an empty configuration AND `get_server` SHALL return no Server_Spec.
2. WHEN the Kiro_MCP_Config is unparseable AND `register_server` is called, THE Kiro_Registrar SHALL write a fresh mcpServers object containing the Managed_Entry, following the established overwrite-on-corruption convention shared with the sibling registrars.
3. IF writing the Kiro_MCP_Config raises an operating-system write error, THEN THE Kiro_Registrar SHALL return the `FAILED` RegisterStatus with a detail message AND SHALL report no partial write as success.
4. WHERE the Kiro_MCP_Config or its parent directory does not exist when `register_server` runs, THE Kiro_Registrar SHALL create the missing parent directories and write the file.

*Validates: design Correctness Property 2 (parseable-config caveat)*

### Requirement 13: Removal of the defunct proxy/base-URL Kiro integration

**User Story:** As a Headroom maintainer, I want the non-functional proxy-era Kiro code removed in full, so that no misleading Kiro wrap path remains and non-Kiro tools are unaffected.

#### Acceptance Criteria

1. THE Headroom_CLI SHALL provide no `wrap kiro` subcommand.
2. THE Headroom_CLI SHALL provide no `unwrap kiro` subcommand.
3. THE Install_Registry SHALL contain no `kiro` entry in `_ENV_BUILDERS` and no `kiro` entry in `_PROVIDER_SCOPE_HANDLERS`.
4. THE ToolTarget enumeration SHALL contain no `KIRO` member.
5. THE `headroom` package SHALL contain no `providers/kiro` slice and no `kiro_settings_path` resolver.
6. THE test suite SHALL contain none of the proxy-era Kiro tests.
7. WHEN a user wraps or unwraps any tool other than Kiro, THE Headroom_CLI SHALL behave identically to its behavior before the defunct Kiro integration was removed.

### Requirement 14: README documentation updates

**User Story:** As a new Headroom user, I want the documentation to describe Kiro as an MCP-native agent, so that I install it correctly and am not misled by the removed wrap path.

#### Acceptance Criteria

1. THE README SHALL list no `kiro` entry in the `headroom wrap` supported-tools feature list.
2. THE README SHALL list no `kiro` entry in the `headroom unwrap` supported-tools list.
3. THE README agent compatibility matrix SHALL describe Kiro as an MCP-native agent integrated via `headroom mcp install`.
4. THE README SHALL document Kiro under the `headroom mcp install` section as an MCP-native agent that uses the standard `mcpServers` configuration.
