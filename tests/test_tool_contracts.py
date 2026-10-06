from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.contracts import (
    CONTRACT_VERSION,
    DiscoverLawsInput,
    DiscoverLawsOutput,
    ErrorCode,
    GetArticleOutput,
    GetFragmentsInput,
    GetMultipleArticlesOutput,
    LawMetadataOutput,
    ListLawsOutput,
    QueryDocumentInput,
    QueryLawOutput,
    ResolveLawOutput,
    SearchAcrossLawsInput,
    SearchAcrossLawsOutput,
    friendly_error,
)


def test_query_law_input_clamps_tokens() -> None:
    low = QueryDocumentInput(legal_order="UA", document_id="922-19", tokens=1)
    high = QueryDocumentInput(legal_order="UA", document_id="922-19", tokens=100000)

    assert low.tokens == 200
    assert high.tokens == 20000


def test_search_across_laws_input_trims_and_clamps_limit() -> None:
    parsed = SearchAcrossLawsInput(legal_order="UA", query="  договір  ", max_results=500)

    assert parsed.query == "договір"
    assert parsed.max_results == 20


def test_discover_laws_input_trims_and_clamps_limit() -> None:
    parsed = DiscoverLawsInput(query="  procurement  ", max_results=500)

    assert parsed.query == "procurement"
    assert parsed.max_results == 25


def test_get_multiple_articles_requires_non_empty_list() -> None:
    with pytest.raises(ValueError):
        GetFragmentsInput(legal_order="UA", document_id="922-19", paths=[])


def test_friendly_error_shape_is_stable() -> None:
    result = friendly_error(
        "Law not found.",
        ErrorCode.NOT_FOUND,
        details={"law_id": "missing"},
    )

    assert result == {
        "error": "Law not found.",
        "code": "not_found",
        "details": {"law_id": "missing"},
        "schema_version": CONTRACT_VERSION,
    }


def test_resolve_law_id_empty_query_uses_friendly_error() -> None:
    import server

    result = server.resolve_law_id("UA", "   ")

    assert result["id"] is None
    assert result["error"] == "Query is empty."
    assert result["code"] == "invalid_input"
    assert result["details"] == {}


def test_resolve_law_id_falls_back_to_open_data(monkeypatch) -> None:
    import server

    monkeypatch.setattr(server.source_adapter, "resolve", lambda query: None)
    monkeypatch.setattr(
        server.open_data_discovery,
        "resolve_best",
        lambda query: {
            "law_id": "2849-20",
            "title": "Про медіа",
            "url": "https://zakon.rada.gov.ua/laws/show/2849-20",
            "print_url": "https://zakon.rada.gov.ua/laws/show/2849-20/print",
            "accepted_at": "20221213",
            "updated_at": "20260423",
            "source": "rada_open_data_doc_cards",
        },
    )

    result = server.resolve_law_id("UA", "Про медіа")

    assert result["id"] == "2849-20"
    assert result["discovered"] is True
    assert result["source"] == "rada_open_data_doc_cards"


def test_query_law_accepts_open_data_title(monkeypatch) -> None:
    import server

    monkeypatch.setattr(
        server,
        "_resolve_from_open_data",
        lambda query: {
            "id": "2849-20",
            "title": "Про медіа",
            "url": "https://zakon.rada.gov.ua/laws/show/2849-20",
            "print_url": "https://zakon.rada.gov.ua/laws/show/2849-20/print",
            "cache_ttl_days": 7,
            "discovered": True,
            "source": "rada_open_data_doc_cards",
        },
    )

    def fake_fetch(law_id, *args, **kwargs):
        assert law_id == "2849-20"
        return {
            "law_id": "2849-20",
            "title": "Zakon Rada document 2849-20",
            "url": "https://zakon.rada.gov.ua/laws/show/2849-20",
            "text": "Стаття 1. Медіа регулюються цим Законом.",
            "articles": {"1": "Стаття 1. Медіа регулюються цим Законом."},
            "source": "test",
            "from_cache": True,
        }

    monkeypatch.setattr(server.cache, "get_or_fetch", fake_fetch)

    result = server.query_law("UA", "Про медіа", "медіа", tokens=200)

    assert result["document_id"] == "Про медіа"
    assert result["resolved_document_id"] == "2849-20"
    assert result["title"] == "Про медіа"
    assert "Медіа" in result["text"]


MEDIA_LAW = {
    "law_id": "2849-20",
    "title": "Про медіа",
    "text": "Стаття 1. Медіа регулюються цим Законом.",
}


def _write_cached_law(texts: Path, law: dict[str, str]) -> None:
    """Кладе документ у гарячий кеш так, як це робить читання: JSON за `law_id`."""
    texts.mkdir(parents=True, exist_ok=True)
    (texts / f"{law['law_id']}.json").write_text(
        json.dumps(law, ensure_ascii=False), encoding="utf-8"
    )


@pytest.fixture()
def hot_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Порожній гарячий кеш і порожній індекс, що не залежать від машини.

    `search_across_laws` шукає в локальному FTS-індексі, який будується з
    `server.LOCAL_TEXTS_DIR` і лежить у `cache_dir()` (`YURKO_CACHE_DIR`, за
    замовчуванням — каталог даних користувача). Без підміни обох тест читає
    корпус розробника й індекс машини: там, де їх немає, він падає, а там, де
    вони є, — проходить, бо в них випадково є той самий закон.
    """
    import server

    texts = tmp_path / "texts"
    texts.mkdir()
    monkeypatch.setenv("YURKO_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(server, "LOCAL_TEXTS_DIR", texts)
    return texts


def test_search_across_laws_uses_open_data_candidates(monkeypatch, hot_cache: Path) -> None:
    import server

    monkeypatch.setattr(server.registry, "list_laws", lambda: [])
    monkeypatch.setattr(server.registry, "find_by_alias", lambda query: [])
    monkeypatch.setattr(
        server.open_data_discovery,
        "search",
        lambda query, max_results: {
            "query": query,
            "results": [
                {
                    "law_id": "2849-20",
                    "title": "Про медіа",
                    "url": "https://zakon.rada.gov.ua/laws/show/2849-20",
                    "print_url": "https://zakon.rada.gov.ua/laws/show/2849-20/print",
                    "source": "rada_open_data_doc_cards",
                }
            ],
            "found": 1,
            "source": "https://data.rada.gov.ua/ogd/zak/laws/data/csv/doc.zip",
            "from_cache": True,
            "retrieved_at": "2026-05-12T00:00:00+00:00",
        },
    )
    monkeypatch.setattr(server.cache, "get_cached_entry", lambda *args, **kwargs: None)

    def fetch_into_hot_cache(*args: Any, **kwargs: Any) -> dict[str, Any]:
        # Справжнє читання кладе документ у гарячий кеш, з якого перебудовується
        # індекс; підмінене читання робить те саме, і кандидат з відкритих даних
        # доходить до видачі тим самим шляхом.
        _write_cached_law(hot_cache, MEDIA_LAW)
        return {
            **MEDIA_LAW,
            "url": "https://zakon.rada.gov.ua/laws/show/2849-20",
            "articles": {"1": "Стаття 1. Медіа регулюються цим Законом."},
        }

    monkeypatch.setattr(server.cache, "get_or_fetch", fetch_into_hot_cache)

    result = server.search_across_laws("UA", "медіа", max_results=1)

    assert result["found_in"] == 1
    assert result["results"][0]["law_id"] == "2849-20"


def test_get_multiple_articles_invalid_input_uses_friendly_error() -> None:
    import server

    result = server.get_multiple_articles("UA", "922-19", [])

    assert result["code"] == "invalid_input"
    assert "paths" in result["error"]


def test_get_article_source_failure_uses_friendly_error(monkeypatch) -> None:
    import server
    from core.contracts import SourceAdapterError

    def fail_fetch(*args, **kwargs):
        raise SourceAdapterError("Zakon Rada unavailable")

    monkeypatch.setattr(server.cache, "get_or_fetch", fail_fetch)

    result = server.get_article("UA", "922-19", "1")

    assert result["code"] == "source_unavailable"
    assert result["details"]["source"] == "ua_rada_open_data"


def test_get_article_unknown_article_returns_not_found_error(monkeypatch) -> None:
    """Regression: get_article must never fabricate another article's text
    when the requested number doesn't exist (was silently returning an
    unrelated article via a full-text relevance fallback)."""
    import server

    monkeypatch.setattr(
        server.cache,
        "get_or_fetch",
        lambda *args, **kwargs: {
            "law_id": "922-19",
            "title": "Про публічні закупівлі",
            "url": "https://zakon.rada.gov.ua/laws/show/922-19",
            "text": "Стаття 1.\nПерша стаття.\n\nСтаття 2.\nДруга стаття.\n",
            "articles": {"1": "Стаття 1.\nПерша стаття.", "2": "Стаття 2.\nДруга стаття."},
            "from_cache": True,
        },
    )

    result = server.get_article("UA", "922-19", "49")

    assert result["code"] == "not_found"
    assert "text" not in result
    assert result["details"]["identifier"] == "922-19#49"


def test_parse_articles_skips_quoted_excerpt_from_another_act() -> None:
    """Regression: Прикінцеві положення often quote a *new* article being
    inserted into a DIFFERENT law (e.g. КУпАП). That quoted header must not
    be captured as this law's own article -- it previously created a phantom
    key (e.g. "164") whose text belonged to a foreign act."""
    from search.search_engine import SearchEngine

    text = (
        "Стаття 1.\n"
        "Перша стаття закону.\n\n"
        "статтю 164-14 Кодексу України про адміністративні правопорушення "
        "викласти в такій редакції:\n"
        '"\n'
        "Стаття 164-14. Порушення законодавства про закупівлі\n\n"
        "Текст чужої статті.\n\n"
        "Стаття 2.\n"
        "Друга стаття закону.\n"
    )

    articles = SearchEngine().parse_articles(text)

    assert "164-14" not in articles, "Quoted foreign-act excerpt leaked in as own article"
    assert "164" not in articles
    assert "1" in articles and "2" in articles


def test_tool_success_outputs_validate_against_contracts(monkeypatch, hot_cache: Path) -> None:
    import server

    # `search_across_laws` шукає в локальному індексі, який будується з гарячого
    # кешу: без документа в ньому вибірка порожня, і тест залежав би від корпусу
    # на машині (тікет 50).
    _write_cached_law(
        hot_cache,
        {
            "law_id": "922-19",
            "title": "ЗУ Про публічні закупівлі",
            "text": "Стаття 1. Визначення. договір про закупівлю.\n\nСтаття 2. Сфера. договір.",
        },
    )
    monkeypatch.setattr(
        server.open_data_discovery,
        "search",
        lambda query, max_results: {
            "query": query,
            "results": [],
            "found": 0,
            "source": "https://data.rada.gov.ua/ogd/zak/laws/data/csv/doc.zip",
            "from_cache": True,
            "retrieved_at": "2026-05-12T00:00:00+00:00",
        },
    )

    # Keep this contract check fully offline (no live zakon.rada fetch): serve a
    # canned cache entry so the tools' success-path shapes are what we assert.
    entry = {
        "law_id": "922-19",
        "title": "ЗУ Про публічні закупівлі",
        "url": "https://zakon.rada.gov.ua/laws/show/922-19",
        "text": "Стаття 1. Визначення. договір про закупівлю.\n\nСтаття 2. Сфера. договір.",
        "articles": {
            "1": "Стаття 1. Визначення. договір про закупівлю.",
            "2": "Стаття 2. Сфера. договір.",
        },
        "source": "print_url",
        "from_cache": True,
        "char_count": 70,
        "adapter": "zakon_rada",
        "source_policy": "html_print",
        "content_hash": "abc",
    }
    monkeypatch.setattr(server.cache, "get_or_fetch", lambda *a, **k: dict(entry))
    monkeypatch.setattr(server.cache, "get_cached_entry", lambda *a, **k: dict(entry))
    monkeypatch.setattr(
        server.cache,
        "get_status",
        lambda *a, **k: {
            "cached": True,
            "fresh": True,
            "char_count": 70,
            "source": "print_url",
            "cached_at": "2026-05-12T00:00:00+00:00",
        },
    )

    list_result = server.list_laws()
    resolve_result = server.resolve_law_id("UA", "922-19")
    article_result = server.get_article("UA", "922-19", "1")
    query_result = server.query_law("UA", "922-19", "договір", tokens=200)
    search_result = server.search_across_laws("UA", "договір", max_results=1)
    discover_result = server.discover_laws("922", max_results=1)
    multiple_result = server.get_multiple_articles("UA", "922-19", ["1"])
    metadata_result = server.get_law_metadata("UA", "922-19")

    ListLawsOutput.model_validate(list_result)
    ResolveLawOutput.model_validate(resolve_result)
    GetArticleOutput.model_validate(article_result)
    QueryLawOutput.model_validate(query_result)
    SearchAcrossLawsOutput.model_validate(search_result)
    DiscoverLawsOutput.model_validate(discover_result)
    GetMultipleArticlesOutput.model_validate(multiple_result)
    LawMetadataOutput.model_validate(metadata_result)


def test_metrics_payload_reports_backend_fields(monkeypatch) -> None:
    import server

    # `health()` адаптера Ради пробує `zakon.rada.gov.ua` справжнім HEAD-запитом;
    # тут перевіряється форма звіту, а не доступність джерела (тікет 50).
    monkeypatch.setattr(server.source_adapter, "_check_connectivity", lambda: False)
    payload = server._metrics_payload()
    assert payload["backend"] in {"file", "postgres"}
    assert payload["pg_backend"] in {
        "disabled",
        "no_database_url",
        "init_failed",
        "active",
        "blocked_by_profile",
    }
    assert payload["version"] == server.__version__
    assert "cache" in payload and "tools" in payload


def test_search_articles_file_backend_returns_friendly_error(monkeypatch) -> None:
    import server

    monkeypatch.setattr(server, "hybrid", None)
    result = server.search_articles("UA", "договір про закупівлю", max_results=3)
    assert result["code"] == ErrorCode.SOURCE_UNAVAILABLE.value
    assert result["details"]["backend"] == "file"


def test_search_articles_postgres_backend_validates(monkeypatch) -> None:
    import server
    from core.contracts import SearchArticlesOutput

    class FakeHybrid:
        def search_articles(self, query, document_id, max_results):
            return [
                {
                    "law_id": "435-15",
                    "title": "ЦКУ",
                    "url": "u",
                    "article_num": "625",
                    "snippet": "body",
                    "score": 0.9,
                    "rrf_score": 0.03,
                    "matched_by": ["fts", "vector"],
                }
            ][:max_results]

    monkeypatch.setattr(server, "hybrid", FakeHybrid())
    result = server.search_articles("UA", "прострочення", document_id="435-15", max_results=5)
    SearchArticlesOutput.model_validate(result)
    assert result["backend"] == "postgres"
    assert result["found"] == 1
    assert result["results"][0]["article_num"] == "625"


# ---------------------------------------------------------------------------
# T020 — параметризация правопорядком (quickstart.md, сценарий 1)
#
# Ломающее изменение R-01: правопорядок становится параметром каждого
# инструмента, а число инструментов при этом не растёт. Проверяется и то и
# другое — вторая половина важнее первой, потому что размножение инструментов
# по юрисдикциям и есть тот способ провалить принцип VII, который выглядит как
# добросовестная работа.
# ---------------------------------------------------------------------------

#: Инструменты, работающие с правом: правопорядок обязателен.
LEGAL_ORDER_REQUIRED = (
    "resolve_law_id",
    "query_law",
    "get_article",
    "get_multiple_articles",
    "search_across_laws",
    "search_articles",
    "get_law_metadata",
    "search_decisions",
    "get_decision",
    "verify_quote",
)

#: Инструменты по украинским реестрам: правопорядок — необязательный фильтр,
#: потому что они привязаны к украинским реестрам по определению.
LEGAL_ORDER_OPTIONAL = (
    "list_laws",
    "discover_laws",
    "discover_registries",
    "search_debtors",
    "search_tenders",
    "get_tender",
    "list_registries",
)

#: Инструменты, не привязанные к одному правопорядку: карта покрытия и публичная
#: карточка дела.
COORDINATOR_REGISTERED = (
    "list_coverage",
    "get_case",
)

#: Правопорядок добавляет значение перечисления, а не инструмент (принцип VII).
EXPECTED_TOOL_COUNT = 19


def _registered_tools() -> dict[str, object]:
    import server

    return server._list_registered_tools()


def test_tool_count_did_not_grow_with_the_new_legal_orders() -> None:
    """Принцип VII: добавление правопорядка добавляет значение перечисления."""
    names = set(_registered_tools())

    assert len(names) == EXPECTED_TOOL_COUNT, sorted(names)
    assert names == (
        set(LEGAL_ORDER_REQUIRED) | set(LEGAL_ORDER_OPTIONAL) | set(COORDINATOR_REGISTERED)
    )


@pytest.mark.parametrize("tool_name", LEGAL_ORDER_REQUIRED + LEGAL_ORDER_OPTIONAL)
def test_every_tool_accepts_legal_order(tool_name: str) -> None:
    import inspect

    import server

    signature = inspect.signature(getattr(server, tool_name))

    assert "legal_order" in signature.parameters, f"{tool_name} не принимает правопорядок"


@pytest.mark.parametrize("tool_name", LEGAL_ORDER_REQUIRED)
def test_law_tools_require_the_legal_order(tool_name: str) -> None:
    import inspect

    import server

    parameter = inspect.signature(getattr(server, tool_name)).parameters["legal_order"]

    assert (
        parameter.default is inspect.Parameter.empty
    ), f"{tool_name} работает с правом — правопорядок обязателен"


@pytest.mark.parametrize("tool_name", LEGAL_ORDER_OPTIONAL)
def test_registry_tools_take_the_legal_order_as_a_filter(tool_name: str) -> None:
    import inspect

    import server

    parameter = inspect.signature(getattr(server, tool_name)).parameters["legal_order"]

    assert (
        parameter.default is not inspect.Parameter.empty
    ), f"{tool_name} привязан к украинским реестрам — правопорядок только фильтр"


@pytest.mark.parametrize("tool_name", LEGAL_ORDER_REQUIRED)
def test_unknown_legal_order_is_a_typed_refusal_listing_the_known_codes(tool_name: str) -> None:
    import server
    from core.legal_orders import KNOWN_LEGAL_ORDER_CODES

    result = _call_with_legal_order(getattr(server, tool_name), "ZZ")

    assert result["code"] == "unknown_legal_order", tool_name
    assert result["details"]["received"] == "ZZ"
    assert set(result["details"]["known_codes"]) == set(KNOWN_LEGAL_ORDER_CODES)
    assert result["error"].strip()


def test_an_international_forum_is_accepted_on_a_par_with_a_state() -> None:
    """Наднациональные и международные форумы — допустимые значения (принцип VII)."""
    import server

    for code in (
        "EU",
        "ECHR",
        "ICJ",
        "UN",
        "UA",
    ):
        result = _call_with_legal_order(server.search_decisions, code)
        assert result.get("code") != "unknown_legal_order", code


def _call_with_legal_order(tool: Any, legal_order: str) -> dict[str, Any]:
    """Вызвать инструмент, подставив безобидные значения остальных параметров."""
    import inspect

    samples: dict[str, Any] = {
        "query": "тест",
        "document_id": "922-19",
        "path": "1",
        "paths": ["1"],
        "citation": "стаття 1",
        "quote": "текст цитати для звірки",
        "answer_text": "",
        "tender_id": "abc",
        "case_number": "",
        "name": "",
        "code": "",
        "category_filter": "",
        "max_results": 1,
        "tokens": 200,
        "force_refresh": False,
    }
    kwargs: dict[str, Any] = {"legal_order": legal_order}
    for name, parameter in inspect.signature(tool).parameters.items():
        if name == "legal_order":
            continue
        if parameter.default is inspect.Parameter.empty:
            kwargs[name] = samples[name]
    result: dict[str, Any] = tool(**kwargs)
    return result


def test_the_known_codes_are_the_five_of_the_registry() -> None:
    from core.legal_orders import KNOWN_LEGAL_ORDER_CODES

    assert set(KNOWN_LEGAL_ORDER_CODES) == {
        "UA",
        "EU",
        "ECHR",
        "ICJ",
        "UN",
    }


# ---------------------------------------------------------------------------
# T016 — ответ с текстом нормы обязан нести конверт происхождения
# ---------------------------------------------------------------------------


def test_a_legal_text_answer_without_a_provenance_envelope_fails_validation() -> None:
    """`contracts/README.md`: это проверяется тестом, а не намерением."""
    from pydantic import ValidationError

    from core.contracts import GetArticleOutput

    without_envelope = {
        "legal_order": "UA",
        "document_id": "922-19",
        "title": "Про публічні закупівлі",
        "path": "1",
        "text": "Стаття 1. Визначення.",
        "url": "https://zakon.rada.gov.ua/laws/show/922-19",
        "from_cache": True,
        "retrieved_at": "2026-09-04T00:00:00+00:00",
    }

    with pytest.raises(ValidationError):
        GetArticleOutput.model_validate(without_envelope)


def test_the_ukrainian_reading_tools_do_carry_the_envelope(monkeypatch) -> None:
    import server
    from core.provenance import ENVELOPE_FIELDS

    entry = {
        "law_id": "922-19",
        "title": "ЗУ Про публічні закупівлі",
        "url": "https://zakon.rada.gov.ua/laws/show/922-19",
        "text": "Стаття 1. Визначення. договір про закупівлю.",
        "articles": {"1": "Стаття 1. Визначення. договір про закупівлю."},
        "source": "print_url",
        "from_cache": True,
    }
    monkeypatch.setattr(server.cache, "get_or_fetch", lambda *a, **k: dict(entry))

    for result in (
        server.get_article("UA", "922-19", "1"),
        server.query_law("UA", "922-19", "договір", tokens=200),
    ):
        missing = ENVELOPE_FIELDS - set(result)
        assert not missing, f"конверт неполон, нет полей: {sorted(missing)}"

    # ``get_multiple_articles`` — обёртка над несколькими прочитанными
    # единицами, и текста на верхнем уровне у неё нет вовсе. Реквизиты
    # фрагмента оттуда сняты (D7, проба 2026-09-09): ответ, где рядом стояли
    # ``evidence_id: null`` и ``content_hash`` первой статьи, читался как хеш
    # всего ответа. Провенанс спрашивается там, где лежит текст, — с каждого
    # фрагмента, и ``envelope_of`` называет, из чьего ответа собран конверт.
    envelope = server.get_multiple_articles("UA", "922-19", ["1"])
    assert envelope["evidence_id"] is None and envelope["confirmable"] is False
    assert envelope["envelope_of"] == "1"
    for absent in ("content_hash", "unit_id", "citation_format"):
        assert absent not in envelope, f"реквизит фрагмента остался в конверте: {absent}"
    document_level = ENVELOPE_FIELDS - {"content_hash", "citation_format"}
    missing = document_level - set(envelope)
    assert not missing, f"конверт документа неполон, нет полей: {sorted(missing)}"
    fragment = envelope["fragments"]["1"]
    assert fragment["evidence_id"] and fragment["content_hash"]


def test_search_snippets_are_navigation_and_never_evidence(monkeypatch, hot_cache: Path) -> None:
    """Сниппет поиска — адрес для следующего чтения, а не доказательство (T181).

    Сниппет собран нами и обрезан по длине; процитированный как норма, он
    выдаёт за текст акта то, чего источник в таком виде не отдавал.
    """
    import server

    _write_cached_law(hot_cache, MEDIA_LAW)

    monkeypatch.setattr(server.registry, "find_by_alias", lambda query: [])
    monkeypatch.setattr(server.open_data_discovery, "search", lambda *a, **k: {"results": []})
    monkeypatch.setattr(
        server.registry,
        "list_laws",
        lambda: [
            {
                "id": "2849-20",
                "title": "Про медіа",
                "url": "https://zakon.rada.gov.ua/laws/show/2849-20",
            }
        ],
    )
    monkeypatch.setattr(
        server.cache,
        "get_cached_entry",
        lambda *args, **kwargs: {
            "law_id": "2849-20",
            "title": "Про медіа",
            "text": "Стаття 1. Медіа регулюються цим Законом.",
            "articles": {"1": "Стаття 1. Медіа регулюються цим Законом."},
        },
    )

    result = server.search_across_laws("UA", "медіа", max_results=1)

    assert result["navigation_only"] is True
    assert result["navigation_notice"]
    assert result["results"], result
    for item in result["results"]:
        assert item["navigation_only"] is True
        assert item["evidence_id"] is None
        assert item["confirmable"] is False


def test_the_package_version_matches_the_server_version() -> None:
    """Одна версия продукта, а не две (T184).

    `pyproject.toml` объявляет версию пакета, `server.py` отдаёт её в каждом
    ответе и в `/metrics`. Разошедшись, они делают отчёт о выпуске
    недоказуемым: непонятно, какая из двух установлена у юриста.
    """
    import re

    import server

    pyproject = (Path(__file__).parent.parent / "pyproject.toml").read_text(encoding="utf-8")
    declared = re.search(r'^version = "([^"]+)"', pyproject, re.MULTILINE)

    assert declared is not None, "pyproject.toml без версии пакета"
    assert declared.group(1) == server.__version__


# ---------------------------------------------------------------------------
# T186 — схемы задачи и аттестации (контракт 3.0.0, data-model.md)
# ---------------------------------------------------------------------------


def test_a_claim_about_a_norm_cannot_exist_without_evidence() -> None:
    """Ссылку печатает сервер из доказательства, а не модель из памяти."""
    from pydantic import ValidationError

    from core.contracts import UNSUPPORTED, Claim

    with pytest.raises(ValidationError):
        Claim(claim_id="c1", text="Стаття 5 забороняє", kind="norm")

    with pytest.raises(ValidationError):
        Claim(claim_id="c2", text="Суд встановив", kind="fact_from_case")

    supported = Claim(claim_id="c3", text="Стаття 5 забороняє", kind="norm", evidence_ids=["ev-1"])
    assert supported.support == "evidence"
    assert Claim(claim_id="c4", text="Звідси випливає", kind="inference").support == UNSUPPORTED
    fact = Claim(
        claim_id="c5",
        text="Суд встановив",
        kind="fact_from_case",
        case_ref={"file": "ruling.pdf", "locator": "п. 12"},
    )
    assert fact.support == "case_ref"


# ---------------------------------------------------------------------------
# T232 — applied_filters говорит о применённом, а не об запрошенном
# ---------------------------------------------------------------------------


class _FakeParams:
    """Ровно те поля входа, которые читает ``server._applied_filters``."""

    def __init__(self) -> None:
        self.case_number = "12345/06"
        self.cites = ""
        self.date_from = "2025-04-02"
        self.date_to = "2025-04-02"
        self.query = ""


def test_applied_filters_repeat_what_the_adapter_declared() -> None:
    """Адаптер объявил применённое — инструмент передаёт его объявление."""
    import server

    declared = {"case_number": "12345/06", "date_from": None, "date_to": None, "free_text": False}
    result = server._applied_filters({"applied_filters": declared}, _FakeParams())

    assert result["declared_by_adapter"] is True
    assert result["case_number"] == "12345/06"
    # Даты в запросе были, но адаптер их источнику не отправлял — и поле
    # не утверждает обратного.
    assert result["date_from"] is None


def test_an_undeclared_filter_is_not_reported_as_applied() -> None:
    """Адаптер промолчал — поле называет запрошенное запрошенным (принцип III)."""
    import server

    result = server._applied_filters({}, _FakeParams())

    assert result["declared_by_adapter"] is False
    assert "date_from" not in result, "необъявленный фильтр не выдаётся за применённый"
    assert result["requested"]["date_from"] == "2025-04-02"
    assert result["notice"]


# ---------------------------------------------------------------------------
# T279/T280 — контракт печати 4.0.0 (`contracts/render-v4.md`)
#
# Ломающее изменение внесено намеренно и сейчас: принцип VII — «до появления
# внешних потребителей, пока их цена минимальна». Восемь изменений §0, и каждое
# держится здесь тестом, а не обещанием.
# ---------------------------------------------------------------------------


def test_the_print_contract_is_the_breaking_four() -> None:
    """Первая цифра, а не вторая: у пяти инструментов изменился вход."""
    assert CONTRACT_VERSION.split(".")[0] == "4"


def test_a_claim_carries_no_quote_of_its_own() -> None:
    """Цитата подаётся один раз — в `attest_claim` (render-v4 §0, строка 3).

    Две копии одной цитаты расходятся, и расхождение обнаруживается отказом
    `quoted_text_absent` уже на печати, когда документ ждут.
    """
    from pydantic import ValidationError

    from core.contracts import Claim

    assert "quote" not in Claim.model_fields

    with pytest.raises(ValidationError):
        Claim(
            claim_id="c1",
            text="Стаття 5 забороняє",
            kind="norm",
            evidence_ids=["ev-1"],
            quote="Article 5",
        )


def test_a_norm_longer_than_the_limit_is_quoted_in_segments() -> None:
    """Норма длиннее предела больше не остаётся нецитированной (FR-526).

    Обход был измерен: норму резали на два утверждения, чтобы каждая половина
    влезла в `Claim.quote`. Сегменты снимают причину, а не запрещают следствие.
    """
    from pydantic import ValidationError

    from core.contracts import QUOTE_MAX_CHARS, Claim

    segments = ("А" * QUOTE_MAX_CHARS, "Б" * 120)
    claim = Claim(
        claim_id="c1",
        text="Стаття 5 забороняє",
        kind="norm",
        evidence_ids=["ev-1"],
        quote_segments=segments,
    )

    assert claim.quote_segments == segments
    assert len(claim.quoted_text) == QUOTE_MAX_CHARS + 120

    with pytest.raises(ValidationError):
        Claim(
            claim_id="c2",
            text="Стаття 5 забороняє",
            kind="norm",
            evidence_ids=["ev-1"],
            quote_segments=["В" * (QUOTE_MAX_CHARS + 1)],
        )


def test_the_skeleton_names_the_task_once() -> None:
    """`skeleton.task_id` изъят (render-v4 §0, строка 1).

    Два поля одного смысла в одном вызове давали расхождение «task_id скелета ≠
    task_id задачи», которое ядро принимало молча.
    """
    from pydantic import ValidationError

    from core.contracts import ArgumentSkeleton, Section

    assert "task_id" not in ArgumentSkeleton.model_fields
    assert set(ArgumentSkeleton.model_fields) == {"sections"}

    with pytest.raises(ValidationError):
        ArgumentSkeleton(task_id="task-1", sections=[])

    # Назначение раздела тоже изъято: заголовок берёт каталог по `section_id`.
    assert "purpose" not in Section.model_fields
    with pytest.raises(ValidationError):
        Section(section_id="s1", purpose="Підстави")


def test_a_fact_of_the_case_names_the_document_and_where_to_read_it() -> None:
    """Голое имя файла больше не принимается (FR-511, render-v4 §1)."""
    from pydantic import ValidationError

    from core.contracts import Claim

    with pytest.raises(ValidationError):
        Claim(claim_id="c1", text="Суд встановив", kind="fact_from_case", case_ref="ruling.pdf")

    claim = Claim(
        claim_id="c1",
        text="Суд встановив",
        kind="fact_from_case",
        case_ref={"file": "ruling.pdf", "locator": "п. 12", "question_id": "q1"},
    )

    assert claim.case_ref is not None
    assert claim.case_ref.file == "ruling.pdf"
    assert claim.case_ref.question_id == "q1"
    assert claim.locator_refusal() is None


def test_a_fact_without_a_locator_is_a_refusal_that_names_the_action() -> None:
    """Отказ, а не исключение валидации: он адресован юристу (принцип III)."""
    from core.contracts import Claim

    claim = Claim(
        claim_id="c1",
        text="Суд встановив",
        kind="fact_from_case",
        case_ref={"file": "ruling.pdf"},
    )
    refusal = claim.locator_refusal()

    assert refusal is not None
    assert refusal.failure_code.value == "case_ref_without_locator"
    assert refusal.as_output()["details"]["file"] == "ruling.pdf"
    assert refusal.manual_path.strip()


def test_the_kind_of_document_is_chosen_by_the_lawyer_never_defaulted() -> None:
    """Пустой вид больше не означает «вид по умолчанию» (render-v4 §1)."""
    from pydantic import ValidationError

    from core.contracts import RenderAttestedInput

    with pytest.raises(ValidationError):
        RenderAttestedInput(task_id="task-1", kind="", skeleton={"sections": []})


#: Отказы контракта печати: восемь новых и три прежних, которые 4.0.0 привёл к
#: тому же правилу. Строится минимальный экземпляр каждого — рецепт обязан
#: получиться без единого явно переданного ``manual_path``.
PRINT_REFUSALS: tuple[tuple[str, dict[str, Any]], ...] = (
    ("unknown_section", {"section_id": "s3", "kind": "written_observations"}),
    ("missing_required_section", {"section_id": "s1", "kind": "written_observations"}),
    ("case_ref_without_locator", {"claim_id": "c1", "file": "ruling.pdf"}),
    ("output_dir_unavailable", {"reason": "каталог не існує"}),
    ("proceeding_language_missing", {"kind": "written_observations"}),
    ("export_forbidden_for_kind", {"kind": "stress_test_report"}),
    ("mark_in_prose", {"mark": "[ФАКТ ІЗ МАТЕРІАЛІВ СПРАВИ]", "claim_id": "c1"}),
    ("revision_marker_in_filename", {"filename": "01-position_REV3.md"}),
    ("unbound_citation_in_prose", {"citation": "стаття 5 Регламенту 2016/679"}),
    ("unattested_norm_claim", {"claim_id": "c1", "kind": "norm"}),
    ("unknown_document_kind", {"kind": "судове рішення", "known_kinds": ("memo",)}),
)


def test_source_unavailable_names_a_manual_path_that_exists() -> None:
    """Тікет 15: рецепт відмови більше не посилається на скрипт плагіна.

    ``plugin/yurko/scripts/read_case_file.py`` прибрано разом із плагіном, тож
    відмова називала дію, якої юрист виконати не може, — а це гірше за мовчання:
    він витрачає час на пошук неіснуючого файла. Рецепт лишається дією, а не
    самим лише часом наступної спроби (FR-537).
    """
    from core.contracts import SourceUnavailable

    recipe = SourceUnavailable(source="eur-lex", retry_after="через 10 хвилин").recipe()

    assert "plugin/" not in recipe
    assert "read_case_file" not in recipe
    # Дія лишається названою: власний браузер і тека справи.
    assert "браузер" in recipe
    assert "теку справи" in recipe
    # Незалежні питання йдуть далі, і час наступної спроби названо.
    assert "не залежать" in recipe
    assert "через 10 хвилин" in recipe


def test_no_tool_description_promises_to_register_evidence() -> None:
    """Тікет 15: опис інструмента каже, що він повертає, а не що засвідчує.

    Шару атестації в ядрі немає — воно дає доступ до реєстрів. «Register it as
    evidence» у ``get_article`` обіцяло процес, якого сервер не веде; те, що
    справді повертається, — текст одиниці, її локатор, конверт джерела і
    ``evidence_id`` сесійного кешу текстів.

    Пряме заперечення обіцянки («nothing is registered as evidence» у
    ``verify_quote`` — тікет 17) — це чесний опис, а не порушення: перевіряємо
    по реченнях і пропускаємо ті, де обіцянку явно спростовано.
    """
    import server

    negations = ("nothing is registered", "not registered", "isn't registered")
    promises = ("as evidence", "attest", "attested", "proves the quote")
    for name, tool in server._list_registered_tools().items():
        description = str(getattr(tool, "description", "") or "").lower()
        for sentence in re.split(r"(?<=[.!?])\s+", description):
            for promise in promises:
                if promise not in sentence:
                    continue
                if promise == "as evidence" and any(n in sentence for n in negations):
                    continue
                pytest.fail(f"{name}: опис обіцяє «{promise}» («{sentence.strip()}»)")

    article = server._list_registered_tools()["get_article"]
    assert "evidence_id" in str(getattr(article, "description", ""))
