"""T057/T132–T136 — інтеграційний тест адаптера права ЄС на мокнутому HTTP.

Перевіряється: розбір статті/частини/підпункту з XHTML Cellar (не Akoma
Ntoso — Cellar відповідає на ``Accept: application/akn+xml`` кодом 400,
research/13-runtime-probes-2026-09-07.md), резолвер редакції на дату,
непокрита мова, `partial_result` на переліміченому пошуку (10 000 записів),
`source_unavailable` на недоступність джерела. Мережа тут не
використовується жодного разу — усе через підмінену сесію.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
import requests

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from core.contracts import (  # noqa: E402
    NotCovered,
    NotFound,
    PartialResult,
    RevisionUnknown,
    SourceUnavailable,
    UpstreamStubDetected,
)
from core.provenance import ContentKind, PublicationKind  # noqa: E402
from sources.base import AdapterPayload  # noqa: E402
from sources.eu_law import (  # noqa: E402
    EuLawAdapter,
    eli_from_celex,
    extract_akn_unit,
    extract_xhtml_unit,
)

AKN_REGULATION = """<?xml version="1.0" encoding="UTF-8"?>
<akomaNtoso xmlns="http://docs.oasis-open.org/legaldocml/ns/akn/3.0">
  <act>
    <body>
      <article eId="art_1">
        <num>Article 1</num>
        <content>
          <p>For the purposes of this Regulation, the following definitions apply.</p>
        </content>
      </article>
    </body>
  </act>
</akomaNtoso>
"""

#: Базовий акт OJ — згорнутий, але за формою справжній фрагмент розмітки
#: Cellar (research/13, §A1, дослівно для article 2; article 5 — та сама
#: форма `div#NNN.MMM` + таблиця з коміркою «(a)»/«(b)»).
BASE_ACT_XHTML = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><body>
<div class="eli-container">
<div class="eli-subdivision" id="art_1">
<p class="oj-ti-art">Article 1</p>
<p class="oj-normal">For the purposes of this Regulation, the following definitions
apply.</p>
</div>
<div class="eli-subdivision" id="art_2">
<p id="d1e300-6-1" class="oj-ti-art">Article 2</p>
<div id="002.001"><p class="oj-normal">1.   All operators placing the products listed
in Annex I on the market shall keep a register of their suppliers, and that register
shall be registered with the competent authorities.</p></div>
<div id="002.002"><p class="oj-normal">2.   No product listed in Annex I shall be made
available on the market by an operator who does not keep such a register.</p></div>
</div>
<div class="eli-subdivision" id="art_5">
<p id="d1e370-6-1" class="oj-ti-art">Article 5</p>
<div id="005.001">
<p class="oj-normal">1.   By way of derogation from Article 2, the competent
authorities of the Member States may exempt certain small operators, if the
following conditions are met:</p>
<table><tbody><tr><td><p class="oj-normal">(a)</p></td>
<td><p class="oj-normal">the operator holds an inspection report issued before the
product was entered in Annex I;</p>
</td></tr></tbody></table>
<table><tbody><tr><td><p class="oj-normal">(b)</p></td>
<td><p class="oj-normal">the products will be supplied exclusively to the operators
named in that report;</p></td></tr></tbody></table>
</div>
</div>
<div class="eli-container" id="anx_II">
<p class="oj-doc-ti">ANNEX II</p>
<p class="oj-normal">Websites for information on the competent authorities and address
for notification to the European Commission</p>
<p class="oj-normal">MEMBER STATE A https://authority.example.invalid/registers</p>
</div>
</div>
</body></html>
"""

#: Консолідація — art_5a за формою research/13, §A3 (дослівно).
CONSOLIDATED_XHTML = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><body>
<div class="eli-container">
<div class="eli-subdivision" id="art_5a">
  <p class="title-article-norm" id="id-090b86ea-3ed2-483f-83e7-6c53fea821de">Article 5a</p>
  <div class="norm"><span class="no-parag">1.  </span>
    <div class="norm inline-element">By way of derogation from Article 2, the
    competent authorities of a Member State may grant an operator a temporary
    exemption, after having determined that a judicial or administrative authority
    of a Member State has ordered the withdrawal of the operator's products from the
    market in the public interest, provided that the withdrawn products are kept in
    storage.</div></div>
  <div class="norm"><span class="no-parag">2.  </span><div class="norm inline-element">
    The Member State concerned shall inform the other Member States and the
    Commission of any exemption granted under paragraph 1.</div></div>
</div>
<div id="anx_II">
<p>ANNEX II</p>
<p>Websites for information on the competent authorities and address for notification
to the European Commission</p>
<p>MEMBER STATE A https://authority.example.invalid/registers</p>
</div>
</div>
</body></html>
"""


class _FakeResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        text: str = "",
        json_data: dict[str, Any] | None = None,
    ) -> None:
        self.status_code = status_code
        self.text = text
        self._json_data = json_data

    def json(self) -> dict[str, Any]:
        if self._json_data is None:
            raise ValueError("no json body")
        return self._json_data


class _FakeSession:
    """Records the calls it receives and answers from a scripted queue."""

    def __init__(self, responses: list[_FakeResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, **kwargs: Any) -> _FakeResponse:
        self.calls.append({"url": url, **kwargs})
        if not self._responses:
            raise AssertionError("no more scripted responses")
        return self._responses.pop(0)


class _RaisingSession:
    def get(self, *args: Any, **kwargs: Any) -> _FakeResponse:
        raise requests.ConnectionError("boom")


def _no_consolidations() -> _FakeResponse:
    """SPARQL-перелік консолідацій порожній: акт у Cellar не консолідовано."""
    return _FakeResponse(status_code=200, json_data={"results": {"bindings": []}})


def _base_act_session(body: str) -> _FakeSession:
    """Без ``as_of`` читач спершу шукає чинну консолідацію (15.09.2026).

    Тут її немає, тож читається сам CELEX — базовий акт OJ, чию розмітку ці
    тести й перевіряють.
    """
    return _FakeSession([_no_consolidations(), _FakeResponse(status_code=200, text=body)])


# -- читання: базовий акт OJ --------------------------------------------------


def test_fetch_extracts_article_from_base_act_xhtml() -> None:
    session = _base_act_session(BASE_ACT_XHTML)
    adapter = EuLawAdapter(session=session)  # type: ignore[arg-type]

    result = adapter.fetch("32014R8888", path="2")

    assert isinstance(result, AdapterPayload)
    assert "register" in result.data["text"]
    assert result.data["celex"] == "32014R8888"
    assert result.data["locator"] == "2"
    assert result.data["unit_id"] == "art_2"
    assert result.data["eli"] == "http://data.europa.eu/eli/reg/2014/8888/oj"
    assert result.provenance.source_channel.value == "live"
    assert result.provenance.publication_kind is PublicationKind.OFFICIAL_JOURNAL
    assert result.provenance.content_kind is ContentKind.FRAGMENT
    assert result.provenance.attribution is not None
    assert "2011/833/EU" in result.provenance.attribution
    assert "2011/833/EU" in result.provenance.notice


def test_fetch_full_text_without_path_is_full_text_kind() -> None:
    session = _base_act_session(BASE_ACT_XHTML)
    adapter = EuLawAdapter(session=session)  # type: ignore[arg-type]

    result = adapter.fetch("32014R8888")

    assert isinstance(result, AdapterPayload)
    assert result.provenance.content_kind is ContentKind.FULL_TEXT
    assert "Article 1" in result.data["text"] and "Article 5" in result.data["text"]


def test_fetch_article_5_1_a_reads_subpoint_from_table() -> None:
    session = _base_act_session(BASE_ACT_XHTML)
    adapter = EuLawAdapter(session=session)  # type: ignore[arg-type]

    result = adapter.fetch("32014R8888", path="5(1)(a)")

    assert isinstance(result, AdapterPayload)
    assert "inspection report" in result.data["text"]
    assert result.data["locator"] == "5(1)(a)"
    assert result.data["unit_id"] == "005.001(a)"


def test_fetch_unknown_article_is_not_found() -> None:
    session = _base_act_session(BASE_ACT_XHTML)
    adapter = EuLawAdapter(session=session)  # type: ignore[arg-type]

    result = adapter.fetch("32014R8888", path="99")

    assert isinstance(result, NotFound)
    assert result.failure_code.value == "not_found"


def test_fetch_missing_document_is_not_found() -> None:
    session = _FakeSession([_FakeResponse(status_code=404, text="")])
    adapter = EuLawAdapter(session=session)  # type: ignore[arg-type]

    result = adapter.fetch("99999X9999")

    assert isinstance(result, NotFound)


def test_fetch_supports_legacy_article_kwarg() -> None:
    """Зворотна сумісність із ``article=`` (tests/e2e/test_eu_law_live.py)."""
    session = _base_act_session(BASE_ACT_XHTML)
    adapter = EuLawAdapter(session=session)  # type: ignore[arg-type]

    result = adapter.fetch("32014R8888", article="1")

    assert isinstance(result, AdapterPayload)
    assert "definitions apply" in result.data["text"]


# -- читання: консолідація, «5a» відрізняється від «5(1)(a)» -----------------


def test_fetch_consolidated_article_5a() -> None:
    session = _FakeSession([_FakeResponse(status_code=200, text=CONSOLIDATED_XHTML)])
    adapter = EuLawAdapter(session=session)  # type: ignore[arg-type]

    result = adapter.fetch("02014R8888-20231219", path="5a")

    assert isinstance(result, AdapterPayload)
    assert "Article 5a" in result.data["text"]
    assert result.data["locator"] == "5a"
    assert result.data["unit_id"] == "art_5a"
    assert result.provenance.publication_kind is PublicationKind.CONSOLIDATED
    assert result.provenance.version_id == "02014R8888-20231219"


@pytest.mark.parametrize("locator", ["5a", "5а", "5A"])
def test_5a_locator_variants_are_equivalent(locator: str) -> None:
    """Кирилична «а» й регістр не міняють, яка одиниця знайдена (T134)."""
    session = _FakeSession([_FakeResponse(status_code=200, text=CONSOLIDATED_XHTML)])
    adapter = EuLawAdapter(session=session)  # type: ignore[arg-type]

    result = adapter.fetch("02014R8888-20231219", path=locator)

    assert isinstance(result, AdapterPayload)
    assert result.data["locator"] == "5a"
    assert result.data["unit_id"] == "art_5a"


def test_extract_xhtml_unit_5a_and_5_1_a_are_different_functions_of_document() -> None:
    """Прямий виклик розбору (без HTTP): «5a» відсутня в базовому акті."""
    from sources.eu_law import _parse_xhtml

    base_root = _parse_xhtml(BASE_ACT_XHTML)
    assert extract_xhtml_unit(base_root, "5a") is None
    unit_511a = extract_xhtml_unit(base_root, "5(1)(a)")
    assert unit_511a is not None
    assert "inspection report" in unit_511a[0]

    consolidated_root = _parse_xhtml(CONSOLIDATED_XHTML)
    unit_5a = extract_xhtml_unit(consolidated_root, "5a")
    assert unit_5a is not None
    assert unit_5a[1] == "art_5a"


@pytest.mark.parametrize("locator", ["Annex II", "ANNEX II", "II", "annex-2", "Додаток II"])
def test_annex_locator_variants_read_the_same_annex(locator: str) -> None:
    """Додаток адресується локатором, і всі природні його форми — одна адреса.

    До 2026-09-09 жодна з них не розв'язувалася: розбірник знав лише статті, а
    акти часто відсилають до додатка зі статті. Різні форми зводяться до одного
    локатора, інакше той самий додаток ліг би в пам'ять доказів двічі під різними
    адресами.
    """
    session = _base_act_session(BASE_ACT_XHTML)
    adapter = EuLawAdapter(session=session)  # type: ignore[arg-type]

    result = adapter.fetch("32014R8888", path=locator)

    assert isinstance(result, AdapterPayload)
    assert result.data["locator"] == "Annex II"
    assert result.data["unit_id"] == "anx_II"
    assert "competent authorities" in result.data["text"]


def test_annex_is_read_in_consolidation_where_the_div_has_no_class() -> None:
    """Виміряно 2026-09-09: у консолідації ``anx_II`` — ``div`` без класу зовсім.

    Прив'язка до класу зробила б додаток читаним у базовому акті й не читаним у
    редакції на дату, тобто саме там, де юрист його й шукає.
    """
    session = _FakeSession([_FakeResponse(status_code=200, text=CONSOLIDATED_XHTML)])
    adapter = EuLawAdapter(session=session)  # type: ignore[arg-type]

    result = adapter.fetch("02014R8888-20231219", path="Annex II")

    assert isinstance(result, AdapterPayload)
    assert result.data["unit_id"] == "anx_II"


def test_bare_arabic_number_stays_an_article_and_not_an_annex() -> None:
    """«2» — це стаття 2, а не додаток II: тлумачення надвоє віддало б не той текст."""
    from sources.eu_law import _parse_xhtml

    root = _parse_xhtml(BASE_ACT_XHTML)
    unit = extract_xhtml_unit(root, "2")

    assert unit is not None
    assert unit[1] == "art_2"


# -- мова: 24 автентичні мови ЄС, українська не покрита -----------------------


def test_fetch_unsupported_language_is_not_covered() -> None:
    session = _FakeSession([])  # мережа не викликається — відмова до HTTP
    adapter = EuLawAdapter(session=session)  # type: ignore[arg-type]

    result = adapter.fetch("32014R8888", path="2", language="uk")

    assert isinstance(result, NotCovered)
    assert result.operation == "read_document"
    assert session.calls == []


def test_fetch_authentic_language_sets_accept_language_header() -> None:
    session = _base_act_session(BASE_ACT_XHTML)
    adapter = EuLawAdapter(session=session)  # type: ignore[arg-type]

    result = adapter.fetch("32014R8888", path="2", language="de")

    assert isinstance(result, AdapterPayload)
    assert session.calls[-1]["headers"]["Accept-Language"] == "deu"
    assert result.provenance.language == "de"
    assert result.provenance.is_authentic_version is True


# -- заглушка: не блокує успішну структурну знахідку --------------------------


def test_fetch_rejects_antibot_stub_when_locator_not_found() -> None:
    session = _base_act_session("Please verify you are a human before continuing." * 3)
    adapter = EuLawAdapter(session=session)  # type: ignore[arg-type]

    result = adapter.fetch("32014R8888", path="1")

    assert isinstance(result, UpstreamStubDetected)
    assert result.failure_code.value == "upstream_stub_detected"


def test_fetch_real_toc_heading_does_not_block_found_fragment() -> None:
    """Документ із самим лише «Table of Contents» у заголовку — не заглушка,

    якщо запитана стаття структурно знайдена (research/13, §A5: Регламент
    Суду має справжній розділ Table of Contents на початку).
    """
    body = (
        "<html><body><p>Table of Contents</p>"
        '<div class="eli-subdivision" id="art_1">'
        '<p class="oj-ti-art">Article 1</p>'
        '<p class="oj-normal">Real substantive text of article one.</p></div>'
        "</body></html>"
    )
    session = _FakeSession([_FakeResponse(status_code=200, text=body)])
    adapter = EuLawAdapter(session=session)  # type: ignore[arg-type]

    result = adapter.fetch("32012Q0929(01)", path="1")

    assert isinstance(result, AdapterPayload)
    assert "Real substantive text" in result.data["text"]


# -- редакція на дату: недоступність джерела під час SPARQL ------------------


def test_fetch_as_of_source_unavailable_on_sparql_network_error() -> None:
    adapter = EuLawAdapter(session=_RaisingSession())  # type: ignore[arg-type]

    result = adapter.fetch("32014R8888", as_of="2024-01-01", path="2")

    assert isinstance(result, SourceUnavailable)


def test_resolve_revision_no_consolidations_is_revision_unknown() -> None:
    session = _FakeSession(
        [_FakeResponse(status_code=200, json_data={"results": {"bindings": []}})]
    )
    adapter = EuLawAdapter(session=session)  # type: ignore[arg-type]

    result = adapter.resolve_revision("32014R8888", "2024-01-01")

    assert isinstance(result, RevisionUnknown)


# -- пошук, недоступність, здоров'я, політика (без змін логіки) --------------


def test_search_over_limit_is_partial_result() -> None:
    bindings = [
        {
            "celex": {"value": f"3201{i % 10}R0{i:03d}"},
            "title": {"value": "x"},
            "work": {"value": "w"},
        }
        for i in range(10_001)
    ]
    session = _FakeSession(
        [_FakeResponse(status_code=200, json_data={"results": {"bindings": bindings}})]
    )
    adapter = EuLawAdapter(session=session)  # type: ignore[arg-type]

    result = adapter.search("product registers")

    assert isinstance(result, PartialResult)
    assert result.received == 10_001
    assert "10000" in result.limited_by or "10 000" in result.limited_by


def test_search_within_limit_returns_results() -> None:
    bindings = [
        {
            "celex": {"value": "32014R8888"},
            "title": {"value": "Regulation 8888/2014"},
            "work": {"value": "w1"},
        },
    ]
    session = _FakeSession(
        [_FakeResponse(status_code=200, json_data={"results": {"bindings": bindings}})]
    )
    adapter = EuLawAdapter(session=session)  # type: ignore[arg-type]

    result = adapter.search("8888/2014")

    assert isinstance(result, AdapterPayload)
    assert result.data["found"] == 1
    assert result.data["results"][0]["celex"] == "32014R8888"


def test_fetch_source_unavailable_on_network_error() -> None:
    adapter = EuLawAdapter(session=_RaisingSession())  # type: ignore[arg-type]

    result = adapter.fetch("32014R8888", path="1")

    assert isinstance(result, SourceUnavailable)
    assert result.source == "eu_law_eurlex_cellar"


def test_fetch_server_error_is_source_unavailable() -> None:
    session = _FakeSession([_FakeResponse(status_code=503, text="")])
    adapter = EuLawAdapter(session=session)  # type: ignore[arg-type]

    result = adapter.fetch("32014R8888", path="1")

    assert isinstance(result, SourceUnavailable)


class _DroppedOnceSession(_FakeSession):
    """Перший запит падає на обірваному з'єднанні, далі — сценарій."""

    def __init__(self, responses: list[_FakeResponse]) -> None:
        super().__init__(responses)
        self._dropped = False

    def get(self, url: str, **kwargs: Any) -> _FakeResponse:
        if not self._dropped:
            self._dropped = True
            self.calls.append({"url": url, **kwargs})
            raise requests.ConnectionError("Connection aborted: WinError 10054")
        return super().get(url, **kwargs)


def test_fetch_survives_a_connection_dropped_by_the_network() -> None:
    """Живий виклик 15.09.2026: ст. 5 FR — source_unavailable через обірване з'єднання."""
    session = _DroppedOnceSession(
        [_no_consolidations(), _FakeResponse(status_code=200, text=BASE_ACT_XHTML)]
    )
    adapter = EuLawAdapter(session=session)  # type: ignore[arg-type]

    result = adapter.fetch("32014R8888", path="2", language="FR")

    assert isinstance(result, AdapterPayload)
    assert "register" in result.data["text"]


def test_source_unavailable_names_its_cause() -> None:
    """Відмова без причини не дала 15.09.2026 відрізнити мережу від джерела."""
    network = EuLawAdapter(session=_RaisingSession()).fetch(  # type: ignore[arg-type]
        "32014R8888", path="1"
    )
    server = EuLawAdapter(
        # 503 — на читанні документа: збій SPARQL консолідацій читання не валить.
        session=_FakeSession(  # type: ignore[arg-type]
            [_no_consolidations(), _FakeResponse(status_code=503, text="")]
        )
    ).fetch("32014R8888", path="1")

    assert isinstance(network, SourceUnavailable)
    assert isinstance(server, SourceUnavailable)
    assert network.reason == "network_error:ConnectionError"
    assert server.reason == "http_503"


def test_revisions_lists_dated_consolidations() -> None:
    bindings = [
        {"celex": {"value": "02014R8888-20260423"}},
        {"celex": {"value": "02014R8888-20260807"}},
    ]
    session = _FakeSession(
        [_FakeResponse(status_code=200, json_data={"results": {"bindings": bindings}})]
    )
    adapter = EuLawAdapter(session=session)  # type: ignore[arg-type]

    result = adapter.revisions("32014R8888")

    assert isinstance(result, AdapterPayload)
    assert result.data["versions"][0]["date"] == "2026-04-23"
    assert result.data["known_through"] == "2026-08-07"


def test_health_reports_source_policy() -> None:
    session = _FakeSession([_FakeResponse(status_code=200, json_data={"boolean": True})])
    adapter = EuLawAdapter(session=session)  # type: ignore[arg-type]

    health = adapter.health()

    assert health.ok is True
    assert health.source_policy.value == "api"


def test_health_reports_failure_on_network_error() -> None:
    adapter = EuLawAdapter(session=_RaisingSession())  # type: ignore[arg-type]

    health = adapter.health()

    assert health.ok is False


def test_policy_declares_eu_cellar_source() -> None:
    adapter = EuLawAdapter()
    source = adapter.policy()

    assert source.id == "eu_law_eurlex_cellar"
    assert source.legal_order == "EU"
    assert source.attribution_required is True
    assert source.limits.max_records_per_request == 10_000


def test_eli_from_celex_maps_regulation() -> None:
    assert eli_from_celex("32014R8888") == "http://data.europa.eu/eli/reg/2014/8888/oj"


def test_eli_from_celex_unknown_shape_is_none() -> None:
    assert eli_from_celex("not-a-celex") is None


def test_extract_akn_unit_missing_returns_none() -> None:
    """Спільний розбір Akoma Ntoso для джерел, що публікують LegalDocML."""
    assert extract_akn_unit(AKN_REGULATION, "55") is None


def test_extract_akn_unit_reads_an_akoma_ntoso_document() -> None:
    assert extract_akn_unit(AKN_REGULATION, "1") is not None


@pytest.mark.parametrize("query", ["", "   "])
def test_fetch_blank_document_id_is_not_found(query: str) -> None:
    adapter = EuLawAdapter(session=_FakeSession([]))  # type: ignore[arg-type]

    result = adapter.fetch(query)

    assert isinstance(result, NotFound)
