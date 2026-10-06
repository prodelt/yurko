"""Offline unit tests for chunking + resumable backfills (no database required)."""

from __future__ import annotations

from typing import Any

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from search.embeddings import NullEmbedder  # noqa: E402
from storage.ingestion import EMBED_MAX_CHARS, Ingestor  # noqa: E402
from registries.law_registry import LawRegistry  # noqa: E402
from search.search_engine import SearchEngine  # noqa: E402

BASE_DIR = Path(__file__).resolve().parent.parent


def test_chunk_text_splits_and_keys_fragments() -> None:
    search = SearchEngine()
    text = "\n\n".join(f"Пункт {i}. " + "слово " * 60 for i in range(10))
    chunks = search.chunk_text(text, target_chars=400)
    assert len(chunks) >= 3
    assert all(key.startswith("frag-") for key in chunks)
    assert all(chunk.strip() for chunk in chunks.values())
    # No fragment grossly exceeds the target (oversized paragraphs are hard-split).
    assert max(len(v) for v in chunks.values()) <= 400


def test_chunk_text_empty() -> None:
    assert SearchEngine().chunk_text("") == {}
    assert SearchEngine().chunk_text("   \n\n   ") == {}


class FakeBackfillRepo:
    """In-memory repo exposing exactly the surface the backfills touch."""

    def __init__(self, versions_missing=None, missing_embeddings=None) -> None:
        self._versions_missing = versions_missing or []
        # list of (article_id, body) lacking an embedding
        self._missing = list(missing_embeddings or [])
        self.upserted_articles: dict[int, dict] = {}
        self.embeddings: dict[int, list[float]] = {}
        self._next_id = 1000

    def versions_missing_articles(self):
        return list(self._versions_missing)

    def upsert_articles(self, version_id, law_id, articles):
        ids = []
        for _ in articles:
            ids.append(self._next_id)
            self._next_id += 1
        self.upserted_articles[version_id] = articles
        # Once chunked, those fragments become embeddable.
        for aid, body in zip(ids, articles.values(), strict=True):
            self._missing.append((aid, body))
        return ids

    def articles_missing_embeddings(self, limit=200, law_id=None, priority_laws=None):
        self.priority_laws_seen = priority_laws
        return self._missing[:limit]

    def upsert_embeddings(self, rows, model):
        for article_id, vector in rows:
            self.embeddings[article_id] = vector
        done = {aid for aid, _ in rows}
        self._missing = [(aid, body) for aid, body in self._missing if aid not in done]


class FakeEmbedder:
    model = "fake-embed"
    dim = 8

    def __init__(self) -> None:
        self.seen: list[str] = []

    def embed(self, texts):
        self.seen.extend(texts)
        return [[0.1] * self.dim for _ in texts]


def _registry() -> LawRegistry:
    return LawRegistry(BASE_DIR / "cache" / "laws.json")


def test_backfill_articles_chunks_zero_article_versions() -> None:
    big = "\n\n".join(f"Пункт {i}." + " текст" * 40 for i in range(20))
    repo: Any = FakeBackfillRepo(versions_missing=[(1, "1178-2022-п", big)])
    ingestor = Ingestor(repo, adapter=None, search=SearchEngine(), registry=_registry())

    created = ingestor.backfill_articles()

    assert created >= 3
    assert 1 in repo.upserted_articles


def test_backfill_embeddings_is_resumable_and_truncates() -> None:
    long_body = "я" * (EMBED_MAX_CHARS + 5000)
    repo: Any = FakeBackfillRepo(missing_embeddings=[(1, "short"), (2, long_body)])
    embedder = FakeEmbedder()
    ingestor = Ingestor(repo, adapter=None, search=SearchEngine(), registry=_registry())
    ingestor._embedder = embedder

    written = ingestor.backfill_embeddings(batch=1)

    assert written == 2
    assert set(repo.embeddings) == {1, 2}
    # Bodies are truncated before hitting the embedding API.
    assert all(len(text) <= EMBED_MAX_CHARS for text in embedder.seen)
    # Resumable: a second run has nothing left to do.
    assert ingestor.backfill_embeddings() == 0


def test_backfill_embeddings_passes_priority_laws(monkeypatch) -> None:
    monkeypatch.delenv("EMBED_PRIORITY_LAWS", raising=False)
    repo: Any = FakeBackfillRepo(missing_embeddings=[(1, "x")])
    ingestor = Ingestor(repo, adapter=None, search=SearchEngine(), registry=_registry())
    ingestor._embedder = FakeEmbedder()

    ingestor.backfill_embeddings()

    assert "435-15" in repo.priority_laws_seen
    assert "922-19" in repo.priority_laws_seen


def test_backfill_embeddings_priority_env_override(monkeypatch) -> None:
    monkeypatch.setenv("EMBED_PRIORITY_LAWS", "1178-2022-п")
    repo: Any = FakeBackfillRepo(missing_embeddings=[(1, "x")])
    ingestor = Ingestor(repo, adapter=None, search=SearchEngine(), registry=_registry())
    ingestor._embedder = FakeEmbedder()

    ingestor.backfill_embeddings()

    assert repo.priority_laws_seen == ["1178-2022-п"]


def test_backfill_embeddings_noop_with_null_embedder() -> None:
    repo: Any = FakeBackfillRepo(missing_embeddings=[(1, "x")])
    ingestor = Ingestor(repo, adapter=None, search=SearchEngine(), registry=_registry())
    ingestor._embedder = NullEmbedder()
    assert ingestor.backfill_embeddings() == 0
    assert repo.embeddings == {}
