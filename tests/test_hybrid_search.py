"""Offline unit tests for HybridSearch RRF fusion (no database required)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from search.embeddings import NullEmbedder  # noqa: E402
from search.hybrid_search import HybridSearch  # noqa: E402


class FakeRepo:
    def __init__(self, fts, vec, fts_any=None):
        self._fts = fts
        self._vec = vec
        self._fts_any = fts_any if fts_any is not None else fts
        self.vector_called = False
        self.match_any_called = False

    def fts_search(self, query, law_id, limit, match_any=False):
        if match_any:
            self.match_any_called = True
            return self._fts_any[:limit]
        return self._fts[:limit]

    def vector_search(self, qvec, law_id, limit):
        self.vector_called = True
        return self._vec[:limit]


class FakeEmbedder:
    model = "fake"
    dim = 8

    def embed(self, texts):
        return [[0.1] * self.dim for _ in texts]


def _hit(law_id, num):
    return {
        "law_id": law_id,
        "article_num": num,
        "snippet": f"body {num}",
        "title": "t",
        "url": "u",
    }


def test_fts_only_when_null_embedder():
    repo = FakeRepo(fts=[_hit("435-15", "625"), _hit("435-15", "549")], vec=[])
    hs = HybridSearch(repo, NullEmbedder())
    results = hs.search_articles("прострочення", law_id="435-15", max_results=2)
    assert [r["article_num"] for r in results] == ["625", "549"]
    assert repo.vector_called is False
    assert results[0]["matched_by"] == ["fts"]


def test_rrf_boosts_items_in_both_channels():
    # 549 appears in both channels -> should win after fusion.
    repo = FakeRepo(
        fts=[_hit("435-15", "625"), _hit("435-15", "549")],
        vec=[_hit("435-15", "549"), _hit("435-15", "611")],
    )
    hs = HybridSearch(repo, FakeEmbedder())
    results = hs.search_articles("оплата боргу", law_id="435-15", max_results=3)
    assert repo.vector_called is True
    assert results[0]["article_num"] == "549"
    assert set(results[0]["matched_by"]) == {"fts", "vector"}
    assert results[0]["rrf_score"] > results[1]["rrf_score"]


def test_vector_only_results_surface():
    repo = FakeRepo(fts=[], vec=[_hit("435-15", "625")], fts_any=[])
    hs = HybridSearch(repo, FakeEmbedder())
    results = hs.search_articles("знецінення грошей", law_id="435-15", max_results=5)
    assert [r["article_num"] for r in results] == ["625"]
    assert results[0]["matched_by"] == ["vector"]


def test_fts_falls_back_to_or_semantics_when_and_pass_is_empty():
    # Real case: «інфляційні втрати ... прострочення» — ст.625 lacks «втрати»,
    # so the AND pass returns nothing; the OR fallback must recover it.
    repo = FakeRepo(fts=[], vec=[], fts_any=[_hit("435-15", "625")])
    hs = HybridSearch(repo, NullEmbedder())
    results = hs.search_articles(
        "інфляційні втрати та три проценти річних за прострочення",
        law_id="435-15",
        max_results=5,
    )
    assert repo.match_any_called is True
    assert [r["article_num"] for r in results] == ["625"]
    assert results[0]["matched_by"] == ["fts"]


def test_no_fallback_call_when_and_pass_has_hits():
    repo = FakeRepo(fts=[_hit("435-15", "625")], vec=[], fts_any=[_hit("435-15", "999")])
    hs = HybridSearch(repo, NullEmbedder())
    results = hs.search_articles("прострочення", law_id="435-15", max_results=5)
    assert repo.match_any_called is False
    assert [r["article_num"] for r in results] == ["625"]
