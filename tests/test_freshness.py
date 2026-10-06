"""Tests for freshness: volatile TTL, amendment_date, force_refresh."""

from __future__ import annotations

import datetime as dt
import json
import sys
from datetime import timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from registries.cache_manager import CacheManager
from registries.law_registry import LawRegistry
from storage.repository import RepositoryCacheAdapter, _freshness
from registries.scraper import Scraper
from search.search_engine import SearchEngine

REGISTRY_PATH = Path(__file__).parent.parent / "cache" / "laws.json"

STABLE_LAWS = ["435-15", "436-15"]  # ЦКУ, ГКУ — 90 days
# Load volatile IDs from the actual registry to avoid encoding mismatches
_reg = LawRegistry(REGISTRY_PATH)
VOLATILE_LAWS = [lid for lid, law in _reg._laws.items() if law.get("volatile")]
VOLATILE_LAW_922 = "922-19"  # safe ASCII ID for freshness tests


# ── fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def registry():
    return LawRegistry(REGISTRY_PATH)


@pytest.fixture(scope="module")
def cache_dir(tmp_path_factory):
    """Кеш тестів — у тимчасовому каталозі, а не в `cache/` теки коду (тікет 50)."""
    return tmp_path_factory.mktemp("texts")


@pytest.fixture(scope="module")
def cache(registry, cache_dir):
    return CacheManager(cache_dir, registry)


@pytest.fixture(scope="module")
def search():
    return SearchEngine()


# ── 1. Registry: volatile flag ─────────────────────────────────────────────


class TestVolatileFlag:
    def test_volatile_laws_marked(self, registry):
        for lid in VOLATILE_LAWS:
            assert registry.is_volatile(lid), f"{lid} must be volatile"

    def test_stable_laws_not_volatile(self, registry):
        for lid in STABLE_LAWS:
            assert not registry.is_volatile(lid), f"{lid} must not be volatile"

    def test_volatile_ttl_is_one_day(self, registry):
        for lid in VOLATILE_LAWS:
            ttl = registry.get_ttl_days(lid)
            assert ttl == 1, f"{lid} volatile TTL must be 1 day, got {ttl}"

    def test_stable_ttl_greater_than_one(self, registry):
        for lid in STABLE_LAWS:
            ttl = registry.get_ttl_days(lid)
            assert ttl > 1, f"{lid} stable TTL must be >1 day, got {ttl}"

    def test_unknown_law_not_volatile(self, registry):
        assert not registry.is_volatile("nonexistent-law")


# ── 2. Scraper: check_amendment_date ──────────────────────────────────────


class TestAmendmentDate:
    def _make_scraper(self):
        return Scraper()

    def test_parses_amendment_date_from_html(self):
        scraper = self._make_scraper()
        html = "<html><body>Редакція від 15.03.2024 чинна</body></html>"
        law_info = {"url": "https://zakon.rada.gov.ua/laws/show/922-19"}

        with patch("sources.transport.SourceTransport.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.content = html.encode("utf-8")
            mock_resp.raise_for_status = MagicMock()
            mock_get.return_value = mock_resp

            result = scraper.check_amendment_date("922-19", law_info)

        assert result == "2024-03-15"

    def test_returns_none_when_no_date_found(self):
        scraper = self._make_scraper()
        law_info = {"url": "https://zakon.rada.gov.ua/laws/show/922-19"}

        with patch("sources.transport.SourceTransport.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.content = b"<html><body>No date here</body></html>"
            mock_resp.raise_for_status = MagicMock()
            mock_get.return_value = mock_resp

            result = scraper.check_amendment_date("922-19", law_info)

        assert result is None

    def test_returns_none_on_network_error(self):
        scraper = self._make_scraper()
        law_info = {"url": "https://zakon.rada.gov.ua/laws/show/922-19"}

        with patch("sources.transport.SourceTransport.get", side_effect=ConnectionError("timeout")):
            result = scraper.check_amendment_date("922-19", law_info)

        assert result is None

    def test_strips_print_suffix_from_url(self):
        scraper = self._make_scraper()
        law_info = {"url": "https://zakon.rada.gov.ua/laws/show/922-19/print"}

        with patch("sources.transport.SourceTransport.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.content = "Редакція від 01.01.2024".encode("utf-8")
            mock_resp.raise_for_status = MagicMock()
            mock_get.return_value = mock_resp

            scraper.check_amendment_date("922-19", law_info)

            called_url = mock_get.call_args[0][0]
            assert not called_url.endswith("/print"), "URL must not end with /print"

    def test_fetch_law_includes_amendment_date(self):
        scraper = self._make_scraper()
        law_info = {
            "url": "https://zakon.rada.gov.ua/laws/show/922-19",
            "print_url": "https://zakon.rada.gov.ua/laws/show/922-19/print",
            "title": "ЗУ 922",
        }

        html_main = "Редакція від 20.04.2024"
        html_print = "А" * 5000

        def fake_get(url, **kwargs):
            mock_resp = MagicMock()
            mock_resp.raise_for_status = MagicMock()
            if "/print" in url:
                mock_resp.text = html_print  # used by _try_print_url
                mock_resp.content = html_print.encode("utf-8")
            else:
                mock_resp.content = html_main.encode("utf-8")  # used by check_amendment_date
            return mock_resp

        with patch("sources.transport.SourceTransport.get", side_effect=fake_get):
            result = scraper.fetch_law("922-19", law_info)

        assert result.get("amendment_date") == "2024-04-20"


# ── 3. CacheManager: amendment_date stored ────────────────────────────────


class TestCacheStoresAmendmentDate:
    def test_amendment_date_persisted_in_cache(self, tmp_path, registry):
        cache = CacheManager(tmp_path, registry)
        search = SearchEngine()

        mock_scraper = MagicMock()
        mock_scraper.backoff.is_blocked.return_value = False
        mock_scraper.fetch_law.return_value = {
            "law_id": "922-19",
            "title": "ЗУ 922",
            "url": "https://zakon.rada.gov.ua/laws/show/922-19",
            "text": "Стаття 1. " + "А" * 5000,
            "source": "print_url",
            "amendment_date": "2024-04-15",
        }

        result = cache.get_or_fetch("922-19", mock_scraper, search)
        assert (
            result.get("amendment_date") == "2024-04-15"
        ), "amendment_date must be stored in cache entry"

        # Verify file also has amendment_date
        cache_file = tmp_path / "922-19.json"
        assert cache_file.exists()
        data = json.loads(cache_file.read_text(encoding="utf-8"))
        assert data.get("amendment_date") == "2024-04-15"

    def test_amendment_date_absent_when_scraper_returns_none(self, tmp_path, registry):
        cache = CacheManager(tmp_path, registry)
        search = SearchEngine()

        mock_scraper = MagicMock()
        mock_scraper.backoff.is_blocked.return_value = False
        mock_scraper.fetch_law.return_value = {
            "law_id": "435-15",
            "title": "ЦКУ",
            "url": "https://zakon.rada.gov.ua/laws/show/435-15",
            "text": "Стаття 1. " + "А" * 5000,
            "source": "print_url",
            # no amendment_date key
        }

        result = cache.get_or_fetch("435-15", mock_scraper, search)
        assert "amendment_date" not in result or result.get("amendment_date") is None


# ── 4. TTL freshness: volatile expires in 1 day ───────────────────────────


class TestVolatileFreshness:
    def _make_entry(self, hours_ago: float) -> dict:
        scraped_at = dt.datetime.now(timezone.utc) - dt.timedelta(hours=hours_ago)
        return {"scraped_at": scraped_at.isoformat(), "text": "x", "law_id": "922-19"}

    def test_volatile_fresh_within_1_day(self, registry, cache_dir):
        cache = CacheManager(cache_dir, registry)
        entry = self._make_entry(hours_ago=12)
        assert cache._is_fresh("922-19", entry), "12h old volatile law must be fresh"

    def test_volatile_stale_after_1_day(self, registry, cache_dir):
        cache = CacheManager(cache_dir, registry)
        entry = self._make_entry(hours_ago=25)
        assert not cache._is_fresh("922-19", entry), "25h old volatile law must be stale"

    def test_stable_fresh_within_90_days(self, registry, cache_dir):
        cache = CacheManager(cache_dir, registry)
        entry_45d = {
            "scraped_at": (dt.datetime.now(timezone.utc) - dt.timedelta(days=45)).isoformat(),
            "text": "x",
        }
        assert cache._is_fresh("435-15", entry_45d), "45d old stable law must be fresh"


# ── 5. force_refresh bypasses cache ───────────────────────────────────────


class TestForceRefresh:
    def test_force_refresh_skips_memory_cache(self, tmp_path, registry):
        cache = CacheManager(tmp_path, registry)
        search = SearchEngine()
        call_count = {"n": 0}

        def mock_fetch(law_id, law_info):
            call_count["n"] += 1
            return {
                "law_id": law_id,
                "title": "ЗУ 922",
                "url": "https://zakon.rada.gov.ua/laws/show/922-19",
                "text": "Стаття 1. " + "А" * 5000,
                "source": "print_url",
                "amendment_date": f"2024-04-{call_count['n']:02d}",
            }

        mock_scraper = MagicMock()
        mock_scraper.backoff.is_blocked.return_value = False
        mock_scraper.fetch_law.side_effect = mock_fetch

        # First fetch
        r1 = cache.get_or_fetch("922-19", mock_scraper, search, force_refresh=False)
        # Second fetch with force_refresh — must call scraper again
        r2 = cache.get_or_fetch("922-19", mock_scraper, search, force_refresh=True)

        assert call_count["n"] == 2, "force_refresh must trigger re-fetch"
        assert r1.get("amendment_date") != r2.get(
            "amendment_date"
        ), "force_refresh must return newly fetched data"


# -- 6. TTL is enforced on READ, not merely computed (ticket 15, cause 1) ----
#
# get_status() has always computed freshness correctly and nothing consulted it,
# so get_current() served three-month-old rows as current. These tests pin the
# behaviour at the two places that decide: the age comparison itself, and the
# adapter that must refetch instead of answering from an expired row.


class TestFreshnessHelper:
    def _stamp(self, days_ago: float) -> dt.datetime:
        return dt.datetime.now(timezone.utc) - dt.timedelta(days=days_ago)

    def test_row_within_ttl_is_fresh(self):
        fresh, age_days = _freshness(self._stamp(10), 30)
        assert fresh is True and age_days == 10

    def test_row_past_ttl_is_stale(self):
        fresh, age_days = _freshness(self._stamp(89), 30)
        assert fresh is False and age_days == 89

    def test_the_three_month_freeze_is_stale_at_every_ttl_in_the_registry(self):
        # 2026-06-06 -> 2026-09-03 is 89 days; no TTL in cache/laws.json survives it
        frozen = self._stamp(89)
        for ttl in (1, 7, 30, 60, 90):
            fresh, _ = _freshness(frozen, ttl)
            assert fresh is (ttl > 89), f"ttl={ttl}"

    def test_naive_timestamp_is_treated_as_utc(self):
        naive = (dt.datetime.now(timezone.utc) - dt.timedelta(days=2)).replace(tzinfo=None)
        fresh, age_days = _freshness(naive, 30)
        assert fresh is True and age_days == 2

    def test_missing_timestamp_is_stale(self):
        assert _freshness(None, 30) == (False, None)


class _Repo:
    """Repository stub: returns whatever entry it was given, records reads."""

    def __init__(self, entry):
        self.entry = entry
        self.allow_stale_seen = []

    def get_current(self, law_id, allow_stale=True):
        self.allow_stale_seen.append(allow_stale)
        if self.entry is None:
            return None
        if not allow_stale and self.entry.get("stale"):
            return None
        return dict(self.entry)

    def store_cache_entry(self, law_id, entry, search):
        return 1, True

    def get_status(self, law_id):
        return {"cached": True, "fresh": False, "char_count": 0, "source": None}


class _Cache:
    """Fallback cache stub standing in for the network path."""

    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = 0

    def get_or_fetch(self, law_id, scraper, search, force_refresh=False):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return dict(self.result or {})

    def get_cached_entry(self, law_id, allow_stale=False):
        return None

    def get_status(self, law_id):
        return {"cached": False, "fresh": False, "char_count": 0, "source": None}

    def get_metrics(self):
        return {}


FRESH_ENTRY = {"law_id": "922-19", "text": "свіжий", "fresh": True, "age_days": 1}
STALE_ENTRY = {
    "law_id": "922-19",
    "text": "протухлий",
    "fresh": False,
    "age_days": 89,
    "stale": True,
}


class TestStaleReadTriggersRefetch:
    def test_fresh_row_is_served_without_touching_the_network(self):
        cache = _Cache()
        adapter = RepositoryCacheAdapter(_Repo(FRESH_ENTRY), cache)

        result = adapter.get_or_fetch("922-19", scraper=None, search=SearchEngine())

        assert result["text"] == "свіжий"
        assert result["from_cache"] is True
        assert cache.calls == 0

    def test_stale_row_is_refetched_not_served(self):
        fetched = {"law_id": "922-19", "text": "перезавантажений", "from_cache": False}
        cache = _Cache(result=fetched)
        adapter = RepositoryCacheAdapter(_Repo(STALE_ENTRY), cache)

        result = adapter.get_or_fetch("922-19", scraper=None, search=SearchEngine())

        assert cache.calls == 1, "an expired row must send us to the live source"
        assert result["text"] == "перезавантажений"
        assert not result.get("stale")

    def test_stale_row_survives_a_dead_source_but_is_labelled(self):
        cache = _Cache(error=ConnectionError("rada unreachable"))
        adapter = RepositoryCacheAdapter(_Repo(STALE_ENTRY), cache)

        result = adapter.get_or_fetch("922-19", scraper=None, search=SearchEngine())

        assert result["text"] == "протухлий"
        assert result["stale"] is True, "stale data may be served, never as current"
        assert "rada unreachable" in result["error"]

    def test_empty_refetch_falls_back_to_the_stale_row_marked(self):
        cache = _Cache(result={"law_id": "922-19", "text": ""})
        adapter = RepositoryCacheAdapter(_Repo(STALE_ENTRY), cache)

        result = adapter.get_or_fetch("922-19", scraper=None, search=SearchEngine())

        assert result["text"] == "протухлий"
        assert result["stale"] is True

    def test_dead_source_and_no_stored_row_still_raises(self):
        cache = _Cache(error=ConnectionError("rada unreachable"))
        adapter = RepositoryCacheAdapter(_Repo(None), cache)

        with pytest.raises(ConnectionError):
            adapter.get_or_fetch("922-19", scraper=None, search=SearchEngine())

    def test_get_cached_entry_passes_allow_stale_through(self):
        repo = _Repo(STALE_ENTRY)
        adapter = RepositoryCacheAdapter(repo, _Cache())

        assert adapter.get_cached_entry("922-19", allow_stale=True)["text"] == "протухлий"
        assert adapter.get_cached_entry("922-19", allow_stale=False) is None
        assert repo.allow_stale_seen == [True, False]


# ---------------------------------------------------------------------------
# T047 — `stale = true` обязано попадать в текст, который видит юрист
#
# Принцип IV в исполняемом виде: незамеченная устаревшая норма — ошибка, а не
# техническая сноска. Проверяется на конверте происхождения (`provenance.py`,
# T008), который читает поле `stale`, выставленное фолбэком CacheManager
# (строки 73-82 выше), — а не на новом механизме.
# ---------------------------------------------------------------------------


class TestStaleIsVisibleToTheLawyer:
    def test_a_stale_cache_entry_produces_a_stale_envelope_with_a_visible_notice(self):
        from core.provenance import stamp_from_cache_entry

        entry = {
            "url": "https://zakon.rada.gov.ua/laws/show/922-19",
            "scraped_at": "2020-01-01T00:00:00+00:00",
            "stale": True,
            "from_cache": True,
        }
        stamp = stamp_from_cache_entry(entry, source_url="https://zakon.rada.gov.ua")

        assert stamp.stale is True
        # Служебное поле само по себе — не пометка: она обязана быть в notice.
        assert "застаріл" in stamp.notice.lower() or "недоступн" in stamp.notice.lower()
        assert stamp.may_be_quoted_as_norm is False, "устаревшая копия — не норма без оговорки"

    def test_a_notice_that_hides_staleness_is_rejected_by_construction(self):
        """Переданная извне фраза, умалчивающая об устаревании, не проходит
        валидацию конверта — служебное поле не заменяет текст для юриста."""
        from pydantic import ValidationError

        from core.provenance import ProvenanceStamp, SourceChannel

        with pytest.raises(ValidationError):
            ProvenanceStamp(
                source_channel=SourceChannel.INDEX,
                source_url="https://zakon.rada.gov.ua",
                language="uk",
                citation_format="стаття 1",
                stale=True,
                is_authentic_version=True,
                is_translation=False,
                notice="Відповідь отримано з кешу.",  # ни слова про застарілість
            )

    def test_a_fresh_entry_is_not_marked_stale(self):
        from core.provenance import stamp_from_cache_entry

        entry = {
            "url": "https://zakon.rada.gov.ua/laws/show/922-19",
            "scraped_at": dt.datetime.now(timezone.utc).isoformat(),
            "from_cache": True,
        }
        stamp = stamp_from_cache_entry(entry, source_url="https://zakon.rada.gov.ua")

        assert stamp.stale is False
        assert "застаріл" not in stamp.notice.lower()


class TestSourceUnavailableWhenNoCopyExists:
    """При недоступном источнике и наличии копии — `stale = true` (проверено
    выше через `TestStaleReadTriggersRefetch` и `TestStaleIsVisibleToTheLawyer`).
    При недоступном источнике и отсутствии копии — `source_unavailable`, а не
    пустой ответ и не авария (принцип IX)."""

    def test_an_adapter_that_raises_with_no_stored_copy_yields_source_unavailable(self):
        from sources.base import typed_failures

        @typed_failures("test_source", retry_after="через 15 хвилин")
        def _fetch_without_a_cached_fallback() -> None:
            raise ConnectionError("upstream unreachable, and nothing was ever cached")

        failure = _fetch_without_a_cached_fallback()
        output = failure.as_output()

        assert output["code"] == "source_unavailable"
        assert output["details"]["source"] == "test_source"
        assert output["details"]["retry_after"]

    def test_dead_source_and_no_stored_row_still_surfaces_as_a_typed_failure_upstream(self):
        """`RepositoryCacheAdapter` itself raises when neither a live fetch nor
        a stored row exists (see `test_dead_source_and_no_stored_row_still_raises`
        above) — the boundary that turns that into `source_unavailable` for the
        tool caller is `sources.base.typed_failures`, exercised directly above."""
        cache = _Cache(error=ConnectionError("rada unreachable"))
        adapter = RepositoryCacheAdapter(_Repo(None), cache)

        with pytest.raises(ConnectionError):
            adapter.get_or_fetch("922-19", scraper=None, search=SearchEngine())
