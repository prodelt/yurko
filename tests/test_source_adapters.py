from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).parent.parent))

from registries.cache_manager import CacheManager
from registries.law_registry import LawRegistry
from search.search_engine import SearchEngine

REGISTRY_PATH = Path(__file__).parent.parent / "cache" / "laws.json"


@pytest.fixture()
def registry() -> LawRegistry:
    return LawRegistry(REGISTRY_PATH)


def test_source_contract_models_are_frozen() -> None:
    from core.contracts import SourceDocument, SourcePolicy

    document = SourceDocument(
        law_id="922-19",
        title="Law",
        url="https://zakon.rada.gov.ua/laws/show/922-19",
        text="Article 1. " + "x" * 5000,
        source="print_url",
        source_policy=SourcePolicy.HTML_PRINT,
        adapter="zakon_rada",
    )

    # A frozen pydantic model refuses the assignment with a ValidationError;
    # asserting on bare Exception would pass for a typo in the attribute name too.
    with pytest.raises(ValidationError):
        document.title = "Changed"


def test_zakon_rada_adapter_fetch_law_returns_legacy_dict(registry: LawRegistry) -> None:
    from registries.source_adapters import ZakonRadaAdapter

    scraper = MagicMock()
    scraper.backoff.status.return_value = {"blocked": False, "retry_after": None}
    scraper.fetch_law.return_value = {
        "law_id": "922-19",
        "title": "Law",
        "url": "https://zakon.rada.gov.ua/laws/show/922-19",
        "text": "Article 1. " + "x" * 5000,
        "source": "print_url",
        "amendment_date": "2026-05-11",
    }
    adapter = ZakonRadaAdapter(registry, SearchEngine(), scraper=scraper)

    result = adapter.fetch_law("922-19", registry.get_law("922-19") or {})

    assert result["law_id"] == "922-19"
    assert result["source"] == "print_url"
    assert result["adapter"] == "zakon_rada"
    assert result["source_policy"] == "html_print"
    assert len(result["content_hash"]) == 64


def test_zakon_rada_adapter_owns_default_scraper(
    registry: LawRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    from registries.source_adapters import ZakonRadaAdapter

    # Mock connectivity check to avoid live network call
    monkeypatch.setattr(
        "registries.source_adapters.ZakonRadaAdapter._check_connectivity", lambda self: True
    )

    adapter = ZakonRadaAdapter(registry, SearchEngine())
    health = adapter.health()

    assert adapter.name == "zakon_rada"
    assert hasattr(adapter, "backoff")
    assert health.ok is True
    assert health.adapter == "zakon_rada"


def test_zakon_rada_adapter_wraps_scraper_errors(registry: LawRegistry) -> None:
    from core.contracts import SourceAdapterError
    from registries.source_adapters import ZakonRadaAdapter

    scraper = MagicMock()
    scraper.backoff.status.return_value = {"blocked": False, "retry_after": None}
    scraper.fetch_law.side_effect = RuntimeError("network down")
    adapter = ZakonRadaAdapter(registry, SearchEngine(), scraper=scraper)

    with pytest.raises(SourceAdapterError):
        adapter.fetch_law("922-19", registry.get_law("922-19") or {})


def test_zakon_rada_adapter_wraps_invalid_payload(registry: LawRegistry) -> None:
    from core.contracts import SourceAdapterError
    from registries.source_adapters import ZakonRadaAdapter

    scraper = MagicMock()
    scraper.backoff.status.return_value = {"blocked": False, "retry_after": None}
    scraper.fetch_law.return_value = {
        "law_id": "922-19",
        "title": "Law",
        "url": "https://zakon.rada.gov.ua/laws/show/922-19",
        "text": "",
        "source": "print_url",
    }
    adapter = ZakonRadaAdapter(registry, SearchEngine(), scraper=scraper)

    with pytest.raises(SourceAdapterError):
        adapter.fetch_law("922-19", registry.get_law("922-19") or {})


def test_cache_entry_includes_source_provenance(tmp_path: Path, registry: LawRegistry) -> None:
    cache = CacheManager(tmp_path, registry)
    adapter = MagicMock()
    adapter.fetch_law.return_value = {
        "law_id": "922-19",
        "title": "Law",
        "url": "https://zakon.rada.gov.ua/laws/show/922-19",
        "text": "Article 1. " + "x" * 5000,
        "source": "print_url",
        "adapter": "zakon_rada",
        "source_policy": "html_print",
        "content_hash": "a" * 64,
    }

    result = cache.get_or_fetch("922-19", adapter, SearchEngine())

    assert result["adapter"] == "zakon_rada"
    assert result["source_policy"] == "html_print"
    assert result["content_hash"] == "a" * 64


def test_cache_can_fetch_dynamic_zakon_rada_law(tmp_path: Path, registry: LawRegistry) -> None:
    cache = CacheManager(tmp_path, registry)
    adapter = MagicMock()
    adapter.fetch_law.return_value = {
        "law_id": "361-20",
        "title": "Zakon Rada document 361-20",
        "url": "https://zakon.rada.gov.ua/laws/show/361-20",
        "text": "Article 1. " + "x" * 5000,
        "source": "print_url",
        "adapter": "zakon_rada",
        "source_policy": "html_print",
        "content_hash": "b" * 64,
    }

    result = cache.get_or_fetch("361-20", adapter, SearchEngine())

    assert result["law_id"] == "361-20"
    assert result["url"] == "https://zakon.rada.gov.ua/laws/show/361-20"
    assert result["adapter"] == "zakon_rada"
