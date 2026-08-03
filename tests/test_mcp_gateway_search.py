"""Unit and property coverage for MCP Gateway hybrid tool search.

The deterministic embedding fixtures reproduce cosine-only misses without an
ONNX model or network access. Coverage exercises the fixed semantic/BM25 blend,
all searchable fields, stable ordering, and lexical-only degradation.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from headroom.mcp_gateway.index import ToolIndex
from headroom.mcp_gateway.search import ToolSearch
from headroom.relevance.embedding import EmbeddingScorer

# Lowercase-only identifiers keep namespaced ids a pure function of
# ``(server, tool)`` and avoid embedding the ``::`` separator in either part.
_names = st.text(
    alphabet=st.characters(min_codepoint=97, max_codepoint=122),
    min_size=1,
    max_size=6,
)
_WORDS = ["search", "files", "data", "create", "task", "read", "write", "list", "user"]
_word = st.sampled_from(_WORDS)


class _OfflineEmbeddingScorer(EmbeddingScorer):
    """Prevent tests for the fallback path from loading a real model."""

    def __init__(self) -> None:
        pass

    def _encode(self, _texts: list[str]):
        raise RuntimeError("embeddings intentionally unavailable")


class _QueryEmbeddingScorer(EmbeddingScorer):
    """Return one deterministic query vector; entry vectors live in the index."""

    def __init__(self, vector: Sequence[float]) -> None:
        self._vector = list(vector)

    def _encode(self, texts: list[str]):
        assert len(texts) == 1
        return [self._vector]


class _FailingQueryEmbeddingScorer(EmbeddingScorer):
    def __init__(self) -> None:
        pass

    def _encode(self, _texts: list[str]):
        raise RuntimeError("query embedding intentionally unusable")


def _tool(name: str, *, description: str = "") -> dict:
    return {"name": name, "description": description, "inputSchema": {"type": "object"}}


def _build_index(tools_by_server: Mapping[str, list[dict]]) -> ToolIndex:
    index = ToolIndex(embedding_scorer=_OfflineEmbeddingScorer())
    index.build(tools_by_server)
    return index


def _build_search(tools_by_server: Mapping[str, list[dict]]) -> ToolSearch:
    # Every entry remains embedding-free, forcing deterministic BM25 fallback.
    return ToolSearch(_build_index(tools_by_server), embedding_scorer=None)


def _build_hybrid_search(
    tools_by_server: Mapping[str, list[dict]],
    vectors_by_id: Mapping[str, Sequence[float]],
    *,
    query_vector: Sequence[float] = (1.0, 0.0),
) -> ToolSearch:
    index = _build_index(tools_by_server)
    for entry in index:
        entry.embedding = list(vectors_by_id[entry.namespaced_id])  # type: ignore[assignment]
    return ToolSearch(index, embedding_scorer=_QueryEmbeddingScorer(query_vector))


def _ids(entries) -> list[str]:
    return [entry.namespaced_id for entry in entries]


# --- Property P6: deterministic, bounded fallback --------------------------


@settings(max_examples=100, deadline=None)
@given(
    servers=st.lists(_names, min_size=0, max_size=5, unique=True),
    data=st.data(),
    query_words=st.lists(_word, min_size=0, max_size=4),
    limit=st.integers(min_value=1, max_value=10),
)
def test_p6_bm25_fallback_is_deterministic_and_bounded(
    servers: list[str],
    data: st.DataObject,
    query_words: list[str],
    limit: int,
) -> None:
    """**Validates: Requirements 5.1, 5.3**"""
    tools_by_server: dict[str, list[dict]] = {}
    for server in servers:
        n_tools = data.draw(st.integers(min_value=0, max_value=4))
        tools = []
        for i in range(n_tools):
            words = data.draw(st.lists(_word, min_size=0, max_size=5))
            tools.append(_tool(f"tool{i}", description=" ".join(words)))
        tools_by_server[server] = tools

    search = _build_search(tools_by_server)
    valid_ids = {entry.namespaced_id for entry in search._index.entries()}
    query = " ".join(query_words)

    first = search.search(query, limit)
    second = search.search(query, limit)
    first_ids = _ids(first)

    assert first_ids == _ids(second)
    assert len(first) <= limit
    assert set(first_ids) <= valid_ids
    assert len(first_ids) == len(set(first_ids))


# --- Property P6: deterministic hybrid ordering ----------------------------


@settings(max_examples=100, deadline=None)
@given(
    records=st.lists(
        st.tuples(_names, st.booleans(), st.booleans()),
        min_size=1,
        max_size=6,
        unique_by=lambda record: record[0],
    ),
    limit=st.integers(min_value=1, max_value=10),
)
def test_p6_hybrid_search_is_repeatable_sorted_and_bounded(
    records: list[tuple[str, bool, bool]],
    limit: int,
) -> None:
    """**Validates: Requirements 5.1, 5.2, 5.3, 5.9**"""
    tools_by_server: dict[str, list[dict]] = {}
    vectors_by_id: dict[str, Sequence[float]] = {}
    for position, (server, has_lexical_match, has_semantic_match) in enumerate(records):
        tool_name = f"tool{position}"
        description = "needle" if has_lexical_match else "unrelated"
        tools_by_server[server] = [_tool(tool_name, description=description)]
        vectors_by_id[f"{server}::{tool_name}"] = (1.0, 0.0) if has_semantic_match else (0.0, 1.0)

    search = _build_hybrid_search(tools_by_server, vectors_by_id)
    first = search.search("needle", limit)
    second = search.search("needle", limit)

    entries = search._index.entries()
    semantic = search._embedding_scores("needle", entries)
    assert semantic is not None
    scores = search._hybrid_scores(semantic, search._keyword_scores("needle", entries))
    expected = [
        entry.namespaced_id
        for entry, score in sorted(scores, key=lambda pair: (-pair[1], pair[0].namespaced_id))
        if score > 0.0
    ][:limit]

    assert _ids(first) == _ids(second) == expected
    assert len(first) <= limit
    assert set(_ids(first)) <= set(vectors_by_id)


# --- Regression: lexical evidence survives adverse cosine ranking ----------


def test_hybrid_regression_exact_term_beats_cosine_only_generic_matches() -> None:
    query = "zephyrcodex"
    target_id = "reference::lookup_catalog"
    tools_by_server = {
        "reference": [
            _tool(
                "lookup_catalog",
                description="Retrieve authoritative entries from the zephyrcodex catalog",
            )
        ],
        "planning-a": [_tool("schedule_work", description="Plan projects and work items")],
        "planning-b": [_tool("create_board", description="Create project planning boards")],
        "planning-c": [_tool("list_work", description="List work and project records")],
        "planning-d": [_tool("update_plan", description="Update schedules and plans")],
    }
    vectors = {
        target_id: (0.0, 1.0),
        "planning-a::schedule_work": (1.0, 0.0),
        "planning-b::create_board": (1.0, 0.0),
        "planning-c::list_work": (1.0, 0.0),
        "planning-d::update_plan": (1.0, 0.0),
    }
    search = _build_hybrid_search(tools_by_server, vectors)
    entries = search._index.entries()

    semantic = search._embedding_scores(query, entries)
    assert semantic is not None
    semantic_order = _ids(
        entry
        for entry, score in sorted(semantic, key=lambda pair: (-pair[1], pair[0].namespaced_id))
        if score > 0.0
    )
    # The deterministic corpus reproduces the old cosine-only failure: every
    # unrelated generic candidate ranks while the exact lexical target is lost.
    assert target_id not in semantic_order
    assert semantic_order == sorted(set(vectors) - {target_id})

    hybrid_order = _ids(search.search(query, len(vectors)))
    assert hybrid_order[0] == target_id
    assert all(
        hybrid_order.index(target_id) < hybrid_order.index(other) for other in semantic_order
    )


@pytest.mark.parametrize(
    ("field", "query", "target_server", "target_tool", "description"),
    [
        ("tool", "amberneedle", "catalog", "amberneedle", "Retrieve a catalog entry"),
        ("namespaced_id", "amber::needle", "amber", "needle", "Retrieve a catalog entry"),
        ("server", "amberneedle", "amberneedle", "lookup", "Retrieve a catalog entry"),
        (
            "description",
            "amberneedle",
            "catalog",
            "lookup",
            "Retrieve the distinctive amberneedle entry",
        ),
    ],
)
def test_hybrid_exact_match_from_every_searchable_field_beats_zero_lexical_candidates(
    field: str,
    query: str,
    target_server: str,
    target_tool: str,
    description: str,
) -> None:
    target_id = f"{target_server}::{target_tool}"
    tools_by_server = {
        target_server: [_tool(target_tool, description=description)],
        "generic-a": [_tool("plan_work", description="Plan general work")],
        "generic-b": [_tool("list_projects", description="List general projects")],
        "generic-c": [_tool("update_record", description="Update a general record")],
    }
    vectors = {
        target_id: (0.0, 1.0),
        "generic-a::plan_work": (1.0, 0.0),
        "generic-b::list_projects": (1.0, 0.0),
        "generic-c::update_record": (1.0, 0.0),
    }

    result = _build_hybrid_search(tools_by_server, vectors).search(query, len(vectors))

    assert _ids(result)[0] == target_id, field


def test_hybrid_semantic_signal_orders_equal_lexical_matches() -> None:
    tools = {
        "alpha": [_tool("first", description="sharedneedle operation")],
        "bravo": [_tool("other", description="sharedneedle operation")],
    }
    vectors = {"alpha::first": (0.0, 1.0), "bravo::other": (1.0, 0.0)}
    search = _build_hybrid_search(tools, vectors)

    lexical = search._keyword_scores("sharedneedle", search._index.entries())
    assert lexical[0][1] == pytest.approx(lexical[1][1])
    assert _ids(search.search("sharedneedle", 2)) == ["bravo::other", "alpha::first"]


def test_hybrid_ties_use_namespaced_id_ascending_independent_of_insertion_order() -> None:
    tools = {
        "zulu": [_tool("lookup", description="sharedneedle operation")],
        "alpha": [_tool("lookup", description="sharedneedle operation")],
    }
    vectors = {"zulu::lookup": (1.0, 0.0), "alpha::lookup": (1.0, 0.0)}

    result = _build_hybrid_search(tools, vectors).search("sharedneedle", 2)

    assert _ids(result) == ["alpha::lookup", "zulu::lookup"]


@pytest.mark.parametrize(
    "scorer",
    [_FailingQueryEmbeddingScorer(), _QueryEmbeddingScorer((0.0, 0.0))],
    ids=["query-error", "zero-norm-query"],
)
def test_unusable_embeddings_preserve_deterministic_bm25_fallback(
    scorer: EmbeddingScorer,
) -> None:
    tools = {
        "zulu": [_tool("lookup", description="needle needle")],
        "alpha": [_tool("lookup", description="needle")],
        "bravo": [_tool("other", description="unrelated")],
    }
    expected = _ids(_build_search(tools).search("needle", 3))
    index = _build_index(tools)
    for entry in index:
        entry.embedding = [1.0, 0.0]  # type: ignore[assignment]

    actual_search = ToolSearch(index, embedding_scorer=scorer)

    assert _ids(actual_search.search("needle", 3)) == expected
    assert _ids(actual_search.search("needle", 3)) == expected


def test_hybrid_query_with_no_lexical_or_semantic_match_returns_empty_list() -> None:
    tools = {"catalog": [_tool("lookup", description="retrieve records")]}
    search = _build_hybrid_search(tools, {"catalog::lookup": (0.0, 1.0)})

    assert search.search("nooverlap", 5) == []


# --- Stable BM25 ties -------------------------------------------------------


@settings(max_examples=100, deadline=None)
@given(
    servers=st.lists(_names, min_size=2, max_size=6, unique=True),
    tool_name=_names,
    data=st.data(),
)
def test_p6_bm25_ties_use_namespaced_id_ascending(
    servers: list[str],
    tool_name: str,
    data: st.DataObject,
) -> None:
    """**Validates: Requirements 5.1, 5.3**"""
    description = "searchneedle files data"
    tools_by_server = {server: [_tool(tool_name, description=description)] for server in servers}
    search = _build_search(tools_by_server)
    all_ids = sorted(f"{server}::{tool_name}" for server in servers)
    limit = data.draw(st.integers(min_value=1, max_value=len(servers)))

    assert _ids(search.search("searchneedle", limit)) == all_ids[:limit]


# --- Limit validation (Requirement 5.7) ------------------------------------


def _one_tool_search() -> ToolSearch:
    return _build_search({"srv": [_tool("t", description="search files data")]})


@pytest.mark.parametrize("bad_limit", [0, -1, -100])
def test_limit_below_one_raises_valueerror(bad_limit: int) -> None:
    with pytest.raises(ValueError):
        _one_tool_search().search("search", bad_limit)


@pytest.mark.parametrize("bad_limit", [1.5, 2.0, "3", None])
def test_non_integer_limit_raises_valueerror(bad_limit: object) -> None:
    with pytest.raises(ValueError):
        _one_tool_search().search("search", bad_limit)  # type: ignore[arg-type]


def test_bool_limit_raises_valueerror() -> None:
    with pytest.raises(ValueError):
        _one_tool_search().search("search", True)  # type: ignore[arg-type]


def test_invalid_limit_returns_no_partial_results() -> None:
    with pytest.raises(ValueError):
        _ = _one_tool_search().search("search", 0)


# --- Empty results (Requirement 5.8) ---------------------------------------


def test_empty_index_returns_empty_list() -> None:
    search = _build_search({})
    assert search._index.entries() == []
    assert search.search("search", 5) == []


def test_query_matching_nothing_returns_empty_list() -> None:
    search = _build_search({"srv": [_tool("t", description="search files data")]})
    assert search.search("zzzznomatch", 5) == []


@pytest.mark.parametrize("blank_query", ["", "   ", "\t", "\n  \t"])
def test_empty_or_whitespace_query_returns_empty_list(blank_query: str) -> None:
    search = _build_search({"srv": [_tool("t", description="search files data")]})
    assert search.search(blank_query, 5) == []
