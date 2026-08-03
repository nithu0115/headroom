"""Out-of-context catalog entries for the MCP Gateway.

The gateway aggregates every downstream MCP server's tools into a
:class:`ToolIndex` so their full schemas live *outside* the model context.
This module defines only the per-tool data model, :class:`ToolIndexEntry`.
The build logic (:meth:`ToolIndex.build`) and ranked lookup
(``ToolSearch``) are added in later tasks.

``numpy`` is an optional dependency (installed via ``headroom[relevance]``),
so it is only imported under :data:`typing.TYPE_CHECKING`. Because this module
uses ``from __future__ import annotations`` every annotation is a string at
runtime, so referencing ``np.ndarray`` never forces numpy to be importable.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from headroom.relevance.bm25 import BM25Scorer
from headroom.relevance.embedding import EmbeddingScorer

if TYPE_CHECKING:
    import numpy as np

logger = logging.getLogger(__name__)

# Shared tokenizer so the keyword tokens precomputed here match exactly what
# the BM25 keyword-search fallback (``ToolSearch``, later task) tokenizes at
# query time. Reused rather than reimplemented.
_TOKENIZER = BM25Scorer()


@dataclass
class ToolIndexEntry:
    """One downstream tool, namespaced and ready for ranked lookup.

    Attributes:
        server: Downstream server name (e.g. ``"tasks"``).
        tool: Downstream tool name (e.g. ``"create_task"``).
        namespaced_id: Unambiguous id, e.g. ``"tasks::create_task"``.
        description: Downstream tool description (verbatim).
        input_schema: Downstream ``inputSchema`` (verbatim, unchanged) so
            downstream argument validation still applies.
        embedding: Precomputed embedding vector, or ``None`` when embeddings
            are unavailable (offline / no ONNX).
        keyword_tokens: Normalized tokens from server, tool, namespaced id,
            and description, used by deterministic BM25 lexical ranking.
    """

    server: str
    tool: str
    namespaced_id: str
    description: str
    input_schema: dict[str, Any]
    embedding: np.ndarray | None
    keyword_tokens: tuple[str, ...]


def _extract_tool_fields(tool: Any) -> tuple[str | None, str, Any]:
    """Normalize a downstream tool into ``(name, description, input_schema)``.

    Tolerates either an ``mcp.types.Tool`` object (``.name`` / ``.description``
    / ``.inputSchema``) or a plain dict (``name`` / ``description`` /
    ``inputSchema``, also accepting the snake_case ``input_schema``). No
    dependency on the downstream client manager is introduced — tools are
    duck-typed so this module never imports ``downstream.py``.

    Returns:
        A ``(name, description, input_schema)`` triple. ``name`` may be ``None``
        (invalid); ``description`` is coerced to ``""`` when absent; the raw
        ``input_schema`` is returned unchanged for the caller to validate.
    """
    if isinstance(tool, Mapping):
        name = tool.get("name")
        description = tool.get("description")
        schema = tool.get("inputSchema", tool.get("input_schema"))
    else:
        name = getattr(tool, "name", None)
        description = getattr(tool, "description", None)
        schema = getattr(tool, "inputSchema", None)
        if schema is None:
            schema = getattr(tool, "input_schema", None)

    if not isinstance(name, str) or not name:
        name = None
    if not isinstance(description, str):
        description = ""
    return name, description, schema


def _coerce_input_schema(schema: Any) -> dict[str, Any] | None:
    """Return the schema as a dict *unchanged*, or ``None`` if unusable.

    Property P3 (schema fidelity) requires byte-equal storage, so a schema that
    is already a mapping is returned as-is (never copied/reformatted). A schema
    advertised as a JSON string is parsed once (the only way to make it usable);
    anything else — ``None``, a list, a scalar, or an unparseable string — is
    rejected so the caller can record an error and exclude the tool.
    """
    if isinstance(schema, dict):
        return schema
    if isinstance(schema, Mapping):
        # Preserve non-dict mappings verbatim (same object, no transform).
        return schema  # type: ignore[return-value]
    if isinstance(schema, str):
        try:
            parsed = json.loads(schema)
        except (ValueError, TypeError):
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


class ToolIndex:
    """Out-of-context catalog of every downstream tool.

    :meth:`build` turns the healthy side of the downstream aggregation (a
    mapping of ``server -> [tool]``) into one :class:`ToolIndexEntry` per tool,
    namespaced as ``f"{server}::{tool}"``. Colliding ids and tools with a
    missing/unparseable ``input_schema`` are excluded and recorded in
    :attr:`errors`; every other tool is indexed.

    Embeddings are computed with the existing :class:`EmbeddingScorer` (ONNX)
    when available and left as ``None`` otherwise, so the index works fully
    offline. ``input_schema`` is stored byte-equal to what the downstream
    advertised (Property P3).

    Attributes:
        errors: Human-readable messages for every excluded tool (duplicate id
            or bad schema). Empty when nothing was excluded.
    """

    def __init__(self, embedding_scorer: EmbeddingScorer | None = None) -> None:
        """Create an empty index.

        Args:
            embedding_scorer: Optional injected scorer (mainly for tests). When
                ``None``, a scorer is created lazily during :meth:`build` iff
                the ONNX embedding stack is available.
        """
        self._entries: list[ToolIndexEntry] = []
        self._by_id: dict[str, ToolIndexEntry] = {}
        self._scorer = embedding_scorer
        self.errors: list[str] = []

    def build(self, tools_by_server: Mapping[str, list[Any]]) -> None:
        """Create one namespaced entry per tool; embed if ONNX is available.

        Preconditions: server names are non-empty.
        Postconditions: all ``namespaced_id``s are unique; ``input_schema`` is
        stored unchanged (P3); ``len(self)`` equals the total number of tools
        across all servers minus any entries excluded for a duplicate id or an
        unusable schema (Requirement 4.8).
        Loop invariant: after processing k servers the index holds exactly the
        entries for those k servers and all their ids are unique.
        """
        self._entries = []
        self._by_id = {}
        self.errors = []

        for server, tools in tools_by_server.items():
            for tool in tools or ():
                name, description, raw_schema = _extract_tool_fields(tool)
                if name is None:
                    self.errors.append(f"{server}: tool with missing/invalid name excluded")
                    continue

                namespaced_id = f"{server}::{name}"

                schema = _coerce_input_schema(raw_schema)
                if schema is None:
                    self.errors.append(
                        f"{namespaced_id}: missing or unparseable input_schema, excluded"
                    )
                    continue

                if namespaced_id in self._by_id:
                    self.errors.append(
                        f"{namespaced_id}: duplicate namespaced_id, colliding entry excluded"
                    )
                    continue

                tokens = tuple(
                    _TOKENIZER._tokenize(f"{server} {name} {namespaced_id} {description}")
                )
                entry = ToolIndexEntry(
                    server=server,
                    tool=name,
                    namespaced_id=namespaced_id,
                    description=description,
                    input_schema=schema,
                    embedding=None,
                    keyword_tokens=tokens,
                )
                self._entries.append(entry)
                self._by_id[namespaced_id] = entry

        self._compute_embeddings()

    def _compute_embeddings(self) -> None:
        """Populate ``entry.embedding`` for all entries when ONNX is available.

        Degrades gracefully: any failure (no fastembed/numpy, model download
        error, runtime error) leaves every ``embedding`` as ``None`` so the
        keyword fallback still works. Reuses :class:`EmbeddingScorer`; never
        reimplements embedding.
        """
        if not self._entries:
            return

        scorer = self._scorer
        if scorer is None:
            if not EmbeddingScorer.is_available():
                return
            scorer = EmbeddingScorer()

        texts = [f"{e.tool} {e.description}".strip() for e in self._entries]
        try:
            vectors = scorer._encode(texts)
        except Exception:  # noqa: BLE001 — embeddings are strictly optional
            logger.debug("embedding computation unavailable; degrading to keyword-only")
            return

        if len(vectors) != len(self._entries):
            logger.debug("embedding count mismatch; degrading to keyword-only")
            return

        for entry, vector in zip(self._entries, vectors):
            entry.embedding = vector

    def entries(self) -> list[ToolIndexEntry]:
        """Return all indexed entries (a shallow copy of the internal list)."""
        return list(self._entries)

    def get(self, namespaced_id: str) -> ToolIndexEntry | None:
        """Look up a single entry by its ``namespaced_id`` (``None`` if absent)."""
        return self._by_id.get(namespaced_id)

    def __contains__(self, namespaced_id: object) -> bool:
        return namespaced_id in self._by_id

    def __len__(self) -> int:
        return len(self._entries)

    def __iter__(self):
        return iter(self._entries)
