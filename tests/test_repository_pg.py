"""Integration tests for PostgresRepository.

Skipped unless DATABASE_URL points at a Postgres+pgvector instance
(docker compose up -d postgres). These exercise schema, dedup, status
transitions and Ukrainian full-text search against a real database.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="DATABASE_URL not set; integration test skipped"
)

if DATABASE_URL:
    from storage.ingestion import Ingestor  # noqa: E402
    from registries.law_registry import LawRegistry  # noqa: E402
    from storage.repository import PostgresRepository, RepositoryCacheAdapter  # noqa: E402
    from search.search_engine import SearchEngine  # noqa: E402

BASE_DIR = Path(__file__).resolve().parent.parent


@pytest.fixture
def repo():
    repository = PostgresRepository(DATABASE_URL)
    repository.ensure_schema()
    # clean slate
    with repository._pool.connection() as conn:  # noqa: SLF001
        conn.execute("TRUNCATE laws, law_versions, articles, article_embeddings CASCADE")
    yield repository
    repository.close()


def _payload(text: str, chash: str, amendment: str | None = None) -> dict:
    return {
        "title": "Тестовий закон про публічні закупівлі",
        "url": "https://zakon.rada.gov.ua/laws/show/922-19",
        "text": text,
        "source": "print_url",
        "source_policy": "html_print",
        "adapter": "zakon_rada",
        "content_hash": chash,
        "amendment_date": amendment,
    }


def test_ensure_schema_and_ukrainian_config(repo):
    with repo._pool.connection() as conn:  # noqa: SLF001
        row = conn.execute("SELECT 1 FROM pg_ts_config WHERE cfgname = 'ukrainian'").fetchone()
    assert row is not None


def test_upsert_and_get_current(repo):
    repo.upsert_law("922-19", {"title": "Закон", "url": "u", "cache_ttl_days": 30})
    vid, created = repo.upsert_version(
        "922-19", _payload("Стаття 1. Текст.", "hash1", "2024-01-01")
    )
    assert created
    repo.upsert_articles(vid, "922-19", {"1": "Стаття 1. Текст."})

    entry = repo.get_current("922-19")
    assert entry is not None
    assert entry["text"].startswith("Стаття 1")
    assert entry["articles"]["1"].startswith("Стаття 1")
    assert entry["amendment_date"] == "2024-01-01"

    status = repo.get_status("922-19")
    assert status["cached"] is True


def test_dedup_same_content(repo):
    repo.upsert_law("922-19", {"title": "Закон"})
    vid1, created1 = repo.upsert_version("922-19", _payload("body", "samehash"))
    vid2, created2 = repo.upsert_version("922-19", _payload("body", "samehash"))
    assert created1 and not created2
    assert vid1 == vid2


def test_status_transition_on_new_redaction(repo):
    repo.upsert_law("922-19", {"title": "Закон"})
    repo.upsert_version("922-19", _payload("old", "h1", "2023-01-01"))
    repo.upsert_version("922-19", _payload("new", "h2", "2024-06-01"))

    with repo._pool.connection() as conn:  # noqa: SLF001
        current = conn.execute(
            "SELECT count(*) FROM law_versions WHERE law_id='922-19' AND status='current'"
        ).fetchone()
        superseded = conn.execute(
            "SELECT content_hash FROM law_versions " "WHERE law_id='922-19' AND status='superseded'"
        ).fetchall()
    assert current[0] == 1
    assert [r[0] for r in superseded] == ["h1"]
    assert repo.get_current("922-19")["text"] == "new"


def test_fts_search_ukrainian(repo):
    repo.upsert_law("922-19", {"title": "Закон про закупівлі", "url": "u"})
    vid, _ = repo.upsert_version("922-19", _payload("body", "h1"))
    repo.upsert_articles(
        vid,
        "922-19",
        {
            "1": "Стаття 1. Публічні закупівлі здійснюються прозоро.",
            "2": "Стаття 2. Оскарження процедур закупівлі.",
        },
    )
    hits = repo.fts_search("прозоро закупівлі", law_id=None, limit=5)
    assert hits
    assert hits[0]["article_num"] in {"1", "2"}


def test_ingestor_and_adapter_over_pg(repo):
    registry = LawRegistry(BASE_DIR / "cache" / "laws.json")
    search = SearchEngine()

    class FakeAdapter:
        def fetch_law(self, law_id, law_info):
            return {
                "law_id": law_id,
                "title": "Цивільний кодекс",
                "url": "https://zakon.rada.gov.ua/laws/show/435-15",
                "text": "Стаття 11. Підстави.\n\nСтаття 625. Прострочення боржника.",
                "source": "print_url",
                "adapter": "zakon_rada",
                "content_hash": "ckhash",
            }

    summary = Ingestor(repo, FakeAdapter(), search, registry).ingest_all(["435-15"])
    assert summary.created == 1

    adapter = RepositoryCacheAdapter(repo, fallback_cache=None)
    entry = adapter.get_or_fetch("435-15", scraper=None, search=search)
    assert entry["from_cache"] is True
    assert "625" in entry["articles"]


class _Fake768:
    """Deterministic embedder matching the vector(768) column for integration."""

    model = "fake-768"
    dim = 768

    def embed(self, texts):
        return [[0.01] * 768 for _ in texts]


def test_backfill_articles_and_embeddings_over_pg(repo):
    registry = LawRegistry(BASE_DIR / "cache" / "laws.json")
    search = SearchEngine()

    # КМУ-style text with no «статті» → parse_articles() finds nothing, so the
    # version is stored with zero articles (the 0-article-law scenario).
    repo.upsert_law("1178-2022-п", {"title": "Особливості закупівель", "url": "u"})
    text = "\n\n".join(f"{i}) пункт особливостей " + "деталі " * 250 for i in range(1, 8))
    vid, created = repo.upsert_version("1178-2022-п", _payload(text, "kmuhash"))
    assert created
    assert any(v_id == vid for v_id, _, _ in repo.versions_missing_articles())

    ingestor = Ingestor(repo, None, search, registry, embedder=_Fake768())

    created_frags = ingestor.backfill_articles()
    assert created_frags >= 3
    assert not any(v_id == vid for v_id, _, _ in repo.versions_missing_articles())

    assert len(repo.articles_missing_embeddings(limit=1000)) == created_frags
    written = ingestor.backfill_embeddings(batch=2)
    assert written == created_frags
    assert repo.articles_missing_embeddings(limit=1000) == []

    # The chunked law is now reachable via Ukrainian FTS.
    hits = repo.fts_search("особливостей", law_id="1178-2022-п", limit=3)
    assert hits

    stats = repo.stats()
    assert stats["embeddings"] == created_frags
    assert stats["pending_embeddings"] == 0
