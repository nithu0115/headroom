"""Tests for :class:`headroom.mcp_gateway.index.ToolIndex`.

These exercise the out-of-context tool catalog build entirely with plain
``dict`` tools (``name`` / ``description`` / ``inputSchema``), so no ``mcp``
package or embedding backend is required. Embeddings are left as ``None`` in
this environment (fastembed not installed); none of these tests rely on them.

Covers:
* Property P7 (namespacing): namespaced ids are unique, one entry per
  aggregated tool, ``namespaced_id == f"{server}::{tool}"``, and index size ==
  total tools minus exclusions.
* Property P3 (schema fidelity): the stored ``input_schema`` is byte-equal to
  the advertised schema and ``build`` never mutates the caller's object.
* Unit cases (collision + unparseable schema): duplicate ``namespaced_id``s and
  tools with a missing/unparseable ``input_schema`` are excluded and recorded
  in ``errors`` while every other tool is still indexed.
"""

from __future__ import annotations

import copy
import json

from hypothesis import given, settings
from hypothesis import strategies as st

from headroom.mcp_gateway.index import ToolIndex

# --- shared strategies ------------------------------------------------------

# Lowercase-only identifiers so neither server nor tool names can contain the
# "::" separator, keeping namespaced-id uniqueness a pure function of the
# (server, tool) pair.
_names = st.text(
    alphabet=st.characters(min_codepoint=97, max_codepoint=122),
    min_size=1,
    max_size=5,
)

# A small shared tool-name pool so duplicate tool names recur across servers
# (and occasionally within a server).
_tool_pool = st.sampled_from(["read", "write", "search", "list", "call"])

# JSON-serializable, reasonably bounded values for arbitrary input schemas.
_json_scalars = (
    st.none()
    | st.booleans()
    | st.integers(min_value=-1000, max_value=1000)
    | st.floats(allow_nan=False, allow_infinity=False)
    | st.text(max_size=20)
)
_json_values = st.recursive(
    _json_scalars,
    lambda children: (
        st.lists(children, max_size=4)
        | st.dictionaries(st.text(min_size=1, max_size=8), children, max_size=4)
    ),
    max_leaves=15,
)
# Top-level schema is always a dict (the only shape ToolIndex accepts).
_json_schemas = st.dictionaries(st.text(min_size=1, max_size=8), _json_values, max_size=5)


def _tool(name: str, *, schema: object | None = None, description: str = "") -> dict:
    return {
        "name": name,
        "description": description,
        "inputSchema": schema if schema is not None else {},
    }


# --- Property P7: namespacing -----------------------------------------------
# Validates: Requirements 4.1, 4.2, 4.3, 4.4, 4.8


@settings(max_examples=100, deadline=None)
@given(
    servers=st.lists(_names, min_size=0, max_size=6, unique=True),
    data=st.data(),
)
def test_p7_namespacing(servers: list[str], data: st.DataObject) -> None:
    # Build a {server: [tool]} map whose tool names deliberately collide across
    # servers (shared pool) and occasionally within a server (list may repeat).
    tools_by_server: dict[str, list[dict]] = {}
    for server in servers:
        names = data.draw(st.lists(_tool_pool, min_size=0, max_size=5))
        tools_by_server[server] = [_tool(name) for name in names]

    index = ToolIndex(embedding_scorer=None)
    index.build(tools_by_server)

    entries = index.entries()

    # namespaced_id is exactly f"{server}::{tool}" for every entry.
    for entry in entries:
        assert entry.namespaced_id == f"{entry.server}::{entry.tool}"

    ids = [e.namespaced_id for e in entries]
    # Every namespaced_id is unique (cross-server duplicates disambiguate).
    assert len(ids) == len(set(ids))

    # Exactly one entry per distinct (server, tool) pair; the aggregated id set
    # matches the distinct pairs drawn from the input.
    distinct_pairs = {(s, t["name"]) for s, tools in tools_by_server.items() for t in tools}
    expected_ids = {f"{s}::{t}" for s, t in distinct_pairs}
    assert set(ids) == expected_ids

    # Index size == total tools minus exclusions. The only exclusions here are
    # within-server duplicate names (all schemas are valid), so:
    total_tools = sum(len(tools) for tools in tools_by_server.values())
    exclusions = total_tools - len(distinct_pairs)
    assert len(index) == total_tools - exclusions
    assert len(index) == len(distinct_pairs)
    # Each excluded duplicate is recorded exactly once.
    assert len(index.errors) == exclusions

    # __contains__ / get agree with the entry list.
    for entry in entries:
        assert entry.namespaced_id in index
        assert index.get(entry.namespaced_id) is entry


# --- Property P3: schema fidelity -------------------------------------------
# Validates: Requirements 4.6, 5.5, 6.1


@settings(max_examples=100, deadline=None)
@given(schemas=st.lists(_json_schemas, min_size=1, max_size=5))
def test_p3_schema_fidelity(schemas: list[dict]) -> None:
    # One server, one tool per generated schema (unique tool names).
    tools = [_tool(f"tool{i}", schema=schema) for i, schema in enumerate(schemas)]
    # Snapshot the advertised schemas so we can detect any mutation by build().
    advertised_before = [copy.deepcopy(schema) for schema in schemas]

    index = ToolIndex(embedding_scorer=None)
    index.build({"srv": tools})

    assert len(index) == len(schemas)

    for i, advertised in enumerate(advertised_before):
        entry = index.get(f"srv::tool{i}")
        assert entry is not None
        # Stored schema equals the advertised schema...
        assert entry.input_schema == advertised
        # ...and is byte-equal when serialized deterministically.
        assert json.dumps(entry.input_schema, sort_keys=True) == json.dumps(
            advertised, sort_keys=True
        )

    # build() did not mutate any caller-supplied schema object.
    for schema, before in zip(schemas, advertised_before):
        assert schema == before


# --- Unit: collision and unparseable-schema exclusion -----------------------
# Requirements: 4.5, 4.7


def test_duplicate_namespaced_id_excluded_and_recorded() -> None:
    # Same server + same tool name twice => second is a collision.
    tools_by_server = {
        "srv": [
            _tool("dup", description="first", schema={"type": "object"}),
            _tool("dup", description="second", schema={"type": "string"}),
            _tool("unique", description="keep", schema={"type": "number"}),
        ],
    }

    index = ToolIndex(embedding_scorer=None)
    index.build(tools_by_server)

    # Colliding entry excluded; the first (non-colliding) occurrence retained.
    assert len(index) == 2
    kept = index.get("srv::dup")
    assert kept is not None
    assert kept.description == "first"
    # Non-colliding tool still indexed.
    assert "srv::unique" in index

    # Collision recorded in errors, naming the offending id.
    assert any("srv::dup" in err and "duplicate" in err.lower() for err in index.errors)


def test_cross_server_duplicate_names_both_indexed() -> None:
    # Same tool name on different servers is NOT a collision.
    index = ToolIndex(embedding_scorer=None)
    index.build({"a": [_tool("search")], "b": [_tool("search")]})

    assert len(index) == 2
    assert "a::search" in index
    assert "b::search" in index
    assert index.errors == []


def test_missing_and_unparseable_schema_excluded_rest_indexed() -> None:
    tools_by_server = {
        "srv": [
            _tool("good", schema={"type": "object"}),
            # Missing schema (None) => excluded.
            {"name": "missing", "description": "", "inputSchema": None},
            # Unparseable JSON string => excluded.
            {"name": "badjson", "description": "", "inputSchema": "{not valid json"},
            # Non-dict schema (list) => excluded.
            {"name": "listschema", "description": "", "inputSchema": [1, 2, 3]},
            _tool("also_good", schema={"type": "string"}),
        ],
    }

    index = ToolIndex(embedding_scorer=None)
    index.build(tools_by_server)

    # Only the two well-formed tools survive.
    assert len(index) == 2
    assert "srv::good" in index
    assert "srv::also_good" in index
    assert "srv::missing" not in index
    assert "srv::badjson" not in index
    assert "srv::listschema" not in index

    # Each excluded tool recorded with a schema-related error.
    for excluded in ("srv::missing", "srv::badjson", "srv::listschema"):
        assert any(excluded in err and "schema" in err.lower() for err in index.errors)


def test_json_string_schema_is_parsed_and_kept() -> None:
    # A schema advertised as a JSON *object* string is parseable => indexed.
    index = ToolIndex(embedding_scorer=None)
    index.build({"srv": [{"name": "t", "description": "", "inputSchema": '{"type": "object"}'}]})

    assert len(index) == 1
    entry = index.get("srv::t")
    assert entry is not None
    assert entry.input_schema == {"type": "object"}
    assert index.errors == []
