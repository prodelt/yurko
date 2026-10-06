"""The persistence gate: what may and may not enter the working set.

Ticket 15, cause 2. Specimens are REAL scrapes this project downloaded from
zakon.rada.gov.ua, never hand-written: an invented fixture once carried invented
titles, the test passed, and it guarded the forgery.

They live in ``tests/fixtures/specimens/`` and are committed. They used to be read
straight out of ``cache/texts/``, which is gitignored — so the suite passed locally
and failed in CI on files that were never in the repo. Each file records its own
provenance in ``_fixture_provenance``: the original size and article count, and how
much of the tail was dropped to keep the repo small. Only truncation, no edits.

  2849-20   valid text, real articles, title never extracted -> defect 2a
  3631-20   2 422 chars, 0 articles, placeholder title       -> defect 2b
  922-19    healthy Закон, real title, real articles         -> must be stored
  z0133-13  наказ МОЗ, real title                            -> bylaw when routed
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from storage.ingestion import Ingestor  # noqa: E402
from registries.law_registry import LawRegistry, is_placeholder_title  # noqa: E402
from storage.repository import (  # noqa: E402
    PostgresRepository,
    persistence_verdict,
    resolve_document_title,
)
from search.search_engine import SearchEngine  # noqa: E402

BASE_DIR = Path(__file__).resolve().parent.parent
SPECIMENS_DIR = Path(__file__).resolve().parent / "fixtures" / "specimens"


def specimen(law_id: str) -> dict[str, Any]:
    """A real scrape, truncated but not edited, loaded read-only."""
    raw = (SPECIMENS_DIR / f"{law_id}.json").read_text(encoding="utf-8")
    data: dict[str, Any] = json.loads(raw)
    return data


@pytest.fixture(scope="module")
def search() -> SearchEngine:
    return SearchEngine()


@pytest.fixture(scope="module")
def registry() -> LawRegistry:
    return LawRegistry(BASE_DIR / "cache" / "laws.json")


def articles_of(entry: dict[str, Any], search: SearchEngine) -> dict[str, str]:
    return entry.get("articles") or search.parse_articles(str(entry.get("text") or ""))


# -- the specimens have the shapes this file relies on ------------------------


def test_specimens_have_the_shapes_this_file_relies_on(search):
    stub = specimen("3631-20")
    assert is_placeholder_title(stub["title"], stub["law_id"])
    assert articles_of(stub, search) == {}
    assert len(stub["text"]) > 1000  # non-empty text — that is what makes it dangerous

    untitled = specimen("2849-20")
    assert is_placeholder_title(untitled["title"], untitled["law_id"])
    assert len(articles_of(untitled, search)) >= 20

    healthy = specimen("922-19")
    assert not is_placeholder_title(healthy["title"], healthy["law_id"])
    assert len(articles_of(healthy, search)) >= 12


# -- rule: no real title, no storage (defect 2a) -----------------------------


def test_placeholder_title_is_refused_despite_valid_text(search):
    entry = specimen("2849-20")
    store, reason, title = persistence_verdict(entry, articles_of(entry, search))
    assert store is False
    assert "title" in reason
    assert title == ""


def test_real_title_from_routing_unblocks_the_same_document(search):
    entry = {**specimen("2849-20"), "real_title": "Про запобігання корупції"}
    store, reason, title = persistence_verdict(entry, articles_of(entry, search))
    assert store is True and reason == ""
    assert title == "Про запобігання корупції"


def test_empty_real_title_blocks_even_with_articles(search):
    """A "" from routing means stub — the registry title must not paper over it."""
    entry = {**specimen("922-19"), "real_title": ""}
    store, reason, _ = persistence_verdict(entry, articles_of(entry, search))
    assert store is False and "title" in reason


def test_law_id_as_title_counts_as_placeholder():
    assert resolve_document_title({"law_id": "922-19", "title": "922-19"}) == ""


# -- rule: zero articles from non-empty text is a truncated fetch (defect 2b) -


def test_stub_with_zero_articles_is_refused(search):
    entry = specimen("3631-20")
    store, reason, _ = persistence_verdict(entry, articles_of(entry, search))
    assert store is False


def test_truncated_real_law_is_refused(search):
    """The 2456-17 shape, reproduced from a real law: real title, text cut short."""
    healthy = specimen("922-19")
    truncated = {**healthy, "text": healthy["text"][:1407], "articles": {}}
    assert articles_of(truncated, search) == {}
    store, reason, title = persistence_verdict(truncated, articles_of(truncated, search))
    assert store is False
    assert "zero articles" in reason
    assert title == healthy["title"]  # the title was fine; the text was not


def test_empty_text_is_refused():
    store, reason, _ = persistence_verdict({"law_id": "922-19", "title": "x", "text": ""}, {})
    assert store is False and reason == "empty text"


# -- rule: only Закон and Кодекс are stored; підзаконні акти are read live ----


def test_bylaw_is_never_stored(search):
    entry = {**specimen("z0133-13"), "doc_kind": "bylaw"}
    store, reason, _ = persistence_verdict(entry, articles_of(entry, search))
    assert store is False
    assert "bylaw" in reason


@pytest.mark.parametrize("doc_kind", ["law", "code", "constitution", "unknown", "", None])
def test_non_bylaw_kinds_are_stored(doc_kind, search):
    entry = {**specimen("922-19"), "doc_kind": doc_kind}
    store, reason, _ = persistence_verdict(entry, articles_of(entry, search))
    assert store is True, reason


def test_absent_doc_kind_does_not_block(search):
    """Routing has not landed yet; a missing field must not stop today's prod."""
    entry = specimen("922-19")
    assert "doc_kind" not in entry
    store, _, _ = persistence_verdict(entry, articles_of(entry, search))
    assert store is True


# -- the gate is wired into both write paths ---------------------------------


class SpyRepo(PostgresRepository):
    """PostgresRepository with the pool replaced by recording — no DB, no network."""

    def __init__(self) -> None:  # noqa: D107 - deliberately skips the pool
        self.writes: list[tuple[str, Any]] = []

    def upsert_law(self, law_id: str, meta: dict[str, Any]) -> None:
        self.writes.append(("law", (law_id, meta)))

    def upsert_version(self, law_id: str, payload: dict[str, Any]) -> tuple[int, bool]:
        self.writes.append(("version", law_id))
        return 7, True

    def upsert_articles(self, version_id: int, law_id: str, articles: dict[str, str]) -> list[int]:
        self.writes.append(("articles", len(articles)))
        return list(range(len(articles)))


def test_store_cache_entry_writes_nothing_when_refused(search):
    repo = SpyRepo()
    version_id, created = repo.store_cache_entry("3631-20", specimen("3631-20"), search)
    assert (version_id, created) == (0, False)
    assert repo.writes == [], "a refused document must leave no row behind"


def test_store_cache_entry_persists_the_real_title(search):
    repo = SpyRepo()
    entry = {**specimen("2849-20"), "real_title": "Про запобігання корупції"}
    version_id, created = repo.store_cache_entry("2849-20", entry, search)
    assert created and version_id == 7
    kind, payload = repo.writes[0]
    law_id, meta = payload
    assert kind == "law" and law_id == "2849-20"
    assert meta["title"] == "Про запобігання корупції"
    assert "Zakon Rada document" not in meta["title"]


class RecordingRepo:
    """Minimal repository surface for the Ingestor."""

    def __init__(self) -> None:
        self.writes: list[str] = []

    def upsert_law(self, law_id: str, meta: dict[str, Any]) -> None:
        self.writes.append("law")

    def upsert_version(self, law_id: str, payload: dict[str, Any]) -> tuple[int, bool]:
        self.writes.append("version")
        return 1, True

    def upsert_articles(self, version_id: int, law_id: str, articles: dict[str, str]) -> list[int]:
        self.writes.append("articles")
        return list(range(len(articles)))


class FrozenAdapter:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def fetch_law(self, law_id: str, law_info: dict[str, Any]) -> dict[str, Any]:
        return {**self.payload, "law_id": law_id}


def test_ingest_law_refuses_a_stub_before_the_first_write(registry, search):
    repo = RecordingRepo()
    ingestor = Ingestor(repo, FrozenAdapter(specimen("3631-20")), search, registry)

    result = ingestor.ingest_law("3631-20")

    assert result.ok is False
    assert result.error is not None and result.error.startswith("not persisted")
    assert repo.writes == []


def test_ingest_law_refuses_a_bylaw_met_on_the_fly(registry, search):
    """A bylaw the registry never vouched for resolves through `_dynamic_law`."""
    payload = {**specimen("z0133-13"), "doc_kind": "bylaw"}
    repo = RecordingRepo()
    ingestor = Ingestor(repo, FrozenAdapter(payload), search, registry)

    result = ingestor.ingest_law("1031-2026-п")

    assert result.ok is False
    assert repo.writes == []


def test_ingest_law_stores_a_bylaw_the_registry_curates(registry, search):
    """z0133-13 is a МОЗ order, is in cache/laws.json on purpose, and must stay."""
    payload = {**specimen("z0133-13"), "doc_kind": "bylaw"}
    repo = RecordingRepo()
    ingestor = Ingestor(repo, FrozenAdapter(payload), search, registry)

    result = ingestor.ingest_law("z0133-13")

    assert result.ok is True, result.error
    assert "law" in repo.writes


# --- the curated registry outranks the "no bylaws" rule ----------------------


def test_a_curated_bylaw_is_stored() -> None:
    """1178-2022-п is a bylaw, is in cache/laws.json on purpose, and must stay.

    The rule "bylaws are read live, never stored" aims at the 285 960 acts in
    Rada's catalogue nobody vouched for. Eleven of the registry's twenty-seven
    entries are bylaws and they are the product's subject matter — procurement
    specifics, pharmacovigilance orders, prescription rules.
    """
    entry = {
        "law_id": "1178-2022-п",
        "text": "Про затвердження особливостей здійснення публічних закупівель...",
        "doc_kind": "bylaw",
        "real_title": "Про затвердження особливостей здійснення публічних закупівель",
        "registry_pinned": True,
    }
    store, reason, title = persistence_verdict(entry, {"1": "текст пункту"})

    assert store is True, reason
    assert title == "Про затвердження особливостей здійснення публічних закупівель"


def test_a_bylaw_met_on_the_fly_is_still_refused() -> None:
    entry = {
        "law_id": "1031-2026-п",
        "text": "Про затвердження Порядку надання державної підтримки...",
        "doc_kind": "bylaw",
        "real_title": "Про затвердження Порядку надання державної підтримки",
        "registry_pinned": False,
    }
    store, reason, _ = persistence_verdict(entry, {"1": "текст пункту"})

    assert store is False
    assert "bylaw" in reason


def test_pinning_does_not_excuse_a_truncated_fetch() -> None:
    """Being curated buys a bylaw past the kind rule, not past the stub rules."""
    entry = {
        "law_id": "1178-2022-п",
        "text": "Про затвердження особливостей здійснення публічних закупівель...",
        "doc_kind": "bylaw",
        "real_title": "Про затвердження особливостей здійснення публічних закупівель",
        "registry_pinned": True,
    }
    store, reason, _ = persistence_verdict(entry, {})

    assert store is False
    assert "zero articles" in reason
