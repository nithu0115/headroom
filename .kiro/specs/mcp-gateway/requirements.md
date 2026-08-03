# Requirements Document

## Introduction

The MCP Gateway is a single Headroom MCP server that a client (Kiro, Claude Code,
Cursor, ...) connects to *instead* of the many individual downstream MCP servers. It is
simultaneously an MCP **server** (to the client, over stdio) and an MCP **client** (to N
downstream servers, stdio and/or http). It aggregates every downstream tool **out of the
model context** and exposes only a small, fixed set of meta-tools, so a client that would
otherwise load hundreds of downstream tool schemas (~30-60k always-on tokens) instead sees
a handful of meta-tools.

These requirements are derived from the approved design at
`.kiro/specs/mcp-gateway/design.md`. They capture the meta-tool surface, downstream
discovery and proxying, tool indexing and search, configuration resolution, registration,
CLI surface, error handling, and security. They are intentionally forward-looking and
consistent with the existing registrar patterns in `headroom/mcp_registry/base.py`
(`ServerSpec`, `MCPRegistrar`) and `headroom/mcp_registry/kiro.py` (`KiroRegistrar`), and
compose those types rather than replacing them.

## Glossary

- **Gateway**: The MCP Gateway server process (`headroom mcp gateway serve`) that fronts N
  downstream MCP servers and exposes only meta-tools to the connecting client.
- **Client**: The MCP client that connects to the Gateway over stdio (e.g. Kiro).
- **Downstream_Server**: An MCP server that the Gateway connects to as a client, over stdio
  or http, in order to aggregate and proxy its tools.
- **Meta_Tool**: One of the fixed tools the Gateway exposes to the Client: `find_tools`,
  `invoke_tool`, `describe_tool`, `list_servers`.
- **Downstream_Client_Manager**: The Gateway component that owns the lifecycle of the N
  downstream client connections (connect, list, route, health, shutdown).
- **Tool_Index**: The out-of-context catalog holding one entry per downstream tool.
- **Tool_Index_Entry**: One catalog record: `server`, `tool`, `namespaced_id`,
  `description`, `input_schema`, optional `embedding`, and `keyword_tokens`.
- **Namespaced_Id**: The unambiguous identifier for a downstream tool, of the form
  `f"{server}::{tool}"` (e.g. `tasks::create_task`).
- **Tool_Search**: The Gateway component that ranks `Tool_Index` entries for a query using
  a deterministic hybrid of normalized semantic and lexical relevance when embeddings are
  available, and deterministic keyword-only (BM25) relevance when embeddings are unavailable.
- **Distinctive_Query_Term**: A normalized query token that occurs exactly in fewer than half
  of the searchable `Tool_Index_Entry` records; rarity is derived from the current Tool_Index
  across `tool`, `namespaced_id`, `server`, and `description` rather than from a term-specific
  rule.
- **Embedding_Scorer**: The existing ONNX embedding stack (`headroom/relevance/embedding.py`)
  reused for semantic ranking.
- **Compression_Store**: The existing shared compression singleton
  (`headroom/cache/compression_store.py`) used for optional result compression and retrieval.
- **Gateway_Config**: The resolved configuration (`GatewayConfigModel`) describing the
  downstreams to aggregate and the Gateway behavior toggles.
- **Downstream_Spec**: A single downstream config entry that composes the existing
  `ServerSpec` for stdio and adds `url`/`headers` for http.
- **Gateway_Registrar**: The component that registers the Gateway as a single
  `headroom-gateway` entry in the Client config, delegating the write to `KiroRegistrar`.
- **Self_Entry**: The `headroom-gateway` entry in a Client's MCP config that launches the
  Gateway itself; excluded from downstream aggregation to prevent recursion.

## Requirements

### Requirement 1: Meta-Tool Surface and Context Reduction

**User Story:** As a Client user, I want the Gateway to expose only a small fixed set of meta-tools, so that my model context is not flooded with hundreds of downstream tool schemas.

#### Acceptance Criteria

1. THE Gateway SHALL expose from its own `list_tools` response exactly the four-member Meta_Tool set `{find_tools, invoke_tool, describe_tool, list_servers}` and no other tools.
2. WHEN the Client requests `list_tools`, THE Gateway SHALL return only the four-member Meta_Tool set regardless of the number of downstream tools aggregated (from 0 to the maximum supported downstream tool count).
3. WHEN the Client requests `list_tools` while zero downstream servers are connected or zero downstream tools are aggregated, THE Gateway SHALL return exactly the four-member Meta_Tool set as a successful response.
4. THE Gateway SHALL retain every downstream tool schema in the Tool_Index and SHALL exclude every downstream tool schema from the Client-facing `list_tools` response.
5. WHERE a downstream tool schema is requested by the model, THE Gateway SHALL return that schema only in response to a `find_tools` or `describe_tool` call.
6. IF a `find_tools` or `describe_tool` call references a downstream tool that is not present in the Tool_Index, THEN THE Gateway SHALL return an error response indicating the requested tool was not found, without altering the Tool_Index.

### Requirement 2: Downstream Discovery and Aggregation

**User Story:** As a Client user, I want the Gateway to connect to all configured downstream servers and catalog their tools at startup, so that every downstream tool is discoverable through the meta-tools.

#### Acceptance Criteria

1. WHEN the Gateway starts, THE Downstream_Client_Manager SHALL attempt to connect to every downstream server in the Gateway_Config concurrently, establishing each connection within a 30-second connection timeout.
2. WHEN a downstream connection is established, THE Downstream_Client_Manager SHALL call `list_tools` on that Downstream_Server exactly once during aggregation and SHALL receive the tool list within a 30-second response timeout.
3. WHERE a Downstream_Spec declares the `stdio` transport, THE Downstream_Client_Manager SHALL spawn the Downstream_Server using the configured `command`, `args`, and `env`.
4. WHERE a Downstream_Spec declares the `http` transport, THE Downstream_Client_Manager SHALL connect to the Downstream_Server using the configured `url` and `headers`.
5. WHEN aggregation completes, THE Downstream_Client_Manager SHALL return an aggregation result partitioning every configured server into exactly one of a healthy set (with its tool list) or a failed set (with a failure reason).
6. IF a downstream connection cannot be established within the 30-second connection timeout or fails with an error, THEN THE Downstream_Client_Manager SHALL place that Downstream_Server in the failed set with a failure reason, SHALL keep that Downstream_Server in the failed set for the remainder of that aggregation pass, and SHALL continue aggregating the remaining servers.
7. IF a `list_tools` call fails or does not return within the 30-second response timeout, THEN THE Downstream_Client_Manager SHALL place that Downstream_Server in the failed set with a failure reason and SHALL continue aggregating the remaining servers.
8. WHEN a Downstream_Server has an established connection and returns an empty tool list, THE Downstream_Client_Manager SHALL place that Downstream_Server in the healthy set with an empty tool list, and THE Downstream_Client_Manager SHALL place a Downstream_Server in the healthy set only after a connection to that Downstream_Server has been established.
9. WHERE retry of failed downstreams is enabled AND one or more Downstream_Servers remain in the failed set after an aggregation pass completes, THE Gateway SHALL retry only those failed Downstream_Servers in the background using a bounded number of attempts with an exponentially increasing delay between attempts, SHALL NOT disturb the established sessions of Downstream_Servers already in the healthy set, SHALL NOT delay serving the Meta_Tool set, and SHALL rebuild the Tool_Index over all healthy Downstream_Servers and replace it atomically when a retried Downstream_Server becomes healthy.

### Requirement 3: Downstream Failure Isolation

**User Story:** As a Client user, I want a single broken downstream server to be contained, so that the Gateway and all other downstream tools keep working.

#### Acceptance Criteria

1. IF a Downstream_Server fails to spawn, fails to connect, or does not complete startup within a configurable startup timeout (default 30 seconds), THEN THE Gateway SHALL mark that server unavailable, log the failure with the server identifier and failure cause, and continue serving the Meta_Tool set.
2. WHILE a Downstream_Server is marked unavailable, THE Gateway SHALL keep every tool from every other healthy Downstream_Server discoverable and invokable.
3. WHILE a Downstream_Server is marked unavailable, THE Tool_Search SHALL omit that server's tools from `find_tools` results.
4. WHILE the Gateway is not in a terminating state, IF any single Downstream_Server errors during startup or aggregation, THEN THE Gateway SHALL continue running rather than terminating.
5. IF a client invokes a tool belonging to a Downstream_Server that is marked unavailable, THEN THE Gateway SHALL reject the invocation with an error response indicating the target server is unavailable, while keeping all other Downstream_Server tools invokable.

### Requirement 4: Tool Indexing and Namespacing

**User Story:** As a Client user, I want each downstream tool cataloged with an unambiguous identifier and an unmodified schema, so that tools never collide and the arguments I construct validate against the real downstream.

#### Acceptance Criteria

1. WHEN the Tool_Index is built, THE Tool_Index SHALL create exactly one Tool_Index_Entry per tool advertised by each healthy Downstream_Server.
2. THE Tool_Index SHALL assign each Tool_Index_Entry a Namespaced_Id of the form `f"{server}::{tool}"`, where `server` is the configured Downstream_Server identifier and `tool` is the tool name as advertised by that Downstream_Server.
3. THE Tool_Index SHALL ensure every Namespaced_Id is unique across the entire index.
4. WHERE two healthy Downstream_Servers expose tools with the same tool name, THE Tool_Index SHALL represent each as a distinct Tool_Index_Entry with a distinct Namespaced_Id.
5. IF building the Tool_Index would produce two Tool_Index_Entries with the same Namespaced_Id, THEN THE Tool_Index SHALL reject the colliding entry, exclude it from the index, and record an error indicating the duplicate Namespaced_Id, while retaining all non-colliding entries.
6. THE Tool_Index SHALL store each Tool_Index_Entry `input_schema` byte-equal to the `input_schema` advertised by the Downstream_Server, applying no transformation, reformatting, or re-serialization.
7. IF a Downstream_Server advertises a tool with a missing or unparseable `input_schema`, THEN THE Tool_Index SHALL exclude that tool from the index and record an error identifying the affected Namespaced_Id, without aborting indexing of the remaining tools.
8. WHEN the Tool_Index finishes building, THE Tool_Index SHALL contain a number of entries equal to the total number of tools across all healthy Downstream_Servers, minus any entries excluded under criteria 5 or 7.

### Requirement 5: Tool Discovery via find_tools

**User Story:** As a model driving the Client, I want to search for relevant tools by query, so that I retrieve only the handful of tools I need with their full schemas.

#### Acceptance Criteria

1. WHEN `find_tools(query, limit)` is called with `limit` an integer greater than or equal to 1, THE Tool_Search SHALL return an ordered list of at most `limit` Tool_Index_Entry results drawn from the Tool_Index, sorted in descending rank-score order.
2. WHERE the Embedding_Scorer is available, THE Tool_Search SHALL rank results using a deterministic hybrid score that combines normalized semantic similarity and normalized lexical relevance, breaking equal hybrid scores by Namespaced_Id in ascending lexicographic order.
3. WHERE the Embedding_Scorer is unavailable, THE Tool_Search SHALL rank results using a deterministic keyword-only (BM25) function of the query and the Tool_Index, breaking ties by Namespaced_Id in ascending lexicographic order.
4. WHEN `find_tools` returns a result, THE Gateway SHALL include for each entry its `namespaced_id`, `server`, `tool`, `description`, and `input_schema`.
5. THE Gateway SHALL return each `find_tools` result `input_schema` byte-equal to the schema advertised by the Downstream_Server.
6. WHEN `find_tools` is called without a `limit` argument, THE Gateway SHALL apply the configured `find_limit_default`, which SHALL be an integer greater than or equal to 1.
7. IF `find_tools` is called with `limit` less than 1 or a non-integer value, THEN THE Tool_Search SHALL reject the call and return an error response indicating the `limit` argument is invalid, without returning any Tool_Index_Entry results.
8. WHEN `find_tools` is called and no Tool_Index_Entry in the Tool_Index matches the query (including when the Tool_Index is empty), THE Tool_Search SHALL return an empty result list.
9. WHERE the Embedding_Scorer is available and a query contains a Distinctive_Query_Term present in a Tool_Index_Entry `tool`, `namespaced_id`, `server`, or `description`, THE Tool_Search SHALL rank at least one entry containing the exact Distinctive_Query_Term ahead of every entry with zero lexical relevance to the query.

### Requirement 6: Tool Description via describe_tool

**User Story:** As a model driving the Client, I want to request the full schema of one named tool, so that I can inspect a specific tool without a broad search.

#### Acceptance Criteria

1. WHEN `describe_tool(name)` is called with a Namespaced_Id present in the Tool_Index, THE Gateway SHALL return that tool's `description` and `input_schema`, where the returned `input_schema` is byte-equal to the schema advertised by the Downstream_Server.
2. WHEN `describe_tool(name)` is called with a bare tool name that matches exactly one Tool_Index_Entry, THE Gateway SHALL return that entry's `description` and `input_schema`, where the returned `input_schema` is byte-equal to the schema advertised by the Downstream_Server.
3. IF `describe_tool(name)` is called with a bare tool name that matches more than one Tool_Index_Entry, THEN THE Gateway SHALL return a disambiguation error that lists every matching Namespaced_Id, and SHALL NOT return any tool's `description` or `input_schema`.
4. IF `describe_tool(name)` is called with a name that matches no Tool_Index_Entry, THEN THE Gateway SHALL return a structured error indicating the tool was not found.
5. IF `describe_tool(name)` is called with a `name` that is empty or contains only whitespace characters, THEN THE Gateway SHALL return a structured validation error indicating that a tool name is required, and SHALL NOT perform a Tool_Index lookup.

### Requirement 7: Tool Invocation Routing and Result Fidelity

**User Story:** As a model driving the Client, I want to invoke a specific downstream tool by explicit server and tool, so that the call reaches exactly the intended downstream and I get back a faithful result.

#### Acceptance Criteria

1. WHEN `invoke_tool(server, tool, arguments)` is called for a `(server, tool)` pair present in the Tool_Index, THE Gateway SHALL dispatch `call_tool(tool, arguments)` to that server's session only and SHALL NOT dispatch the call to any other server's session.
2. THE Gateway SHALL select the target session using the explicit `server` and `tool` arguments and SHALL NOT infer the target from a bare tool name.
3. WHERE `compress_results` is disabled, OR WHERE the downstream result size is strictly less than `compress_min_tokens`, THE Gateway SHALL return content that is byte-equal to the unmodified downstream result.
4. WHERE `compress_results` is enabled AND the downstream result size is greater than or equal to `compress_min_tokens`, THE Gateway SHALL store the unmodified original result in the Compression_Store and SHALL return the compressed content together with a non-empty `hash` that identifies the stored original.
5. WHEN a result has been compressed and stored under a `hash`, THE Gateway SHALL ensure that retrieving that `hash` from the Compression_Store returns content byte-equal to the original downstream result.
6. IF `invoke_tool` is called for a `(server, tool)` pair that is not present in the Tool_Index, THEN THE Gateway SHALL return a structured error identifying the requested `server` and `tool`, SHALL NOT dispatch a call to any session, and SHALL NOT raise an exception.
7. IF a `call_tool` call has been dispatched to a downstream session and that call returns an error or does not complete within `call_timeout_s` seconds, THEN THE Gateway SHALL return a structured error that contains the `server` and `tool` and that correctly classifies the failure as either a downstream error or a timeout, SHALL leave all other sessions unaffected, and SHALL NOT raise an exception.

### Requirement 8: Downstream Inventory via list_servers

**User Story:** As a Client user, I want to see which downstream servers are present and whether they are healthy, so that I can understand what the Gateway is fronting.

#### Acceptance Criteria

1. WHEN `list_servers()` is called, THE Gateway SHALL return the inventory of all configured Downstream_Servers, where each entry includes the server's identifier and a health status of either "healthy" or "unavailable".
2. IF a Downstream_Server does not respond to a health check within 5 seconds, THEN THE Gateway SHALL report that server's health status as "unavailable" in the `list_servers` response.
3. WHEN `list_servers()` is called and no Downstream_Servers are configured, THE Gateway SHALL return an empty inventory.

### Requirement 9: Configuration Resolution and Anti-Recursion

**User Story:** As a Client user, I want the Gateway to resolve its downstream list from a predictable set of sources and never point at itself, so that configuration is deterministic and cannot recurse.

#### Acceptance Criteria

1. WHEN resolving the Gateway_Config, THE Gateway SHALL select the first available and parseable source in this priority order and use it exclusively: (1) the path named by the `HEADROOM_GATEWAY_CONFIG` environment variable, (2) a dedicated `headroom-gateway.json`, (3) the Client's existing `mcp.json`.
2. WHILE resolving downstreams from any selected source, THE Gateway SHALL exclude the Self_Entry (`headroom-gateway`) from the resolved downstream list, even if an `include` filter names it.
3. WHERE an `include` filter is set, THE Gateway SHALL resolve only those Downstream_Servers whose names appear in the filter and exist in the selected source, and SHALL ignore filter names that match no entry in the source.
4. WHERE an `exclude` filter is set, THE Gateway SHALL drop the named Downstream_Servers from the resolved list, and SHALL ignore exclude names that match no entry in the source.
5. WHERE both an `include` filter and an `exclude` filter are set, THE Gateway SHALL first apply the `include` filter and then apply the `exclude` filter to the included result.
6. IF the highest-priority available source cannot be read or cannot be parsed, THEN THE Gateway SHALL resolve an empty downstream list and continue serving the Meta_Tool set.
7. THE Gateway SHALL read stdio Downstream_Spec entries using the same `mcpServers` entry shape that `KiroRegistrar` reads.

### Requirement 10: Gateway Registration

**User Story:** As a Client user, I want to install the Gateway as a single managed entry in my MCP config, so that my other MCP settings are preserved and I connect to one server.

#### Acceptance Criteria

1. WHEN the Gateway is installed, THE Gateway_Registrar SHALL write exactly one `headroom-gateway` server entry to the Client config and SHALL NOT add any additional server entries.
2. THE Gateway_Registrar SHALL build the Self_Entry spec as a `ServerSpec` named `headroom-gateway` whose `command` field is the value returned by `resolve_headroom_command()` and whose `args` list ends with the ordered tokens `mcp`, `gateway`, `serve`.
3. THE Gateway_Registrar SHALL delegate the config write to `KiroRegistrar` such that the unmanaged Kiro keys `disabled`, `timeout`, and `autoApprove` present on any pre-existing entry retain their prior values after the write.
4. WHERE a `headroom-gateway` entry already exists whose `command` and `args` are equal to the Self_Entry spec, THE Gateway_Registrar SHALL report an already-registered outcome and SHALL leave the existing entry byte-for-byte unchanged.
5. IF a `headroom-gateway` entry already exists whose `command` or `args` differ from the Self_Entry spec and no override is requested, THEN THE Gateway_Registrar SHALL report a mismatch outcome, SHALL leave the existing entry unchanged, and SHALL return an indication identifying the differing spec.
6. WHERE a `headroom-gateway` entry already exists whose `command` or `args` differ from the Self_Entry spec and an override is requested, THE Gateway_Registrar SHALL replace the entry with the Self_Entry spec while preserving the unmanaged keys `disabled`, `timeout`, and `autoApprove`.
7. IF the delegated config write fails, THEN THE Gateway_Registrar SHALL report a write-failure outcome, SHALL leave the Client config unchanged from its pre-write state, and SHALL return an indication describing the failure.

### Requirement 11: CLI Surface

**User Story:** As a Client user, I want CLI commands to run and manage the Gateway, so that I can serve, install, uninstall, and inspect it from the terminal.

#### Acceptance Criteria

1. THE `headroom mcp gateway` command group SHALL expose the `serve`, `install`, `uninstall`, `status`, and `version` subcommands.
2. WHEN `headroom mcp gateway serve` is invoked, THE Gateway SHALL connect to its configured downstreams, build the Tool_Index, and serve the Meta_Tool set to the Client over stdio.
3. WHEN `headroom mcp gateway install` is invoked, THE Gateway SHALL register the Self_Entry in the Client config via the Gateway_Registrar and return an indication of successful installation.
4. WHEN `headroom mcp gateway uninstall` is invoked, THE Gateway SHALL remove the Self_Entry from the Client config via the Gateway_Registrar and return an indication of successful removal.
5. WHEN `headroom mcp gateway status` is invoked and both the resolved downstream inventory and the Self_Entry registration state are available, THE Gateway SHALL report the resolved downstream inventory and the Self_Entry registration state as either registered or not registered.
6. IF one or more configured downstreams are unreachable when `headroom mcp gateway serve` is invoked, whether from a hard connection failure or from being otherwise unreachable after connecting, THEN THE Gateway SHALL serve the Meta_Tool set built from the reachable downstreams and return an indication identifying each unreachable downstream, without terminating the serve session.
7. IF `headroom mcp gateway install` is invoked while the Self_Entry is already present in the Client config, THEN THE Gateway SHALL leave the existing Self_Entry unchanged and return an indication that the Gateway is already installed.
8. IF the Client config cannot be written during `install` or `uninstall`, THEN THE Gateway SHALL abort the operation, leave the existing Client config unchanged, and return an error indication describing the failure.
9. IF `headroom mcp gateway status` is invoked and only one of the resolved downstream inventory or the Self_Entry registration state can be determined, THEN THE Gateway SHALL NOT return a partial status and SHALL return an error indication that the status could not be fully determined.

### Requirement 12: Security and Trust Boundary

**User Story:** As a Client user, I want the Gateway to preserve the existing trust boundary and never leak secrets, so that fronting my servers through it introduces no new risk.

#### Acceptance Criteria

1. WHEN launching a stdio Downstream_Server, THE Gateway SHALL use that server's own configured `command`, `args`, and `env` without modification, addition, or substitution, producing a process environment identical to the Client launching that server directly.
2. WHEN connecting an http Downstream_Server, THE Gateway SHALL use that server's own configured `headers` without modification and SHALL send requests only to the endpoint(s) configured for that server.
3. IF an http Downstream_Server connection would resolve to an endpoint not configured for that server, or an authorized initial endpoint responds with a redirect to a location not configured for that server, THEN THE Gateway SHALL reject the connection or redirect without sending the configured `headers` and SHALL return an error indicating the endpoint is not permitted.
4. WHEN connecting an http Downstream_Server, THE Gateway SHALL allow DNS resolution of the configured endpoint to proceed and SHALL perform the endpoint authorization check after DNS resolution completes, evaluating the resolved address and port against the endpoints configured for that Downstream_Server.
5. THE Gateway SHALL operate as a local stdio process and SHALL confine any optional proxy interaction to the loopback address `127.0.0.1:8787`.
6. IF an optional proxy interaction would target any address other than the loopback `127.0.0.1:8787`, THEN THE Gateway SHALL refuse that interaction and SHALL preserve the loopback-only Headroom contract.
7. WHEN logging or storing a downstream result, THE Gateway SHALL apply the existing Compression_Store redaction to remove secrets, auth tokens, and API keys before the content is written.
8. IF the existing Compression_Store redaction cannot be applied to a downstream result, THEN THE Gateway SHALL exclude that result from logs and stored output.
9. THE Gateway SHALL treat downstream tool descriptions and results as data and SHALL exclude downstream `env` and `headers` values from all logs and stored output.
