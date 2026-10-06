"""Routing: the live channel is primary, the index is a hot cache.

No test here touches the network (Constitution V). The Rada client is replaced by
fakes that record what they were asked for — which is the point: the filters and the
fall-through order are the behaviour under test, not the transport.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core import routing  # noqa: E402


@pytest.fixture(autouse=True)
def _engineering_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    """Живой поиск Ради — инженерный профиль; в legal строка запроса не уходит (ADR 0009)."""
    monkeypatch.setenv("YURKO_PROFILE", "engineering")


from core.contracts import SourceAdapterError, SourceRecordNotFound  # noqa: E402
from registries.rada_open_data import (  # noqa: E402
    TYPE_CODE,
    TYPE_LAW,
    RadaDocumentNotFound,
    RadaOpenDataError,
)

FIXTURES = Path(__file__).parent / "fixtures"

OPEN_DATA_LAW = (
    "Про публічні закупівлі\n"
    "\n"
    "Верховна Рада України; Закон від 25.12.2015 № 922-VIII\n"
    "\n"
    "Про публічні закупівлі\n"
    "\n"
    "Стан: Чинний\n"
    "Ідентифікатор: 922-19\n"
    "\n"
    "ЗАКОН УКРАЇНИ\n"
    "\n"
    "Стаття 1. Визначення понять\n"
)

OPEN_DATA_CODE = (
    "Кримінальний кодекс України\n"
    "\n"
    "Верховна Рада України; Кодекс України, Закон, Кодекс від 05.04.2001 № 2341-III\n"
    "\n"
    "Стан: Чинний\n"
    "\n"
    "КРИМІНАЛЬНИЙ КОДЕКС УКРАЇНИ\n"
    "\n"
    "Стаття 190. Шахрайство\n"
)

OPEN_DATA_BYLAW = (
    "Про затвердження особливостей здійснення публічних закупівель\n"
    "\n"
    "Кабінет Міністрів України; Постанова, Порядок від 12.10.2022 № 1178\n"
    "\n"
    "КАБІНЕТ МІНІСТРІВ УКРАЇНИ\n"
    "ПОСТАНОВА\n"
)

PRINT_LAW = (
    "Про лікарські засоби | від 04.04.1996 № 123/96-ВР (Текст для друку)\n"
    "\n"
    "Друкувати\n"
    "Допомога\n"
    "\n"
    "ЗАКОН УКРАЇНИ\n"
    "\n"
    "Про лікарські засоби\n"
)

PRINT_BYLAW = (
    "Про затвердження Порядку... | від 12.10.2022 № 1178 (Текст для друку)\n"
    "\n"
    "Друкувати\n"
    "\n"
    "КАБІНЕТ МІНІСТРІВ УКРАЇНИ\n"
    "ПОСТАНОВА\n"
    "\n"
    "Про затвердження Порядку проведення моніторингу закупівель\n"
)


# ---------------------------------------------------------------------------
# Classification — the seam fields this agent produces
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,law_id,expected",
    [
        (OPEN_DATA_LAW, "922-19", routing.KIND_LAW),
        (OPEN_DATA_CODE, "2341-14", routing.KIND_CODE),
        (OPEN_DATA_BYLAW, "1178-2022-п", routing.KIND_BYLAW),
        (PRINT_LAW, "123/96-вр", routing.KIND_LAW),
        (PRINT_BYLAW, "1178-2022-п", routing.KIND_BYLAW),
        ("", "", routing.KIND_UNKNOWN),
        ("нічого змістовного тут немає", "", routing.KIND_UNKNOWN),
    ],
)
def test_classify_doc_kind(text: str, law_id: str, expected: str) -> None:
    assert routing.classify_doc_kind(text, law_id=law_id) == expected


def test_classify_doc_kind_trusts_an_explicit_rada_type_code() -> None:
    assert routing.classify_doc_kind("", doc_type_code=1) == routing.KIND_LAW
    assert routing.classify_doc_kind("", doc_type_code="21") == routing.KIND_CODE
    assert routing.classify_doc_kind("", doc_type_code="216") == routing.KIND_CONSTITUTION
    assert routing.classify_doc_kind("", doc_type_code="8") == routing.KIND_BYLAW


def test_a_law_amending_a_code_is_still_a_law() -> None:
    """The first heading decides; a substring scan called this one a code."""
    text = (
        "Про внесення змін до Кримінального кодексу України | "
        "від 01.01.2020 № 3687-20 (Текст для друку)\n\nДрукувати\n\nЗАКОН УКРАЇНИ\n\n"
        "Про внесення змін до Кримінального кодексу України\n"
    )
    assert routing.classify_doc_kind(text, law_id="3687-20") == routing.KIND_LAW


def test_cacheable_kinds_exclude_bylaw_and_unknown() -> None:
    assert routing.KIND_BYLAW not in routing.CACHEABLE_KINDS
    assert routing.KIND_UNKNOWN not in routing.CACHEABLE_KINDS


def test_extract_real_title_from_both_page_shapes() -> None:
    assert routing.extract_real_title(OPEN_DATA_LAW) == "Про публічні закупівлі"
    assert routing.extract_real_title(PRINT_LAW) == "Про лікарські засоби"
    assert routing.extract_real_title("") == ""


def test_extract_real_title_undoes_the_truncated_print_header() -> None:
    assert (
        routing.extract_real_title(PRINT_BYLAW)
        == "Про затвердження Порядку проведення моніторингу закупівель"
    )


def test_classification_matches_the_live_document_fixture() -> None:
    """Fixture cut from a real data.rada.gov.ua answer (see tests/fixtures)."""
    from registries.scraper import _HtmlTextExtractor

    html = (FIXTURES / "rada_document.html").read_text(encoding="utf-8", errors="replace")
    extractor = _HtmlTextExtractor()
    extractor.feed(html)
    text = extractor.as_text()

    assert routing.classify_doc_kind(text, law_id="2210-14") == routing.KIND_LAW
    assert routing.extract_real_title(text) == "Про захист економічної конкуренції"


# ---------------------------------------------------------------------------
# The loader: live first, print page second, honest failure third
# ---------------------------------------------------------------------------


class FakeClient:
    def __init__(self, document: dict[str, Any] | None = None, error: str = "") -> None:
        self._document = document
        self._error = error
        self.calls: list[str] = []
        self.searches: list[dict[str, Any]] = []

    def fetch_document(self, law_id: str) -> dict[str, Any]:
        self.calls.append(law_id)
        if self._error:
            raise RadaOpenDataError(self._error)
        assert self._document is not None
        return self._document

    def search(self, text: str, **kwargs: Any) -> dict[str, Any]:
        self.searches.append({"text": text, **kwargs})
        return {
            "query": text,
            "format": "page",
            "found": 47,
            "results": [
                {
                    "law_id": "4619-20",
                    "title": "Про публічно-приватне партнерство",
                    "status": "Чинний",
                    "url": "https://zakon.rada.gov.ua/laws/show/4619-20",
                }
            ],
            "from_cache": False,
            "retrieved_at": "2026-09-03T19:15:00+00:00",
        }


class FakePrintAdapter:
    name = "zakon_rada"
    source_policy = "html_print"
    backoff = object()

    def __init__(self, payload: dict[str, Any] | None = None, fail: bool = False) -> None:
        self._payload = payload
        self._fail = fail
        self.calls: list[str] = []

    def fetch_law(self, law_id: str, law_info: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(law_id)
        if self._fail or self._payload is None:
            raise RuntimeError("print page unavailable")
        return dict(self._payload)

    def resolve(self, query: str) -> dict[str, Any] | None:
        return None


def _law_info() -> dict[str, Any]:
    return {
        "id": "922-19",
        "title": "Zakon Rada document 922-19",
        "url": "https://zakon.rada.gov.ua/laws/show/922-19",
    }


def test_loader_prefers_the_live_channel_and_produces_the_seam_fields() -> None:
    client = FakeClient(
        {
            "law_id": "922-19",
            "text": OPEN_DATA_LAW,
            "public_url": "https://zakon.rada.gov.ua/laws/show/922-19",
            "source": "rada_open_data",
            "retrieved_at": "2026-09-03T19:15:00+00:00",
        }
    )
    print_adapter = FakePrintAdapter()
    loader = routing.LiveFirstLoader(client, print_adapter)

    payload = loader.fetch_law("922-19", _law_info())

    assert client.calls == ["922-19"]
    assert print_adapter.calls == []
    assert payload["source"] == "rada_open_data"
    assert payload["doc_kind"] == routing.KIND_LAW
    assert payload["real_title"] == "Про публічні закупівлі"
    # The registry placeholder must not survive into the stored entry.
    assert payload["title"] == "Про публічні закупівлі"


def test_loader_falls_back_to_the_print_page_when_open_data_fails() -> None:
    client = FakeClient(error="data.rada answered 500 for 922-19")
    print_adapter = FakePrintAdapter(
        {
            "law_id": "922-19",
            "title": "Zakon Rada document 922-19",
            "url": "https://zakon.rada.gov.ua/laws/show/922-19/print",
            "text": PRINT_LAW,
            "source": "print_url",
        }
    )
    loader = routing.LiveFirstLoader(client, print_adapter)

    payload = loader.fetch_law("922-19", _law_info())

    assert print_adapter.calls == ["922-19"]
    assert payload["source"] == "print_url"
    assert payload["doc_kind"] == routing.KIND_LAW
    assert payload["real_title"] == "Про лікарські засоби"


def test_loader_raises_source_adapter_error_when_both_channels_fail() -> None:
    loader = routing.LiveFirstLoader(FakeClient(error="boom"), FakePrintAdapter(fail=True))

    with pytest.raises(SourceAdapterError):
        loader.fetch_law("922-19", _law_info())


def test_bylaw_is_produced_as_bylaw_so_the_gate_can_refuse_it() -> None:
    client = FakeClient(
        {
            "law_id": "1178-2022-п",
            "text": OPEN_DATA_BYLAW,
            "public_url": "https://zakon.rada.gov.ua/laws/show/1178-2022-п",
            "source": "rada_open_data",
        }
    )
    loader = routing.LiveFirstLoader(client, FakePrintAdapter())

    payload = loader.fetch_law("1178-2022-п", {"id": "1178-2022-п"})

    assert payload["doc_kind"] == routing.KIND_BYLAW
    assert payload["doc_kind"] not in routing.CACHEABLE_KINDS


# ---------------------------------------------------------------------------
# The metadata wrapper: the seam fields must survive the cache layer
# ---------------------------------------------------------------------------


class FakeInnerCache:
    def __init__(self, entry: dict[str, Any]) -> None:
        self._entry = entry
        self.fetch_calls = 0

    def get_or_fetch(
        self, law_id: str, scraper: Any, search: Any, force_refresh: bool = False
    ) -> dict[str, Any]:
        self.fetch_calls += 1
        return dict(self._entry)

    def get_cached_entry(self, law_id: str, allow_stale: bool = False) -> dict[str, Any] | None:
        return dict(self._entry)

    def get_status(self, law_id: str) -> dict[str, Any]:
        return {"cached": True, "fresh": True}

    def get_metrics(self) -> dict[str, Any]:
        return {"backend": "file"}

    def count_cached_laws(self) -> int:
        return 1


def test_metadata_cache_reattaches_fields_the_cache_layer_drops() -> None:
    """CacheManager rebuilds its entry from a fixed key set — these two go missing."""
    inner = FakeInnerCache({"law_id": "922-19", "text": OPEN_DATA_LAW, "from_cache": False})
    wrapper = routing.DocumentMetadataCache(inner)

    entry = wrapper.get_or_fetch("922-19", object(), object())

    assert entry["doc_kind"] == routing.KIND_LAW
    assert entry["real_title"] == "Про публічні закупівлі"
    cached = wrapper.get_cached_entry("922-19")
    assert cached is not None and cached["doc_kind"] == routing.KIND_LAW


# ---------------------------------------------------------------------------
# Provenance: an aged answer must say that it is aged
# ---------------------------------------------------------------------------


def test_provenance_marks_a_live_answer() -> None:
    marks = routing.provenance({"from_cache": False, "retrieved_at": "2026-09-03T19:00:00+00:00"})

    assert marks["source_channel"] == "live"
    assert marks["stale"] is False
    assert "наживо" in marks["freshness_notice"]


def test_provenance_marks_a_cached_answer_with_its_age() -> None:
    marks = routing.provenance({"from_cache": True, "scraped_at": "2020-01-01T00:00:00+00:00"})

    assert marks["source_channel"] == "index"
    assert marks["cached_at"] == "2020-01-01T00:00:00+00:00"
    assert marks["age_days"] is not None and marks["age_days"] > 1000
    assert "кеш" in marks["freshness_notice"]


def test_provenance_marks_a_stale_answer_served_because_rada_is_down() -> None:
    marks = routing.provenance(
        {"from_cache": True, "stale": True, "scraped_at": "2026-01-01T00:00:00+00:00"}
    )

    assert marks["stale"] is True
    assert "недоступне" in marks["freshness_notice"]


def test_provenance_of_nothing_is_not_a_crash() -> None:
    marks = routing.provenance(None)

    assert marks["source_channel"] == "live"
    assert marks["cached_at"] is None
    assert marks["age_days"] is None


# ---------------------------------------------------------------------------
# Live search: laws and codes only
# ---------------------------------------------------------------------------


def test_live_search_filters_to_laws_and_codes() -> None:
    client = FakeClient()

    payload = routing.search_laws_live(client, "публічні закупівлі", max_results=5)

    assert client.searches[0]["types"] == (TYPE_LAW, TYPE_CODE)
    assert client.searches[0]["status"] == "5"
    assert payload["source_channel"] == "live"
    assert payload["found"] == 47
    assert payload["results"][0]["law_id"] == "4619-20"
    assert payload["retrieved_at"]


def test_pick_title_match_ignores_a_body_only_hit() -> None:
    """Measured live: «Про рейтингування» ranked «Про публічно-приватне партнерство» first."""
    results = [
        {"law_id": "4510-20", "title": "Про публічно-приватне партнерство"},
        {"law_id": "3981-20", "title": "Про рейтингування"},
    ]

    picked = routing.pick_title_match("Про рейтингування", results)

    assert picked is not None and picked["law_id"] == "3981-20"


def test_pick_title_match_refuses_to_guess() -> None:
    results = [{"law_id": "4510-20", "title": "Про публічно-приватне партнерство"}]

    assert routing.pick_title_match("шахрайство", results) is None


def test_resolve_law_id_reports_not_found_rather_than_the_wrong_law(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import server

    monkeypatch.setenv("RADA_LIVE_CHANNEL", "1")
    monkeypatch.setattr(server, "rada_client", FakeClient())
    monkeypatch.setattr(server.source_adapter, "resolve", lambda query: None)
    monkeypatch.setattr(server, "_resolve_from_open_data", lambda query: None)

    result = server.resolve_law_id("UA", "шахрайство")

    assert result["id"] is None
    assert result["code"] == "not_found"


def test_live_search_degrades_to_none_instead_of_raising() -> None:
    class DeadClient:
        def search(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            raise RadaOpenDataError("connection reset")

    assert routing.try_search_laws_live(DeadClient(), "шахрайство") is None


# ---------------------------------------------------------------------------
# server.py wiring — including the regression of ticket 14
# ---------------------------------------------------------------------------


def test_index_miss_still_reaches_the_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """The SQL slice is an optimisation, never a replacement for the live path.

    A miss returning None once meant an empty answer (ticket 14). This pins the
    fall-through: slice misses, loader runs, text comes back.
    """
    import server

    calls: dict[str, Any] = {"limited": 0, "fetched": 0}

    class SliceCache:
        def get_current_limited(self, law_id: str, limit_chars: int) -> dict[str, Any] | None:
            calls["limited"] += 1
            return None  # the law is not in the working set

        def get_or_fetch(
            self, law_id: str, scraper: Any, search: Any, force_refresh: bool = False
        ) -> dict[str, Any]:
            calls["fetched"] += 1
            assert scraper is server.live_loader, "the live loader must be the one asked"
            return {
                "law_id": law_id,
                "title": "Кримінальний кодекс України",
                "text": OPEN_DATA_CODE,
                "articles": {"190": "Стаття 190. Шахрайство"},
                "source": "rada_open_data",
                "from_cache": False,
                "scraped_at": "2026-09-03T19:15:00+00:00",
            }

    monkeypatch.setattr(server, "USE_PG_BACKEND", True)
    monkeypatch.setattr(server, "cache", SliceCache())

    result = server.query_law("UA", "2341-14", tokens=500)

    assert calls == {"limited": 1, "fetched": 1}
    assert "Шахрайство" in result["text"]
    assert result["source_channel"] == "live"
    assert result["from_cache"] is False


def test_rada_down_answers_from_cache_with_a_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    import server

    class StaleCache:
        def get_or_fetch(
            self, law_id: str, scraper: Any, search: Any, force_refresh: bool = False
        ) -> dict[str, Any]:
            return {
                "law_id": law_id,
                "title": "Про публічні закупівлі",
                "text": OPEN_DATA_LAW,
                "articles": {"1": "Стаття 1. Визначення понять"},
                "source": "rada_open_data",
                "from_cache": True,
                "stale": True,
                "error": "connection reset",
                "scraped_at": "2026-01-01T00:00:00+00:00",
            }

    monkeypatch.setattr(server, "cache", StaleCache())

    result = server.query_law("UA", "922-19", tokens=500)

    assert result["stale"] is True
    assert result["source_channel"] == "index"
    assert "недоступне" in result["freshness_notice"]
    assert result["age_days"] > 100


def test_nothing_cached_and_rada_down_is_an_honest_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import server

    class DeadCache:
        def get_or_fetch(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            raise SourceAdapterError("both Rada channels failed for 922-19")

    monkeypatch.setattr(server, "cache", DeadCache())

    result = server.query_law("UA", "922-19", tokens=500)

    assert result["code"] == "source_unavailable"
    assert "text" not in result


def test_a_tender_that_does_not_exist_is_not_told_as_an_outage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Тікет 24: «спробуйте пізніше» там, де пробувати нічого, — хибна порада.

    Prozorro на невідомий ідентифікатор відповідає 404 (жива проба
    16.09.2026), і юрист має почути «такого тендера немає», а не «джерело
    тимчасово недоступне».
    """
    import server

    def missing(tender_id: str) -> dict[str, Any]:
        raise SourceRecordNotFound(f"prozorro has no tender {tender_id}")

    monkeypatch.setattr(server.prozorro_registry, "get_tender", missing)

    result = server.get_tender("a" * 32)

    assert result["code"] == "not_found"
    assert "no tender" in result["error"].lower()


def test_prozorro_being_down_is_still_told_as_an_outage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import server

    def down(tender_id: str) -> dict[str, Any]:
        raise SourceAdapterError("prozorro failed")

    monkeypatch.setattr(server.prozorro_registry, "get_tender", down)

    result = server.get_tender("a" * 32)

    assert result["code"] == "source_unavailable"


def test_resolve_law_id_uses_live_search_when_the_index_does_not_know(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import server

    client = FakeClient()
    monkeypatch.setenv("RADA_LIVE_CHANNEL", "1")
    monkeypatch.setattr(server, "rada_client", client)
    monkeypatch.setattr(server.source_adapter, "resolve", lambda query: None)

    result = server.resolve_law_id("UA", "публічно-приватне партнерство")

    assert result["id"] == "4619-20"
    assert result["source"] == "rada_live_search"
    assert result["source_channel"] == "live"
    assert client.searches[0]["types"] == (TYPE_LAW, TYPE_CODE)


def test_discover_laws_prefers_live_search(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("YURKO_PROFILE", "engineering")
    import server

    client = FakeClient()
    monkeypatch.setenv("RADA_LIVE_CHANNEL", "1")
    monkeypatch.setattr(server, "rada_client", client)

    def explode(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("the catalogue must not be consulted while Rada answers")

    monkeypatch.setattr(server.open_data_discovery, "search", explode)

    result = server.discover_laws("публічні закупівлі", 5)

    assert result["source_channel"] == "live"
    assert result["results"][0]["law_id"] == "4619-20"


def test_discover_laws_degrades_to_the_catalogue_with_a_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import server

    class DeadClient:
        def search(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            raise RadaOpenDataError("connection reset")

    monkeypatch.setenv("RADA_LIVE_CHANNEL", "1")
    monkeypatch.setattr(server, "rada_client", DeadClient())
    monkeypatch.setattr(
        server.open_data_discovery,
        "search",
        lambda query, max_results: {
            "query": query,
            "results": [{"law_id": "922-19", "title": "Про публічні закупівлі"}],
            "found": 1,
            "source": "doc.zip",
            "from_cache": True,
            "retrieved_at": "2026-09-03T00:00:00+00:00",
        },
    )

    result = server.discover_laws("публічні закупівлі", 5)

    assert result["source_channel"] == "index"
    assert "недоступний" in result["freshness_notice"]


def test_live_search_is_on_in_production_and_switchable_by_the_operator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The switch is for operators, not for the test suite.

    `_live_channel_enabled` must not consult `PYTEST_CURRENT_TEST`: production
    code that behaves differently under test guarantees the tested path is not
    the shipped one. Constitution V is enforced in `tests/conftest.py`, which
    replaces the Rada client with one that refuses to open a socket.
    """
    import server

    monkeypatch.delenv("RADA_LIVE_CHANNEL", raising=False)
    assert server._live_channel_enabled() is True

    monkeypatch.setenv("RADA_LIVE_CHANNEL", "0")
    assert server._live_channel_enabled() is False


def test_the_suite_mutes_the_network_without_the_product_knowing() -> None:
    """The autouse fixture stands in for the real client in every test."""
    import server

    with pytest.raises(RuntimeError, match="Constitution V"):
        server.rada_client.search("шахрайство")


# --- "no such document" is an answer, not an outage ---------------------------


class NotFoundClient:
    """data.rada answering 404: the document does not exist."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def fetch_document(self, law_id: str) -> dict[str, Any]:
        self.calls.append(law_id)
        raise RadaDocumentNotFound(f"data.rada has no document {law_id} (404)")

    def search(self, text: str, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("search must not be called here")


def test_a_404_stops_the_chain_instead_of_falling_through() -> None:
    """A 404 must not cost a print-page attempt and a Playwright launch.

    Measured before this guard: query_law on a non-existent id took 52.8 seconds
    and ended in "source temporarily unavailable" — slow, and false. Rada had
    already answered: there is no such document.
    """
    client = NotFoundClient()
    printer = FakePrintAdapter(fail=True)
    loader = routing.LiveFirstLoader(client, printer)

    with pytest.raises(routing.DocumentNotFound):
        loader.fetch_law("99999-99", {"id": "99999-99", "title": "Zakon Rada document 99999-99"})

    assert client.calls == ["99999-99"]
    assert printer.calls == [], "the print page must not be tried after a definitive 404"


def test_document_not_found_is_not_a_source_failure() -> None:
    """The two need opposite answers, so they must not share a type."""
    assert not issubclass(routing.DocumentNotFound, SourceAdapterError)


def test_an_unreachable_host_still_falls_through_to_the_print_page() -> None:
    """Only a 404 short-circuits; a transport failure keeps the fallback."""
    client = FakeClient(error="connection reset")
    printer = FakePrintAdapter({"text": OPEN_DATA_LAW, "title": "Про публічні закупівлі"})
    loader = routing.LiveFirstLoader(client, printer)

    result = loader.fetch_law("922-19", _law_info())

    assert printer.calls == ["922-19"]
    assert result["real_title"] == "Про публічні закупівлі"


# ---------------------------------------------------------------------------
# T187 — план задачи строится до сети, по карте возможностей
# ---------------------------------------------------------------------------


@pytest.fixture()
def _coverage_map() -> None:
    """Карта покрытия и адаптеры в известном состоянии.

    Планировщик отвечает по реестрам, а не по сети, поэтому реестры и есть
    предмет проверки; собирать их надо явно, иначе результат зависел бы от
    порядка запуска тестов.
    """
    from core import legal_orders
    import sources

    legal_orders.register_default_sources()
    legal_orders.register_default_capabilities()
    sources.load_adapters()
