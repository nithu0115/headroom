# Implementation Plan: MCP Gateway

## Overview

This plan builds the MCP Gateway as an additive Python package (`headroom/mcp_gateway/`)
plus a thin registrar (`headroom/mcp_registry/gateway.py`) and CLI/steering/README updates.
It reuses the existing building blocks — `ServerSpec`/`MCPRegistrar`/`KiroRegistrar`
(`headroom/mcp_registry/`), the `HeadroomMCPServer` scaffolding + `CompressionStore` +
`savings_ledger` (`headroom/ccr/mcp_server.py`, `headroom/cache/compression_store.py`),
the `EmbeddingScorer` ONNX stack (`headroom/relevance/embedding.py`), and
`headroom.compress` — and never reimplements them.

Work is ordered strictly bottom-up so each task builds on the previous one with no orphaned
code: data models → config → downstream manager → tool index → search → server/meta-tools →
registrar → CLI → steering/README. Development is test-driven: every implementation task ends
with building the package (`uv run maturin build --profile ci`) and running the relevant
tests (`uv run pytest`). Property-based tests use `hypothesis` (already a dev dependency).

Build command: `uv run maturin build --profile ci`
Test command: `uv run pytest`

## Tasks

- [x] 1. Create package scaffolding and core data models
  - [x] 1.1 Create `headroom/mcp_gateway/` package with data models
    - Create `headroom/mcp_gateway/__init__.py` with package exports
    - In `headroom/mcp_gateway/index.py`, define the `ToolIndexEntry` dataclass
      (`server`, `tool`, `namespaced_id`, `description`, `input_schema`,
      `embedding: np.ndarray | None`, `keyword_tokens: tuple[str, ...]`) — no build logic yet
    - In `headroom/mcp_gateway/config.py`, define the `DownstreamSpec` dataclass
      (composing the existing `ServerSpec` for `transport == "stdio"`, plus `url`/`headers`
      for `http`) and the `GatewayConfigModel` dataclass with the design defaults
      (`find_limit_default=5`, `compress_results=False`, `compress_min_tokens=1000`,
      `connect_timeout_s=20.0`, `call_timeout_s=120.0`)
    - Import `ServerSpec` from `headroom.mcp_registry.base`; do not redefine it
    - _Requirements: 4.2, 9.7_

  - [x]* 1.2 Write unit tests for data model construction and defaults
    - Assert `DownstreamSpec` composes `ServerSpec` for stdio and holds `url`/`headers` for http
    - Assert `GatewayConfigModel` default field values match the design
    - _Requirements: 4.2, 9.7_

- [x] 2. Implement configuration resolution
  - [x] 2.1 Implement `resolve_gateway_config()` in `headroom/mcp_gateway/config.py`
    - Resolve the source in priority order: `HEADROOM_GATEWAY_CONFIG` env path →
      `~/.kiro/settings/headroom-gateway.json` → the client's `~/.kiro/settings/mcp.json`,
      selecting the first available and parseable source and using it exclusively
    - Reuse `KiroRegistrar`'s JSON read helpers / `mcpServers` entry shape so stdio entries
      map onto the existing schema (`_entry_to_spec`)
    - Always exclude the `headroom-gateway` self entry from the resolved downstream list,
      even if an `include` filter names it
    - Apply `include` first, then `exclude`; ignore filter names not present in the source
    - On unreadable/unparseable highest-priority source, return an empty downstream list
      (never raise) so the gateway still serves the meta-tools
    - _Requirements: 9.1, 9.2, 9.3, 9.4, 9.5, 9.6, 9.7_

  - [x]* 2.2 Write property test for config resolution filtering and self-exclusion
    - **Property P10: Config resolution filtering and self-exclusion**
    - **Validates: Requirements 9.2, 9.3, 9.4**
    - Use `hypothesis` to generate random server-name maps + include/exclude filters; assert
      the resolved list excludes `headroom-gateway`, is a subset of `include` when set, and
      contains none of the `exclude` names

  - [x]* 2.3 Write unit tests for source precedence and invalid config
    - Cover env override wins, dedicated file next, `mcp.json` last, and empty-on-invalid
    - _Requirements: 9.1, 9.6_

- [x] 3. Implement the downstream client manager
  - [x] 3.1 Implement `DownstreamClientManager` and `AggregationResult` in `headroom/mcp_gateway/downstream.py`
    - `start(specs)` connects every downstream concurrently
      (`asyncio.gather(..., return_exceptions=True)`) with a 30s connect timeout; spawn stdio
      via `mcp.client.stdio` using configured `command`/`args`/`env`; connect http via
      streamable-http/sse using configured `url`/`headers`
    - Call `list_tools()` exactly once per established connection (30s response timeout)
    - Return `AggregationResult(ok={server: [Tool]}, failed={server: reason})` partitioning
      every configured server into exactly one set; a failure never raises; a server enters
      `ok` only after a connection is established (empty tool list still counts as healthy)
    - Route `call(server, tool, arguments)` to that server's session only; expose `status()`
      (health with a 5s check) and graceful `shutdown()`
    - Launch downstreams with their own configured `command`/`args`/`env`/`headers`
      unmodified (same trust boundary); never log `env`/`headers` values
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8, 3.1, 3.5, 7.1, 7.2, 8.1, 8.2, 12.1, 12.2, 12.9_

  - [x]* 3.2 Write property test for downstream failure isolation
    - **Property P5: Isolation**
    - **Validates: Requirements 2.5, 2.6, 2.7, 3.1, 3.2, 3.3, 3.4, 7.7**
    - Use `hypothesis` with fake in-process sessions where a random subset fails on
      start/list/call; assert `start` partitions every server into exactly one of `ok`/`failed`
      without raising and healthy servers remain routable

  - [x]* 3.3 Write unit tests for single-listing and health reporting
    - **Property P8: Single aggregation listing** — assert `list_tools` is called exactly once
      per healthy server during aggregation
    - **Validates: Requirement 2.2**
    - Add unit cases for `status()` marking a non-responsive server "unavailable" within 5s
    - _Requirements: 8.1, 8.2_

- [x] 4. Implement the tool index build
  - [x] 4.1 Implement `ToolIndex` with `build()` in `headroom/mcp_gateway/index.py`
    - Create exactly one `ToolIndexEntry` per tool of each healthy server; assign
      `namespaced_id = f"{server}::{tool}"`; store `input_schema` byte-equal (no
      transform/re-serialize)
    - Reject duplicate `namespaced_id` collisions (exclude colliding entry, record error,
      keep non-colliding); exclude tools with missing/unparseable `input_schema` (record
      error, keep indexing the rest)
    - Precompute normalized `keyword_tokens` from name + description; compute embeddings via
      the existing `EmbeddingScorer` when ONNX is available, else leave `embedding=None`
    - Maintain the loop invariant: after k servers the index holds exactly those k servers'
      entries with unique ids; final size = total healthy tools minus exclusions
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 4.8_

  - [x]* 4.2 Write property test for namespacing
    - **Property P7: Namespacing**
    - **Validates: Requirements 4.1, 4.2, 4.3, 4.4, 4.8**
    - Use `hypothesis` to generate `{server: [tool]}` maps including duplicate tool names
      across servers; assert unique `namespaced_id`s, one entry per aggregated tool, and index
      size equals total healthy tool count

  - [x]* 4.3 Write property test for schema fidelity at index storage
    - **Property P3: Schema fidelity**
    - **Validates: Requirements 4.6, 5.5, 6.1**
    - Use `hypothesis` to generate random input schemas; assert the schema stored by `build`
      is byte-equal to the advertised schema

  - [x]* 4.4 Write unit tests for collision and unparseable-schema exclusion
    - Cover duplicate `namespaced_id` rejection and missing/unparseable schema exclusion with
      recorded errors, non-colliding entries retained
    - _Requirements: 4.5, 4.7_

- [x] 5. Implement tool search
  - [x] 5.1 Implement `ToolSearch.search(query, limit)` in `headroom/mcp_gateway/search.py`
    - Rank by cosine similarity when the `EmbeddingScorer` is available; fall back to a
      deterministic keyword (BM25) function of `(query, index)` when unavailable
    - Return at most `limit` entries in descending score order; break ties by `namespaced_id`
      ascending; return an empty list when nothing matches (including empty index)
    - Reject `limit < 1` or non-integer `limit` by raising a validation error handled by the
      caller (no partial results)
    - _Requirements: 5.1, 5.2, 5.3, 5.7, 5.8_

  - [x]* 5.2 Write property test for keyword-fallback determinism
    - **Property P6: Discovery relevance / determinism**
    - **Validates: Requirements 5.1, 5.3**
    - Use `hypothesis` to assert the keyword-fallback `search` is a pure function of
      `(query, index)` returning ≤ `limit` entries with a stable `namespaced_id` tie-break

  - [x]* 5.3 Write unit tests for limit validation and empty results
    - Cover `limit < 1`/non-integer rejection and empty-index/no-match empty result
    - _Requirements: 5.7, 5.8_

- [x] 6. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 7. Implement the MCP gateway server and meta-tools
  - [x] 7.1 Implement `MCPGatewayServer` skeleton in `headroom/mcp_gateway/server.py`
    - Model on `HeadroomMCPServer` (stdio `Server`, `Tool`, `TextContent`, shared
      `get_compression_store()`, client attribution, `savings_ledger`)
    - `start()` calls `DownstreamClientManager.start()` (isolating failures) then builds the
      `ToolIndex`; `run_stdio()` serves over stdio; `cleanup()` shuts down downstream sessions
    - Register a `list_tools` handler that returns ONLY the four meta-tools
      `{find_tools, invoke_tool, describe_tool, list_servers}` regardless of downstream count
      (including zero), and a `call_tool` dispatcher wired to per-tool handlers
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 3.4_

  - [x] 7.2 Implement `_handle_find_tools`
    - Call `ToolSearch.search`; apply `find_limit_default` when `limit` omitted; render each
      result with `namespaced_id`, `server`, `tool`, `description`, and byte-equal
      `input_schema`; return a structured error on invalid `limit`; omit unavailable servers
    - _Requirements: 5.1, 5.4, 5.5, 5.6, 5.7, 5.8, 3.3, 1.5, 1.6_

  - [x]* 7.3 Write unit tests for find_tools completeness and errors
    - **Property P9: Discovery result completeness** — every returned entry includes all of
      `namespaced_id`, `server`, `tool`, `description`, `input_schema`
    - **Validates: Requirement 5.4**
    - Add cases for `find_limit_default` applied and invalid-limit structured error
    - _Requirements: 5.6, 5.7_

  - [x] 7.4 Implement `_handle_describe_tool`
    - Resolve a `namespaced_id` or a unique bare tool name; return `description` + byte-equal
      `input_schema`; disambiguation error listing matching ids for ambiguous bare names;
      not-found error for no match; validation error for empty/whitespace name (no lookup)
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 1.5, 1.6_

  - [x]* 7.5 Write unit tests for describe_tool resolution paths
    - Cover namespaced hit, unique bare-name hit (byte-equal schema), ambiguous disambiguation,
      not-found, and empty/whitespace validation error
    - _Requirements: 6.2, 6.3, 6.4, 6.5_

  - [x] 7.6 Implement `_handle_invoke_tool`
    - Look up `(server, tool)` in the index; dispatch `call(server, tool, arguments)` to that
      session only (never inferred from a bare name); passthrough byte-equal result when
      `compress_results` off or result `< compress_min_tokens`; otherwise store the original in
      the `CompressionStore` and return compressed content plus a non-empty `hash`
    - Return a structured `{error, server, tool}` (no exception, other sessions unaffected) for
      an unknown `(server, tool)`, an unavailable server, a downstream error, or a
      `call_timeout_s` timeout (correctly classified); reuse existing `CompressionStore`
      redaction before logging/storing and exclude any result that cannot be redacted
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5, 7.6, 7.7, 3.5, 12.7, 12.8_

  - [x]* 7.7 Write property test for invocation routing
    - **Property P2: Routing**
    - **Validates: Requirements 7.1, 7.2**
    - Use `hypothesis` with fake sessions (including duplicate tool names across servers);
      assert `invoke_tool(server, tool, args)` reaches the intended session and no other

  - [x]* 7.8 Write property test for result fidelity
    - **Property P4: Result fidelity**
    - **Validates: Requirements 7.3, 7.4, 7.5**
    - Use `hypothesis` to assert passthrough is byte-equal (P4a) and that compressed results
      carry a `hash` whose `retrieve(hash)` returns the exact original (P4b)

  - [x]* 7.9 Write unit test for secret redaction on invoke results
    - **Property P12: Secret redaction**
    - **Validates: Requirement 12.7**
    - Assert logged/stored downstream results contain no raw secret/auth/API-key values
      (reusing the existing `CompressionStore` redaction)

  - [x] 7.10 Implement `_handle_list_servers`
    - Return the inventory of all configured downstreams with health "healthy"/"unavailable"
      (5s health check), and an empty inventory when none are configured
    - _Requirements: 8.1, 8.2, 8.3_

  - [x]* 7.11 Write unit tests for list_servers inventory and health
    - Cover healthy/unavailable statuses and the empty-inventory case
    - _Requirements: 8.1, 8.2, 8.3_

- [x] 8. Implement the gateway registrar and spec builder
  - [x] 8.1 Implement `build_gateway_spec()` and `GatewayRegistrar` in `headroom/mcp_registry/gateway.py`
    - `build_gateway_spec()` returns a `ServerSpec` named `headroom-gateway` with `command`
      from `resolve_headroom_command()` and `args` ending in the ordered tokens
      `mcp`, `gateway`, `serve`
    - Delegate config writes to `KiroRegistrar` so unmanaged keys (`disabled`, `timeout`,
      `autoApprove`) survive; report already-registered / mismatch / write-failure outcomes and
      support force-override, writing exactly one `headroom-gateway` entry
    - Re-export `build_gateway_spec` from `headroom/mcp_registry/install.py` alongside
      `build_headroom_spec`
    - _Requirements: 10.1, 10.2, 10.3, 10.4, 10.5, 10.6, 10.7_

  - [x]* 8.2 Write unit tests for registration outcomes and key preservation
    - **Property P11: Registration key preservation**
    - **Validates: Requirement 10.3**
    - Add cases for single-entry write, already/mismatch/force/write-failure outcomes (reuse
      the `KiroRegistrar` test pattern)
    - _Requirements: 10.1, 10.4, 10.5, 10.6, 10.7_

- [x] 9. Add the CLI gateway subgroup
  - [x] 9.1 Add a `gateway` subgroup to `headroom/cli/mcp.py`
    - Add `headroom mcp gateway` with `serve`, `install`, `uninstall`, `status`
    - `serve` resolves config, starts `MCPGatewayServer` over stdio, and reports unreachable
      downstreams without terminating; `install`/`uninstall` delegate to `GatewayRegistrar`
      (already-installed and write-failure handling); `status` reports the resolved inventory
      and registration state, and errors rather than returning a partial status when only one
      is determinable
    - _Requirements: 11.1, 11.2, 11.3, 11.4, 11.5, 11.6, 11.7, 11.8, 11.9_

  - [x]* 9.2 Write unit tests for the gateway CLI subcommands
    - Cover subcommand presence, serve-with-unreachable, install-already, install/uninstall
      write-failure abort, and status partial-determination error
    - _Requirements: 11.1, 11.6, 11.7, 11.8, 11.9_

- [x] 10. Integration, steering, and README wiring
  - [x]* 10.1 Write integration test asserting context reduction end-to-end
    - **Property P1: Context reduction**
    - **Validates: Requirements 1.1, 1.2, 1.3, 1.4**
    - Stand up several fake in-process MCP servers as downstreams; assert the gateway's
      `list_tools` returns only the four meta-tools regardless of downstream tool count, and
      that `find_tools → invoke_tool` returns the expected downstream result

  - [x] 10.2 Add the steering rule and README documentation
    - Add a `.kiro/steering/*` rule guiding the model to call `find_tools` at the start of a
      task to discover the tools it needs
    - Update `README.md` to document `headroom mcp gateway install|serve` and the
      disable-individual-servers workflow (and Kiro support)
    - _Requirements: 11.1, 11.2, 11.3_

- [x] 11. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 12. Restore rare exact-term relevance with hybrid tool search
  - [x] 12.1 Implement hybrid ranking and regression coverage
    - In `tests/test_mcp_gateway_search.py`, add a deterministic regression corpus that
      reproduces a distinctive exact query term being lost behind unrelated generic semantic
      matches under cosine-only ranking; cover an exact match from `tool`, `namespaced_id`,
      `server`, or `description`, and complete the implementation in this same task rather
      than leaving an exploration-only failing test
    - In `headroom/mcp_gateway/search.py`, combine independently normalized semantic and BM25
      lexical scores when embeddings are available, using one fixed generic rule with no new
      dependency, configurable weight, or query/tool/server/provider-specific hardcoding
    - Ensure at least one distinctive exact-match entry ranks ahead of every zero-lexical
      candidate, while preserving descending-score ordering, `namespaced_id` ascending
      tie-breaking, empty/no-match behavior, and deterministic BM25-only fallback when
      embeddings are unavailable or unusable
    - Extend targeted unit/property coverage for hybrid signal contribution, all searchable
      fields, repeatable ordering, stable ties, and fallback preservation
    - Run `uv run pytest tests/test_mcp_gateway_search.py`, `uv run pytest`,
      `uv run ruff check .`, `uv run ruff format --check .`, and
      `uv run maturin build --profile ci`; resolve all failures within this task
    - **Property P6: Discovery relevance / determinism**
    - **Validates: Requirements 5.1, 5.2, 5.3, 5.9**

## Notes

- Tasks marked with `*` are optional test tasks and can be skipped for a faster MVP; core
  implementation tasks are never optional.
- Each task references specific requirement sub-clauses (not just user stories) for
  traceability, and every property task names the design property (P1–P12) it validates.
- Property-based tests use `hypothesis` (already a dev dependency); unit and property tests
  are complementary.
- Every implementation task ends by building the package with
  `uv run maturin build --profile ci` and running the relevant tests with `uv run pytest`.
- The plan is additive and reuses `ServerSpec`/`MCPRegistrar`/`KiroRegistrar`,
  `HeadroomMCPServer` + `CompressionStore` + `savings_ledger`, `EmbeddingScorer`, and
  `headroom.compress` — no parallel reimplementations.
- This workflow produces only the design and planning artifacts; it does not implement the
  feature.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "8.1"] },
    { "id": 1, "tasks": ["1.2", "2.1", "3.1", "4.1", "8.2"] },
    { "id": 2, "tasks": ["2.2", "2.3", "3.2", "3.3", "4.2", "4.3", "4.4", "5.1"] },
    { "id": 3, "tasks": ["5.2", "5.3", "7.1"] },
    { "id": 4, "tasks": ["7.2"] },
    { "id": 5, "tasks": ["7.3", "7.4"] },
    { "id": 6, "tasks": ["7.5", "7.6"] },
    { "id": 7, "tasks": ["7.7", "7.8", "7.9", "7.10"] },
    { "id": 8, "tasks": ["7.11", "9.1"] },
    { "id": 9, "tasks": ["9.2", "10.1", "10.2"] },
    { "id": 10, "tasks": ["12.1"] }
  ]
}
```
