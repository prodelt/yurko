"""Hybrid search: fuse Ukrainian full-text and semantic (vector) channels.

Combines ``PostgresRepository.fts_search`` (lexical) and ``vector_search``
(semantic, via Gemini/pgvector) using Reciprocal Rank Fusion (RRF). When no
real embedder is configured the vector channel is empty and results degrade
gracefully to pure FTS — so this is safe with NullEmbedder.
"""

from __future__ import annotations

from typing import Any

RRF_K = 60


class HybridSearch:
    def __init__(self, repo: Any, embedder: Any) -> None:
        self._repo = repo
        self._embedder = embedder

    def search_articles(
        self, query: str, law_id: str | None = None, max_results: int = 5
    ) -> list[dict[str, Any]]:
        pool = max(max_results * 2, max_results)
        fts_hits = self._repo.fts_search(query, law_id, pool)
        if not fts_hits:
            # AND-semantics recall fallback: long natural-language queries with
            # one off-text word would otherwise return nothing from FTS.
            fts_hits = self._repo.fts_search(query, law_id, pool, match_any=True)

        vec_hits: list[dict[str, Any]] = []
        if getattr(self._embedder, "dim", 0) > 0:
            try:
                qvec = self._embedder.embed([query])
            except Exception:
                qvec = []
            if qvec and qvec[0]:
                vec_hits = self._repo.vector_search(qvec[0], law_id, pool)

        fused = self._rrf_fuse(fts_hits, vec_hits)
        return fused[:max_results]

    @staticmethod
    def _key(hit: dict[str, Any]) -> tuple[str, str]:
        return (str(hit.get("law_id")), str(hit.get("article_num")))

    def _rrf_fuse(
        self, fts_hits: list[dict[str, Any]], vec_hits: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        scores: dict[tuple[str, str], float] = {}
        payloads: dict[tuple[str, str], dict[str, Any]] = {}
        channels: dict[tuple[str, str], set[str]] = {}

        for channel, hits in (("fts", fts_hits), ("vector", vec_hits)):
            for rank, hit in enumerate(hits):
                key = self._key(hit)
                scores[key] = scores.get(key, 0.0) + 1.0 / (RRF_K + rank + 1)
                payloads.setdefault(key, hit)
                channels.setdefault(key, set()).add(channel)

        ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
        results: list[dict[str, Any]] = []
        for key, score in ranked:
            hit = dict(payloads[key])
            hit["rrf_score"] = round(score, 6)
            hit["matched_by"] = sorted(channels[key])
            results.append(hit)
        return results
