---
inclusion: always
---

# MCP Gateway: discover tools before you use them

When the Headroom MCP Gateway (`headroom-gateway`) is connected, downstream MCP
tools are not loaded into your context. Instead the gateway exposes four
meta-tools: `find_tools`, `describe_tool`, `invoke_tool`, and `list_servers`.
Follow this workflow so you always work from real, up-to-date tool schemas.

## At the start of a task

1. Call `find_tools` with a natural-language query describing what you need
   (for example, "create a task" or "search documents"). It returns the
   most relevant tools with their `namespaced_id` (`server::tool`), `server`,
   `tool`, `description`, and full `input_schema`.
2. If you already know a tool by name, call `describe_tool` with its
   `namespaced_id` (or a unique bare name) to get its exact schema. Prefer the
   `namespaced_id` when the same tool name exists on more than one server.
3. Call `invoke_tool` with the explicit `server`, `tool`, and `arguments` built
   against the schema you just retrieved. Route by `server::tool`, never by a
   bare tool name.
4. Use `list_servers` when you need to see which downstreams are configured and
   whether each is healthy.

## Good practices

- Search first, then invoke. Do not guess tool names or argument shapes — pull
  the schema with `find_tools`/`describe_tool` and build arguments from it.
- A large `invoke_tool` result may come back compressed with a `hash`. Retrieve
  the full original with `headroom_retrieve` using that `hash` when you need the
  exact content.
- Keep queries specific. Re-search with different wording if the first results
  are not relevant rather than invoking a poorly matched tool.
