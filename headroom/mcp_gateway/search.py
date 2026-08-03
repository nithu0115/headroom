"""Ranked lookup over the MCP Gateway :class:`ToolIndex`.

``ToolSearch.search(query, limit)`` returns the best-matching tools for a query
so the gateway can hand the model only a *relevant* handful of tools (with their
full schemas) instead of every downstream tool.

Two ranking paths reuse Headroom's existing scorers:

* **Hybrid semantic + lexical** — when every embedding is usable, cosine and
  BM25 scores are independently normalized and combined with one fixed blend.
  The lexical share is deliberately the narrow majority so the strongest exact
  lexical match ranks ahead of every candidate with zero lexical relevance,
  even under the most adverse semantic scores.
* **BM25 keyword fallback** — used when embeddings are unavailable or unusable.
  It scores the query against each entry's precomputed ``keyword_tokens`` with
  :class:`BM25Scorer`'s corpus-IDF logic.

Both paths are deterministic functions of ``(query, index)`` and break final
score ties by ``namespaced_id`` ascending. ``numpy`` remains optional and is
only touched while validating and scoring embeddings.
"""

from __future__ import annotations

import logging
import math
from collections import Counter
from typing import TYPE_CHECKING

from headroom.relevance.bm25 import BM25Scorer
from headroom.relevance.embedding import EmbeddingScorer

if TYPE_CHECKING:
    from .index import ToolIndex, ToolIndexEntry

logger = logging.getLogger(__name__)

# One fixed, internal rule for every query and entry. Giving lexical relevance
# the narrow majority ensures a normalized strongest lexical match (1.0) is
# strictly above a zero-lexical candidate even when their semantic scores are
# respectively 0.0 and 1.0. These are intentionally not configuration knobs.
_LEXICAL_WEIGHT = 0.51
_SEMANTIC_WEIGHT = 1.0 - _LEXICAL_WEIGHT


def _normalize_nonnegative(scores: list[float]) -> list[float]:
    """Normalize finite positive scores to ``[0, 1]`` by their peak.

    Zero remains zero, which is important because it represents absence of a
    signal. Peak normalization also preserves each signal's relative score and
    avoids making equal positive scores depend on corpus insertion order.
    """
    usable = [score if math.isfinite(score) and score > 0.0 else 0.0 for score in scores]
    peak = max(usable, default=0.0)
    if peak == 0.0:
        return usable
    return [score / peak for score in usable]


class ToolSearch:
    """Rank :class:`ToolIndex` entries with hybrid search or BM25 fallback.

    The embedding path is strictly optional. Missing, malformed, non-finite,
    zero-norm, or dimension-mismatched vectors fall back to deterministic BM25
    so search remains available offline and never fails because of embeddings.
    """

    def __init__(
        self,
        index: ToolIndex,
        embedding_scorer: EmbeddingScorer | None = None,
    ) -> None:
        """Create a search over ``index``.

        Args:
            index: The built :class:`ToolIndex` to search.
            embedding_scorer: Optional injected scorer (mainly for tests). When
                ``None``, a scorer is created lazily iff the ONNX embedding
                stack is available and the entries carry embeddings.
        """
        self._index = index
        self._scorer = embedding_scorer
        # Reused for query tokenization + BM25 scoring; matches the tokenizer
        # the index used to precompute each entry's ``keyword_tokens``.
        self._bm25 = BM25Scorer()

    def search(self, query: str, limit: int) -> list[ToolIndexEntry]:
        """Return up to ``limit`` best matches in descending score order.

        Preconditions: ``limit`` is an ``int`` >= 1.
        Postconditions: returns <= ``limit`` entries drawn from the index,
        sorted by score descending with ties broken by ``namespaced_id``
        ascending; returns ``[]`` when nothing matches (including an empty
        index). Property P6 applies to both hybrid and fallback ranking.

        Raises:
            ValueError: If ``limit`` is not an ``int`` or is less than 1. No
                partial results are returned; the caller renders the error.
        """
        # ``bool`` is a subclass of ``int`` but is not a valid limit.
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise ValueError(f"limit must be an integer >= 1, got {limit!r}")
        if limit < 1:
            raise ValueError(f"limit must be >= 1, got {limit}")

        entries = self._index.entries()
        if not entries:
            return []

        # An empty/whitespace query matches nothing (Requirement 5.8).
        if not query or not query.strip():
            return []

        lexical_scores = self._keyword_scores(query, entries)
        semantic_scores = self._embedding_scores(query, entries)
        if semantic_scores is None:
            scored = lexical_scores
        else:
            scored = self._hybrid_scores(semantic_scores, lexical_scores)

        # Drop non-matches (score <= 0) so "nothing matches" yields [] (5.8),
        # then order by score desc, breaking ties by namespaced_id asc (P6).
        matches = [(entry, score) for entry, score in scored if score > 0.0]
        matches.sort(key=lambda pair: (-pair[1], pair[0].namespaced_id))
        return [entry for entry, _ in matches[:limit]]

    def _hybrid_scores(
        self,
        semantic_scores: list[tuple[ToolIndexEntry, float]],
        lexical_scores: list[tuple[ToolIndexEntry, float]],
    ) -> list[tuple[ToolIndexEntry, float]]:
        """Combine independently normalized semantic and lexical scores.

        Both input lists are produced over the same index order. The fixed
        lexical-majority blend is generic: no query, tool, server, or provider
        receives a special case.
        """
        semantic_normalized = _normalize_nonnegative([score for _, score in semantic_scores])
        lexical_normalized = _normalize_nonnegative([score for _, score in lexical_scores])

        combined: list[tuple[ToolIndexEntry, float]] = []
        for position, (entry, _score) in enumerate(semantic_scores):
            lexical_entry = lexical_scores[position][0]
            if lexical_entry.namespaced_id != entry.namespaced_id:
                # This cannot occur through ``search``; treating it as an
                # unusable semantic batch keeps the method deterministic if an
                # injected scorer violates the internal ordering contract.
                return lexical_scores
            score = (
                _SEMANTIC_WEIGHT * semantic_normalized[position]
                + _LEXICAL_WEIGHT * lexical_normalized[position]
            )
            combined.append((entry, score))
        return combined

    def _embedding_scores(
        self, query: str, entries: list[ToolIndexEntry]
    ) -> list[tuple[ToolIndexEntry, float]] | None:
        """Cosine-score ``entries`` against ``query``; ``None`` to fall back.

        Returns ``None`` when the scorer is unavailable or any query/entry
        vector is missing, malformed, non-finite, zero norm, or dimensionally
        incompatible. Embeddings are optional, so this method never raises.
        """
        if any(entry.embedding is None for entry in entries):
            return None

        scorer = self._scorer
        if scorer is None:
            if not EmbeddingScorer.is_available():
                return None
            scorer = EmbeddingScorer()

        try:
            from headroom.relevance.embedding import _cosine_similarity, _get_numpy

            vectors = scorer._encode([query])
            if len(vectors) != 1:
                return None

            np = _get_numpy()
            query_vec = np.asarray(vectors[0], dtype=float)
            if (
                query_vec.ndim != 1
                or query_vec.size == 0
                or not bool(np.all(np.isfinite(query_vec)))
                or float(np.linalg.norm(query_vec)) == 0.0
            ):
                return None

            scored: list[tuple[ToolIndexEntry, float]] = []
            for entry in entries:
                entry_vec = np.asarray(entry.embedding, dtype=float)
                if (
                    entry_vec.ndim != 1
                    or entry_vec.size != query_vec.size
                    or not bool(np.all(np.isfinite(entry_vec)))
                    or float(np.linalg.norm(entry_vec)) == 0.0
                ):
                    return None
                score = _cosine_similarity(query_vec, entry_vec)
                if not math.isfinite(score):
                    return None
                scored.append((entry, score))
            return scored
        except Exception:  # noqa: BLE001 — embeddings are strictly optional
            logger.debug("query embedding unusable; falling back to keyword search")
            return None

    def _keyword_scores(
        self, query: str, entries: list[ToolIndexEntry]
    ) -> list[tuple[ToolIndexEntry, float]]:
        """Deterministic BM25 ranking over precomputed ``keyword_tokens``.

        Reuses :class:`BM25Scorer`'s tokenizer, corpus IDF, and scoring so rare
        query terms retain more lexical evidence than corpus-wide terms.
        """
        query_tokens = self._bm25._tokenize(query)
        if not query_tokens:
            return [(entry, 0.0) for entry in entries]

        docs = [list(entry.keyword_tokens) for entry in entries]
        n_docs = len(docs)
        avg_len = sum(len(doc) for doc in docs) / max(n_docs, 1)

        doc_freq: Counter[str] = Counter()
        for doc in docs:
            doc_freq.update(set(doc))
        idf_map = {
            term: self._bm25._compute_idf(term, n_docs, doc_freq[term])
            for term in set(query_tokens)
            if term in doc_freq
        }

        scored: list[tuple[ToolIndexEntry, float]] = []
        for entry, doc in zip(entries, docs):
            raw_score, _matched = self._bm25._bm25_score(
                doc, query_tokens, avg_doc_len=avg_len, idf_map=idf_map
            )
            scored.append((entry, raw_score))
        return scored
