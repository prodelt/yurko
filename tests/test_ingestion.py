"""Offline unit tests for the Postgres-backed layer (no database required)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from search.embeddings import NullEmbedder, make_embedder  # noqa: E402
from storage.ingestion import Ingestor  # noqa: E402
from registries.law_registry import LawRegistry  # noqa: E402
from storage.repository import RepositoryCacheAdapter, _parse_date  # noqa: E402
from search.search_engine import SearchEngine  # noqa: E402

BASE_DIR = Path(__file__).resolve().parent.parent


@pytest.fixture
def registry() -> LawRegistry:
    return LawRegistry(BASE_DIR / "cache" / "laws.json")


class FakeAdapter:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def fetch_law(self, law_id: str, law_info: dict) -> dict:
        return {**self.payload, "law_id": law_id}


class FakeRepo:
    """Records repository writes; raises on reads when configured."""

    def __init__(self, raise_on_read: bool = False) -> None:
        self.laws: dict = {}
        self.versions: list = []
        self.articles: dict = {}
        self.raise_on_read = raise_on_read
        self._next_id = 1

    def upsert_law(self, law_id, meta):
        self.laws[law_id] = meta

    def upsert_version(self, law_id, payload):
        vid = self._next_id
        self._next_id += 1
        self.versions.append((law_id, payload, vid))
        return vid, True

    def upsert_articles(self, version_id, law_id, articles):
        self.articles[version_id] = articles
        return list(range(len(articles)))

    def store_cache_entry(self, law_id, entry, search):
        self.upsert_law(law_id, {"title": entry.get("title")})
        vid, created = self.upsert_version(law_id, entry)
        self.upsert_articles(vid, law_id, entry.get("articles", {}))
        return vid, created

    def get_current(self, law_id):
        if self.raise_on_read:
            raise RuntimeError("db down")
        return None

    def get_status(self, law_id):
        if self.raise_on_read:
            raise RuntimeError("db down")
        return {"cached": False, "fresh": False, "char_count": 0, "source": None}


def test_ingest_law_persists_text_and_articles(registry):
    payload = {
        "title": "Тестовий закон",
        "url": "https://zakon.rada.gov.ua/laws/show/922-19",
        "text": "Стаття 1. Загальні положення.\n\nСтаття 2. Прозорість закупівель.",
        "source": "print_url",
        "adapter": "zakon_rada",
        "content_hash": "deadbeef",
    }
    repo = FakeRepo()
    ingestor = Ingestor(repo, FakeAdapter(payload), SearchEngine(), registry)

    result = ingestor.ingest_law("922-19")

    assert result.ok and result.created
    assert "922-19" in repo.laws
    assert result.articles >= 2
    assert repo.articles  # articles were written


def test_ingest_law_empty_text_fails(registry):
    repo = FakeRepo()
    ingestor = Ingestor(repo, FakeAdapter({"text": ""}), SearchEngine(), registry)
    result = ingestor.ingest_law("922-19")
    assert not result.ok
    assert result.error == "empty text"


def test_ingest_unknown_law(registry):
    repo = FakeRepo()
    ingestor = Ingestor(repo, FakeAdapter({"text": "x"}), SearchEngine(), registry)
    result = ingestor.ingest_law("definitely not a law id with spaces")
    assert not result.ok


def test_adapter_falls_through_on_db_error():
    """When the repo read raises, the adapter uses the fallback cache."""
    calls: dict = {}

    class FakeCache:
        def get_or_fetch(self, law_id, scraper, search, force_refresh=False):
            calls["fetched"] = law_id
            return {"law_id": law_id, "text": "fallback", "articles": {}, "from_cache": False}

        def get_status(self, law_id):
            calls["status"] = law_id
            return {"cached": True, "fresh": True, "char_count": 8, "source": "cache"}

        def get_cached_entry(self, law_id, allow_stale=False):
            return {"law_id": law_id, "text": "fallback"}

        def get_metrics(self):
            return {"hits": 0}

    adapter = RepositoryCacheAdapter(FakeRepo(raise_on_read=True), FakeCache())
    result = adapter.get_or_fetch("922-19", scraper=None, search=SearchEngine())

    assert result["text"] == "fallback"
    assert calls["fetched"] == "922-19"
    assert adapter.get_status("922-19")["cached"] is True


def test_adapter_serves_from_repo_when_present():
    class HitRepo(FakeRepo):
        def get_current(self, law_id):
            return {"law_id": law_id, "text": "from-pg", "articles": {"1": "a"}}

        def store_cache_entry(self, law_id, entry, search):  # pragma: no cover
            raise AssertionError("should not fetch when repo has the law")

    class FakeCache:
        def get_or_fetch(self, *a, **k):  # pragma: no cover
            raise AssertionError("should not be called")

    adapter = RepositoryCacheAdapter(HitRepo(), FakeCache())
    result = adapter.get_or_fetch("922-19", scraper=None, search=SearchEngine())
    assert result["text"] == "from-pg"
    assert result["from_cache"] is True


def test_ingest_law_splits_giant_articles(registry):
    # A parser artifact: one «стаття» swallowing a huge tail must be chunked
    # at ingest so it cannot dominate FTS ranking.
    giant_tail = "Прикінцеві положення. " * 1000  # ~22k chars
    payload = {
        "title": "Тестовий кодекс",
        "url": "https://zakon.rada.gov.ua/laws/show/435-15",
        "text": f"Стаття 1. Коротка стаття.\n\nСтаття 2. {giant_tail}",
        "source": "print_url",
        "adapter": "zakon_rada",
        "content_hash": "deadbeef",
    }
    repo = FakeRepo()
    ingestor = Ingestor(repo, FakeAdapter(payload), SearchEngine(), registry)

    result = ingestor.ingest_law("435-15")

    assert result.ok
    stored = next(iter(repo.articles.values()))
    assert "1" in stored and "2" in stored
    assert "2#2" in stored  # giant article was split, first chunk kept the num
    assert all(len(body) <= 12000 for body in stored.values())


def test_backfill_split_articles_replaces_only_giants(registry):
    class GiantRepo(FakeRepo):
        def __init__(self):
            super().__init__()
            self.replaced: list = []

        def oversized_articles(self, min_chars=12000):
            return [(7, 1, "435-15", "1308", "слово " * 4000, 42)]

        def replace_article_with_chunks(self, article_id, version_id, law_id, ordinal, chunks):
            self.replaced.append((article_id, law_id, list(chunks.keys())))
            return list(range(len(chunks)))

    repo = GiantRepo()
    ingestor = Ingestor(repo, FakeAdapter({"text": "x"}), SearchEngine(), registry)

    replaced = ingestor.backfill_split_articles()

    assert replaced == 1
    article_id, law_id, keys = repo.replaced[0]
    assert (article_id, law_id) == (7, "435-15")
    assert keys[0] == "1308"
    assert keys[1] == "1308#2"


def test_split_oversized_keeps_normal_articles_untouched(registry):
    ingestor = Ingestor(FakeRepo(), FakeAdapter({"text": "x"}), SearchEngine(), registry)
    articles = {"625": "Боржник, який прострочив виконання грошового зобов'язання..."}
    assert ingestor._split_oversized(articles) == articles


def test_make_embedder_defaults_to_null(monkeypatch):
    for var in ("EMBEDDING_PROVIDER", "EMBEDDING_MODEL", "GOOGLE_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    embedder = make_embedder()
    assert isinstance(embedder, NullEmbedder)
    assert embedder.dim == 0
    assert embedder.embed(["a", "b"]) == [[], []]


def test_make_embedder_gemini_requires_key(monkeypatch):
    monkeypatch.setenv("EMBEDDING_PROVIDER", "gemini")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    with pytest.raises(ValueError):
        make_embedder()


def test_parse_date():
    assert _parse_date("2024-01-15") is not None
    assert _parse_date("2024-01-15T10:00:00+00:00").isoformat() == "2024-01-15"
    assert _parse_date("") is None
    assert _parse_date(None) is None
    assert _parse_date("not-a-date") is None


def test_cache_manager_carries_the_routing_seam_fields(tmp_path, registry):
    """doc_kind / real_title must survive the fetch → entry rebuild → gate path."""
    from registries.cache_manager import CacheManager

    class SeamScraper:
        def fetch_law(self, law_id, law_info):
            return {
                "law_id": law_id,
                "title": "Zakon Rada document 922-19",
                "url": "https://zakon.rada.gov.ua/laws/show/922-19",
                "text": "Стаття 1. Загальні положення.\n\nСтаття 2. Прозорість.",
                "source": "print_url",
                "doc_kind": "law",
                "real_title": "Про публічні закупівлі",
            }

    cache = CacheManager(tmp_path, registry)
    result = cache.get_or_fetch("922-19", SeamScraper(), SearchEngine())

    assert result["doc_kind"] == "law"
    assert result["real_title"] == "Про публічні закупівлі"
