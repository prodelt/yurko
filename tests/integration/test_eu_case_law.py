"""T060 — інтеграційний тест адаптера практики Суду ЄС на мокнутому HTTP.

CURIA офіційного API не має — шлях завжди через Cellar: SPARQL резолвить ECLI
у CELEX, REST повертає документ. Обидва кроки мокнуті.

Зразок розмітки взято з живого читання 62019CJ0487 (2026-09-07): рішення в
Cellar розмічене класами ``coj-*``, а пронумерований пункт лежить у таблиці
двома клітинками — ``p.coj-count`` з ``id="pointN"`` несе номер, текст стоїть
у сусідній клітинці. Akoma Ntoso тут не буває: на ``Accept: application/akn+xml``
Cellar відповідає 400.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
import requests

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from core.contracts import NotCovered, NotFound, SourceUnavailable  # noqa: E402
from core.provenance import ContentKind, PublicationKind  # noqa: E402
from sources.base import AdapterPayload  # noqa: E402
from sources.eu_case_law import (  # noqa: E402
    _CASE_NUMBER_RE,
    _case_number_year,
    build_cited_by_sparql,
    EuCaseLawAdapter,
    extract_judgment_paragraph,
    is_case_law_celex,
    is_valid_ecli,
)
from sources.eu_law import _parse_xhtml  # noqa: E402

JUDGMENT_TEXT = """<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><body>
  <div id="judgment">
    <p class="coj-title-grseq-2">JUDGMENT OF THE COURT (Grand Chamber)</p>
    <table><tr>
      <td><p class="coj-count" id="point44">44</p></td>
      <td><p class="coj-normal">An earlier paragraph about admissibility.</p></td>
    </tr></table>
    <table><tr>
      <td><p class="coj-count" id="point45">45</p></td>
      <td><p class="coj-normal">It follows that the referring court must assess
      proportionality having regard to the objectives pursued by the contested
      measure.</p></td>
    </tr></table>
  </div>
</body></html>
"""

#: Середня родина розмітки (за формою 62005CJ0438, 62000CJ0050): номер пункту —
#: провідні цифри самого абзаца, відділені нерозривними пробілами. Поряд навмисно
#: стоїть цитований перелік з тими самими цифрами й іншим класом: він не пункт.
MIDDLE_JUDGMENT_HTML = """<html><body>
  <P class="C75Debutdesmotifs"><B>Judgment</B></P>
  <P class="C01PointnumeroteAltN">44&nbsp;&nbsp;&nbsp;&nbsp;An earlier paragraph about
     admissibility.</P>
  <P class="C04Titre1">&nbsp;<B>Legal context</B></P>
  <P class="C09Marge0avecretrait">45.&nbsp;&nbsp;a quoted list item inside the judgment,
     not paragraph 45;</P>
  <P class="C01PointnumeroteAltN">45&nbsp;&nbsp;&nbsp;&nbsp;Proceedings brought against a
     State for acts of its armed forces do not fall within civil matters within the meaning
     of Article 1 of the Brussels Convention.</P>
</body></html>
"""

#: Стара родина розмітки (за формою 61993CJ0415): ``div#TexteOnly`` з якорями
#: секцій; пункти — прості ``<p>N …</p>`` після якоря Grounds, без класів.
#: Навмисно поряд: точка Summary «1.» (інша нумерація) і рядок цитованого
#: переліку «4.» усередині пункту 2 — жоден із них не є пунктом рішення.
OLD_JUDGMENT_HTML = """<html><body><div id="TexteOnly"><p>
<a name="SM"/><h2>Summary</h2><br/><em>
<p>1. A headnote point which is not paragraph 1 of the judgment.</p>
</em><p/>
<a name="MO"/><h2>Grounds</h2><br/><em>
<p>1 By order of 4 December 1997 the Bundesgerichtshof referred three questions
to the Court for a preliminary ruling.</p>
<p>The Convention</p>
<p>2 Article 5 of the Convention provides:</p>
<p>4. as regards a civil claim for damages, in the court seised of those proceedings.</p>
<p>3 Recourse to the public-policy clause in Article 27, point 1, of the Convention
can be envisaged only in exceptional cases.</p>
</em><p/>
<a name="CO"/><h2>Decision on costs</h2><br/><em>
<p>4 The costs incurred by the German Government are not recoverable.</p>
</em><p/>
</div></body></html>
"""

ECLI = "ECLI:EU:C:2022:100"


class _FakeResponse:
    def __init__(
        self, *, status_code: int = 200, text: str = "", json_data: dict[str, Any] | None = None
    ) -> None:
        self.status_code = status_code
        self.text = text
        self._json_data = json_data

    def json(self) -> dict[str, Any]:
        if self._json_data is None:
            raise ValueError("no json body")
        return self._json_data


class _FakeSession:
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


def _resolve_response(celex: str = "62021CJ0100") -> _FakeResponse:
    return _FakeResponse(
        status_code=200,
        json_data={"results": {"bindings": [{"work": {"value": "w"}, "celex": {"value": celex}}]}},
    )


def test_fetch_resolves_ecli_and_reads_paragraph() -> None:
    session = _FakeSession(
        [_resolve_response(), _FakeResponse(status_code=200, text=JUDGMENT_TEXT)]
    )
    adapter = EuCaseLawAdapter(session=session)  # type: ignore[arg-type]

    result = adapter.fetch(ECLI, paragraph="45")

    assert isinstance(result, AdapterPayload)
    assert "proportionality" in result.data["text"]
    assert result.data["celex"] == "62021CJ0100"
    assert result.provenance.attribution is not None
    assert "2011/833/EU" in result.provenance.attribution


def test_fetch_invalid_ecli_is_not_found() -> None:
    adapter = EuCaseLawAdapter(session=_FakeSession([]))  # type: ignore[arg-type]

    result = adapter.fetch("not-an-ecli")

    assert isinstance(result, NotFound)


def test_fetch_unresolved_ecli_is_not_found() -> None:
    session = _FakeSession(
        [_FakeResponse(status_code=200, json_data={"results": {"bindings": []}})]
    )
    adapter = EuCaseLawAdapter(session=session)  # type: ignore[arg-type]

    result = adapter.fetch(ECLI)

    assert isinstance(result, NotFound)


def test_fetch_source_unavailable_on_sparql_network_error() -> None:
    adapter = EuCaseLawAdapter(session=_RaisingSession())  # type: ignore[arg-type]

    result = adapter.fetch(ECLI)

    assert isinstance(result, SourceUnavailable)
    assert result.source == "eu_case_law_cellar"


def test_fetch_source_unavailable_on_document_fetch_failure() -> None:
    session = _FakeSession([_resolve_response(), _FakeResponse(status_code=503, text="")])
    adapter = EuCaseLawAdapter(session=session)  # type: ignore[arg-type]

    result = adapter.fetch(ECLI)

    assert isinstance(result, SourceUnavailable)
    assert result.reason == "http_503"


class _DroppedOnceSession(_FakeSession):
    """Перший запит падає на з'єднанні, яке мережа мовчки скинула."""

    def __init__(self, responses: list[_FakeResponse]) -> None:
        super().__init__(responses)
        self._dropped = False

    def get(self, url: str, **kwargs: Any) -> _FakeResponse:
        if not self._dropped:
            self._dropped = True
            raise requests.ConnectionError("Connection aborted: WinError 10054")
        return super().get(url, **kwargs)


def test_a_connection_dropped_by_the_network_does_not_fail_the_read() -> None:
    session = _DroppedOnceSession(
        [_resolve_response(), _FakeResponse(status_code=200, text=JUDGMENT_TEXT)]
    )
    adapter = EuCaseLawAdapter(session=session)  # type: ignore[arg-type]

    result = adapter.fetch(ECLI, paragraph="45")

    assert isinstance(result, AdapterPayload)


def test_network_failure_names_its_cause() -> None:
    adapter = EuCaseLawAdapter(session=_RaisingSession())  # type: ignore[arg-type]

    result = adapter.fetch(ECLI)

    assert isinstance(result, SourceUnavailable)
    assert result.reason == "network_error:ConnectionError"


def test_search_returns_results_with_ecli() -> None:
    bindings = [
        {
            "ecli": {"value": ECLI},
            "celex": {"value": "62021CJ0100"},
            "title": {"value": "Case C-100/21"},
        },
    ]
    session = _FakeSession(
        [_FakeResponse(status_code=200, json_data={"results": {"bindings": bindings}})]
    )
    adapter = EuCaseLawAdapter(session=session)  # type: ignore[arg-type]

    result = adapter.search("C-100/21")

    assert isinstance(result, AdapterPayload)
    assert result.data["results"][0]["ecli"] == ECLI


def test_health_reports_ok() -> None:
    session = _FakeSession([_FakeResponse(status_code=200, json_data={"boolean": True})])
    adapter = EuCaseLawAdapter(session=session)  # type: ignore[arg-type]

    assert adapter.health().ok is True


def test_policy_declares_case_law_source() -> None:
    adapter = EuCaseLawAdapter()
    source = adapter.policy()

    assert source.id == "eu_case_law_cellar"
    assert source.legal_order == "EU"
    assert source.attribution_required is True


@pytest.mark.parametrize(
    "value,expected",
    [
        ("ECLI:EU:C:2022:100", True),
        ("ECLI:CE:ECHR:2018:1109JUD007140910", True),
        ("not-an-ecli", False),
        ("", False),
    ],
)
def test_is_valid_ecli(value: str, expected: bool) -> None:
    assert is_valid_ecli(value) is expected


# ---------------------------------------------------------------------------
# T151 (виправлення): читання через XHTML, а не Akoma Ntoso
# ---------------------------------------------------------------------------


def test_fetch_asks_cellar_for_xhtml_not_akn() -> None:
    """`Accept: application/akn+xml` Cellar відхиляє кодом 400 (research/13)."""
    session = _FakeSession(
        [_resolve_response(), _FakeResponse(status_code=200, text=JUDGMENT_TEXT)]
    )
    adapter = EuCaseLawAdapter(session=session)  # type: ignore[arg-type]

    adapter.fetch(ECLI, path="45")

    document_call = session.calls[-1]
    assert "akn+xml" not in document_call["headers"]["Accept"]
    assert "application/xhtml+xml" in document_call["headers"]["Accept"]
    assert document_call["headers"]["Accept-Language"] == "eng"


def test_fetch_paragraph_does_not_return_the_neighbouring_one() -> None:
    session = _FakeSession(
        [_resolve_response(), _FakeResponse(status_code=200, text=JUDGMENT_TEXT)]
    )
    adapter = EuCaseLawAdapter(session=session)  # type: ignore[arg-type]

    result = adapter.fetch(ECLI, path="45")

    assert isinstance(result, AdapterPayload)
    assert "proportionality" in result.data["text"]
    assert "admissibility" not in result.data["text"]
    assert result.data["locator"] == "45"


@pytest.mark.parametrize("path", ["§ 45", "п. 45", "paragraph 45", "45."])
def test_paragraph_label_forms_reach_the_same_unit(path: str) -> None:
    session = _FakeSession(
        [_resolve_response(), _FakeResponse(status_code=200, text=JUDGMENT_TEXT)]
    )
    adapter = EuCaseLawAdapter(session=session)  # type: ignore[arg-type]

    result = adapter.fetch(ECLI, path=path)

    assert isinstance(result, AdapterPayload)
    assert result.data["locator"] == "45"


def test_unknown_paragraph_is_not_found_not_a_neighbour() -> None:
    session = _FakeSession(
        [_resolve_response(), _FakeResponse(status_code=200, text=JUDGMENT_TEXT)]
    )
    adapter = EuCaseLawAdapter(session=session)  # type: ignore[arg-type]

    result = adapter.fetch(ECLI, path="9999")

    assert isinstance(result, NotFound)


def test_whole_judgment_returns_text_not_markup() -> None:
    """Раніше сюди потрапляв сам XHTML, зведений по пробілах."""
    session = _FakeSession(
        [_resolve_response(), _FakeResponse(status_code=200, text=JUDGMENT_TEXT)]
    )
    adapter = EuCaseLawAdapter(session=session)  # type: ignore[arg-type]

    result = adapter.fetch(ECLI)

    assert isinstance(result, AdapterPayload)
    assert "JUDGMENT OF THE COURT" in result.data["text"]
    assert "<p" not in result.data["text"] and "coj-normal" not in result.data["text"]


def test_a_read_judgment_can_confirm_a_citation() -> None:
    """Без publication_kind/content_kind конверт не підтверджував би нічого."""
    session = _FakeSession(
        [_resolve_response(), _FakeResponse(status_code=200, text=JUDGMENT_TEXT)]
    )
    adapter = EuCaseLawAdapter(session=session)  # type: ignore[arg-type]

    result = adapter.fetch(ECLI, path="45")

    assert isinstance(result, AdapterPayload)
    assert result.provenance.publication_kind is PublicationKind.COURT_PUBLICATION
    assert result.provenance.content_kind is ContentKind.FRAGMENT
    assert result.provenance.confirmable is True
    assert result.provenance.version_id == "62021CJ0100"


def test_oj_notice_celex_is_not_labelled_court_publication() -> None:
    """T-cites (2026-09-09): `62020CN0340` — повідомлення в OJ C, не рішення суду.

    Друга літера типу CELEX — ``N``: анонс відкритого провадження, який
    видає Офіційний вісник, а не Суд ЄС чи Загальний суд.
    """
    session = _FakeSession(
        [_resolve_response("62020CN0340"), _FakeResponse(status_code=200, text=JUDGMENT_TEXT)]
    )
    adapter = EuCaseLawAdapter(session=session)  # type: ignore[arg-type]

    result = adapter.fetch("62020CN0340")

    assert isinstance(result, AdapterPayload)
    assert result.provenance.publication_kind is PublicationKind.OFFICIAL_JOURNAL_NOTICE
    assert result.provenance.publication_kind is not PublicationKind.COURT_PUBLICATION


def test_supplementary_celex_is_not_taken_for_the_judgment() -> None:
    """SPARQL віддає ``…_RES`` упереміш із самим рішенням; перший рядок не вирок."""
    bindings = [
        {"celex": {"value": "62021CJ0100_RES"}},
        {"celex": {"value": "62021CJ0100"}},
    ]
    session = _FakeSession(
        [
            _FakeResponse(status_code=200, json_data={"results": {"bindings": bindings}}),
            _FakeResponse(status_code=200, text=JUDGMENT_TEXT),
        ]
    )
    adapter = EuCaseLawAdapter(session=session)  # type: ignore[arg-type]

    result = adapter.fetch(ECLI, path="45")

    assert isinstance(result, AdapterPayload)
    assert result.data["celex"] == "62021CJ0100"
    assert "62021CJ0100_RES" not in session.calls[-1]["url"]


def test_a_language_outside_the_official_24_is_not_covered() -> None:
    """У Cellar української версії немає взагалі — це не переклад, а межа покриття."""
    session = _FakeSession([_resolve_response()])
    adapter = EuCaseLawAdapter(session=session)  # type: ignore[arg-type]

    result = adapter.fetch(ECLI, language="uk")

    assert isinstance(result, NotCovered)
    assert len(session.calls) == 1, "по документ не ходимо, якщо мови в джерелі немає"


# ---------------------------------------------------------------------------
# Три родини розмітки й перебір форматів (виправлення після прогону T204)
# ---------------------------------------------------------------------------


def test_middle_family_paragraph_is_read_from_leading_digits() -> None:
    """`C01PointnumeroteAltN`: номер пункту — провідні цифри тексту абзаца."""
    root = _parse_xhtml(MIDDLE_JUDGMENT_HTML)

    text = extract_judgment_paragraph(root, "45")

    assert text is not None
    assert text.startswith("Proceedings brought against a State")
    # Ні провідного номера, ні нерозривних пробілів у тексті пункту не лишається.
    assert not text.startswith("45")
    assert "\xa0" not in text
    # Цитований перелік «45.» з іншим класом — не пункт 45.
    assert "quoted list item" not in text


def test_middle_family_neighbouring_paragraph_is_not_returned() -> None:
    root = _parse_xhtml(MIDDLE_JUDGMENT_HTML)

    assert extract_judgment_paragraph(root, "44") == "An earlier paragraph about admissibility."
    assert extract_judgment_paragraph(root, "9999") is None


def test_old_family_paragraph_is_read_from_grounds_section() -> None:
    """`div#TexteOnly`: пункти — прості `<p>N …</p>` після якоря `#MO`."""
    root = _parse_xhtml(OLD_JUDGMENT_HTML)

    text = extract_judgment_paragraph(root, "3")

    assert text is not None
    assert text.startswith("Recourse to the public-policy clause")
    assert not text.startswith("3")


def test_old_family_quoted_list_item_is_not_taken_for_a_paragraph() -> None:
    """«4. as regards…» — рядок переліку всередині пункту 2, а не пункт 4.

    Пункт 4 у цьому рішенні є, але він у секції витрат: нумерація йде наскрізно,
    і саме її, а не першу-ліпшу цифру на початку абзаца, шукає розбір.
    """
    root = _parse_xhtml(OLD_JUDGMENT_HTML)

    text = extract_judgment_paragraph(root, "4")

    assert text == "The costs incurred by the German Government are not recoverable."


def test_old_family_headnote_point_is_not_taken_for_paragraph_one() -> None:
    """Summary нумерується власними «1.», «2.» — це не пункти рішення."""
    root = _parse_xhtml(OLD_JUDGMENT_HTML)

    text = extract_judgment_paragraph(root, "1")

    assert text is not None
    assert text.startswith("By order of 4 December 1997")
    assert "headnote" not in text


def test_the_old_markup_heuristic_does_not_fire_without_text_only() -> None:
    """Без `div#TexteOnly` наскрізна нумерація абзаців нічого не означає."""
    root = _parse_xhtml(
        "<html><body><p>1 Some prose that merely starts with a digit.</p></body></html>"
    )

    assert extract_judgment_paragraph(root, "1") is None


def test_a_404_on_the_first_format_is_a_missing_format_not_a_missing_document() -> None:
    """Старе рішення на `xhtml` віддає 404, а на `text/html` — документ.

    Раніше перший 404 закривав читання й видавав `not_found`; це оголошувало
    відсутнім документ, який у джерелі є (61993CJ0415, 62005CJ0438, 62012CJ0131).
    """
    session = _FakeSession(
        [
            _resolve_response(),
            _FakeResponse(status_code=404, text=""),
            _FakeResponse(status_code=200, text=OLD_JUDGMENT_HTML),
        ]
    )
    adapter = EuCaseLawAdapter(session=session)  # type: ignore[arg-type]

    result = adapter.fetch(ECLI, path="3")

    assert isinstance(result, AdapterPayload)
    assert result.data["text"].startswith("Recourse to the public-policy clause")
    # Скільки спроб і чим прочитано — видно у відповіді, а не лише в логах.
    assert result.data["fetch_attempts"] == 2
    assert result.data["retrieved_format"] == "html"
    document_calls = session.calls[1:]
    assert len(document_calls) == 2
    assert "application/xhtml+xml" in document_calls[0]["headers"]["Accept"]
    assert document_calls[1]["headers"]["Accept"] == "text/html"


def test_the_first_format_that_works_is_reported_as_one_attempt() -> None:
    session = _FakeSession(
        [_resolve_response(), _FakeResponse(status_code=200, text=JUDGMENT_TEXT)]
    )
    adapter = EuCaseLawAdapter(session=session)  # type: ignore[arg-type]

    result = adapter.fetch(ECLI, path="45")

    assert isinstance(result, AdapterPayload)
    assert result.data["fetch_attempts"] == 1
    assert result.data["retrieved_format"] == "xhtml"


def test_no_machine_readable_format_is_not_covered_not_not_found() -> None:
    """Ідентифікатор резолвиться — отже, «такого ідентифікатора немає» було б брехнею."""
    session = _FakeSession(
        [
            _resolve_response(),
            _FakeResponse(status_code=404, text=""),
            _FakeResponse(status_code=404, text=""),
        ]
    )
    adapter = EuCaseLawAdapter(session=session)  # type: ignore[arg-type]

    result = adapter.fetch(ECLI)

    assert isinstance(result, NotCovered)
    assert "машиночитаному" in result.manual_path
    assert result.subject == ECLI


def test_a_server_error_stops_the_format_loop_instead_of_looking_unpublished() -> None:
    """503 — стан джерела, а не відсутня подача: перебирати його не можна."""
    session = _FakeSession([_resolve_response(), _FakeResponse(status_code=503, text="")])
    adapter = EuCaseLawAdapter(session=session)  # type: ignore[arg-type]

    result = adapter.fetch(ECLI)

    assert isinstance(result, SourceUnavailable)
    assert len(session.calls) == 2, "після 503 інший формат не питаємо"


# ---------------------------------------------------------------------------
# CELEX як ідентифікатор читання
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("62016CJ0064", True),
        ("62016CJ0064_SUM", True),
        ("61993CJ0415", True),
        ("32014R8888", False),  # це законодавство, не практика
        ("ECLI:EU:C:2018:117", False),
        ("", False),
    ],
)
def test_is_case_law_celex(value: str, expected: bool) -> None:
    assert is_case_law_celex(value) is expected


def test_fetch_accepts_a_celex_and_resolves_the_ecli_back() -> None:
    """`get_decision(EU, "62016CJ0064")` більше не «очікуваний формат — ECLI»."""
    session = _FakeSession(
        [
            _FakeResponse(
                status_code=200,
                json_data={"results": {"bindings": [{"ecli": {"value": "ECLI:EU:C:2018:117"}}]}},
            ),
            _FakeResponse(status_code=200, text=JUDGMENT_TEXT),
        ]
    )
    adapter = EuCaseLawAdapter(session=session)  # type: ignore[arg-type]

    result = adapter.fetch("62016CJ0064", path="45")

    assert isinstance(result, AdapterPayload)
    assert result.data["celex"] == "62016CJ0064"
    assert result.data["ecli"] == "ECLI:EU:C:2018:117"
    assert result.data["resolved_document_id"] == "62016CJ0064"
    assert "62016CJ0064" in session.calls[-1]["url"]


def test_a_celex_without_an_ecli_is_still_read() -> None:
    """Немає ECLI — немає ECLI; вигадувати його чи відмовляти в читанні не можна.

    ``OPTIONAL`` у запиті лишає рядок навіть без ``?ecli``, і саме за наявністю
    рядка видно, що документ у Cellar є.
    """
    session = _FakeSession(
        [
            _FakeResponse(status_code=200, json_data={"results": {"bindings": [{}]}}),
            _FakeResponse(status_code=200, text=JUDGMENT_TEXT),
        ]
    )
    adapter = EuCaseLawAdapter(session=session)  # type: ignore[arg-type]

    result = adapter.fetch("61993CJ0415")

    assert isinstance(result, AdapterPayload)
    assert result.data["ecli"] == ""
    assert result.data["celex"] == "61993CJ0415"
    # Реквізити, які джерело віддало, у формі посилання є; ECLI, якого воно не
    # віддало, — немає, і замість нього нічого не підставлено.
    assert result.provenance.citation_format == (
        "рішення (judgment), Суд Європейського Союзу (Court of Justice), CELEX 61993CJ0415"
    )
    assert "ECLI" not in result.provenance.citation_format


def test_a_celex_absent_from_cellar_is_not_found_not_unpublished() -> None:
    """Нуль рядків тут означає рівно «такого CELEX немає», а не «немає формату»."""
    session = _FakeSession(
        [_FakeResponse(status_code=200, json_data={"results": {"bindings": []}})]
    )
    adapter = EuCaseLawAdapter(session=session)  # type: ignore[arg-type]

    result = adapter.fetch("69999CJ9999")

    assert isinstance(result, NotFound)
    assert len(session.calls) == 1, "по документ не ходимо, якщо CELEX у Cellar немає"


def test_a_summary_celex_asked_for_directly_is_not_swapped_for_the_judgment() -> None:
    """Просили резюме — читаємо резюме: підміна відповіла б не на те питання."""
    session = _FakeSession(
        [
            _FakeResponse(
                status_code=200,
                json_data={"results": {"bindings": [{"ecli": {"value": "ECLI:EU:C:2018:117"}}]}},
            ),
            _FakeResponse(status_code=200, text=JUDGMENT_TEXT),
        ]
    )
    adapter = EuCaseLawAdapter(session=session)  # type: ignore[arg-type]

    result = adapter.fetch("62016CJ0064_SUM")

    assert isinstance(result, AdapterPayload)
    assert result.data["celex"] == "62016CJ0064_SUM"
    assert "62016CJ0064_SUM" in session.calls[-1]["url"]


def test_a_summary_celex_does_not_win_over_the_judgment_when_resolving_an_ecli() -> None:
    """SPARQL віддає `…_SUM` поряд із рішенням; резюме — не текст рішення."""
    bindings = [
        {"celex": {"value": "62016CJ0064_SUM"}},
        {"celex": {"value": "62016CJ0064"}},
    ]
    session = _FakeSession(
        [
            _FakeResponse(status_code=200, json_data={"results": {"bindings": bindings}}),
            _FakeResponse(status_code=200, text=JUDGMENT_TEXT),
        ]
    )
    adapter = EuCaseLawAdapter(session=session)  # type: ignore[arg-type]

    result = adapter.fetch("ECLI:EU:C:2018:117", path="45")

    assert isinstance(result, AdapterPayload)
    assert result.data["celex"] == "62016CJ0064"
    assert "_SUM" not in session.calls[-1]["url"]


def test_an_identifier_that_is_neither_ecli_nor_celex_is_not_found() -> None:
    adapter = EuCaseLawAdapter(session=_FakeSession([]))  # type: ignore[arg-type]

    result = adapter.fetch("C-64/16")

    assert isinstance(result, NotFound)
    assert "CELEX" in result.id_format_hint


# ---------------------------------------------------------------------------
# Номер справи: вікно сторіч і суфікс виду провадження (чисті функції)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "two_digits,expected",
    [
        # Живі перевірки SPARQL 2026-09-08: у кожному випадку REGEX з цим роком
        # знаходить справу, а з протилежним сторіччям — нічого.
        ("93", "1993"),  # C-415/93 → 61993CJ0415 (Bosman)
        ("99", "1999"),  # T-1/99 DEP → 61999TJ0001
        ("53", "1953"),  # межа вікна: дворозрядна нумерація почалася 1953-го
        ("52", "2052"),
        ("05", "2005"),  # C-438/05 → 62005CJ0438 (Viking Line)
        ("01", "2001"),  # T-177/01 → 62001TJ0177
        ("12", "2012"),  # C-131/12 → 62012CJ0131
        ("16", "2016"),  # C-64/16 → 62016CJ0064
    ],
)
def test_case_number_year_uses_a_century_window(two_digits: str, expected: str) -> None:
    assert _case_number_year(two_digits) == expected


@pytest.mark.parametrize(
    "case,forum,num,yy,suffix",
    [
        ("C-50/00 P", "C", "50", "00", "P"),
        ("C-263/02 P", "C", "263", "02", "P"),
        ("C-123/45 R", "C", "123", "45", "R"),
        ("T-1/99 DEP", "T", "1", "99", "DEP"),
        ("C-64/16", "C", "64", "16", None),
        ("T-177/01", "T", "177", "01", None),
    ],
)
def test_case_number_regex_keeps_the_procedure_suffix(
    case: str, forum: str, num: str, yy: str, suffix: str | None
) -> None:
    match = _CASE_NUMBER_RE.match(case)

    assert match is not None
    assert (match.group("forum"), match.group("num"), match.group("yy")) == (forum, num, yy)
    assert match.group("suffix") == suffix


@pytest.mark.parametrize("case", ["C-50/00 обжалование", "50/00 P", "C-50/2000", "C-50/00-P"])
def test_an_unparsed_case_number_form_stays_unparsed(case: str) -> None:
    """Тихо вгадувати форму не можна: невідоме лишається невідомим."""
    assert _CASE_NUMBER_RE.match(case) is None


def test_a_twentieth_century_case_card_queries_the_right_century() -> None:
    """Раніше C-415/93 шукалося як `62093C…` і давало хибне «не опубліковано»."""
    session = _FakeSession(
        [
            _FakeResponse(
                status_code=200,
                json_data={"results": {"bindings": [{"celex": {"value": "61993CJ0415"}}]}},
            )
        ]
    )
    adapter = EuCaseLawAdapter(session=session)  # type: ignore[arg-type]

    result = adapter.public_card("C-415/93")

    assert isinstance(result, AdapterPayload)
    assert result.data["published"] is True
    assert "61993" in session.calls[0]["params"]["query"]
    assert "62093" not in session.calls[0]["params"]["query"]


def test_a_case_card_keeps_the_suffix_and_still_finds_the_case() -> None:
    """Суфікс на CELEX не впливає, але з номера справи не зникає."""
    session = _FakeSession(
        [
            _FakeResponse(
                status_code=200,
                json_data={"results": {"bindings": [{"celex": {"value": "62000CJ0050"}}]}},
            )
        ]
    )
    adapter = EuCaseLawAdapter(session=session)  # type: ignore[arg-type]

    result = adapter.public_card("C-50/00 P")

    assert isinstance(result, AdapterPayload)
    assert result.data["published_status"] == "published"
    assert result.data["case_number"] == "C-50/00 P"
    assert result.data["case_suffix"] == "P"
    assert "62000C[A-Z]0*0050" in session.calls[0]["params"]["query"]


# ---------------------------------------------------------------------------
# T-cites (2026-09-09) — «документи, що цитують акт»
# ---------------------------------------------------------------------------


def _cites_bindings() -> list[dict[str, Any]]:
    """Форма живої видачі 2026-09-09 (перші рядки); значення тестові."""
    return [
        {
            "celex": {"value": "62016TJ0101"},
            "date": {"value": "2016-11-30"},
            "ecli": {"value": "ECLI:EU:T:2016:600"},
            "parties": {"value": "Operator A v European Commission"},
            "case": {"value": "Case T-101/16"},
        },
        {
            "celex": {"value": "62016TA0101"},
            "date": {"value": "2016-11-30"},
        },
        {
            "celex": {"value": "62017TJ0202"},
            "date": {"value": "2017-01-25"},
            "ecli": {"value": "ECLI:EU:T:2017:300"},
        },
    ]


def test_cites_returns_navigation_rows_with_document_type_and_case_name() -> None:
    session = _FakeSession(
        [_FakeResponse(status_code=200, json_data={"results": {"bindings": _cites_bindings()}})]
    )
    adapter = EuCaseLawAdapter(session=session)  # type: ignore[arg-type]

    result = adapter.search("", cites="32014R8888", max_results=10)

    assert isinstance(result, AdapterPayload)
    assert result.data["found"] == 3
    assert result.data["complete"] is True
    first = result.data["results"][0]
    assert first["celex"] == "62016TJ0101"
    assert first["ecli"] == "ECLI:EU:T:2016:600"
    assert first["date"] == "2016-11-30"
    assert first["document_type"] == "TJ"
    assert first["court"] == "Загальний суд (General Court)"
    assert first["case_name"] == "Operator A v European Commission"
    assert first["case_number"] == "Case T-101/16"
    # Навігація, не доказ: жодного тексту й ніякого evidence_id тут нема.
    assert "text" not in first
    assert result.data["applied_filters"]["cites"] == "32014R8888"


def test_cites_query_escapes_the_celex_and_starts_with_prefix_six() -> None:
    query = build_cited_by_sparql("32014R8888", limit=60)

    assert '"32014R8888"^^<http://www.w3.org/2001/XMLSchema#string>' in query
    assert 'FILTER(STRSTARTS(STR(?celex), "6"))' in query
    assert "LIMIT 60" in query


def test_cites_query_escapes_embedded_quotes() -> None:
    query = build_cited_by_sparql('32014R8888" ; DROP', limit=5)

    assert '\\"' in query
    assert query.count('"32014R8888\\" ; DROP"') == 1


def test_cites_query_adds_date_and_court_filters_only_when_asked() -> None:
    bare = build_cited_by_sparql("32014R8888", limit=10)
    # ?date дістається завжди (для показу), а FILTER за межами дати — лише
    # коли їх попросили.
    assert "?date >=" not in bare
    assert "?date <=" not in bare
    assert "SUBSTR(STR(?celex), 6, 1)" not in bare

    filtered = build_cited_by_sparql(
        "32014R8888", date_from="2022-01-01", date_to="2023-12-31", court="C", limit=10
    )
    assert '?date >= "2022-01-01"^^<http://www.w3.org/2001/XMLSchema#date>' in filtered
    assert '?date <= "2023-12-31"^^<http://www.w3.org/2001/XMLSchema#date>' in filtered
    assert 'FILTER(SUBSTR(STR(?celex), 6, 1) = "C")' in filtered


def test_cites_query_ignores_an_unknown_court_letter() -> None:
    query = build_cited_by_sparql("32014R8888", court="X", limit=10)

    assert "SUBSTR(STR(?celex), 6, 1)" not in query


def test_cites_marks_completeness_honestly_via_limit_plus_one() -> None:
    """``max_results`` рядків прийшло вище лімітом — видача урізана, не повна."""
    bindings = [
        {"celex": {"value": f"6202{i}TJ000{i}"}, "date": {"value": f"2020-01-0{i}"}}
        for i in range(1, 4)
    ]
    session = _FakeSession(
        [_FakeResponse(status_code=200, json_data={"results": {"bindings": bindings}})]
    )
    adapter = EuCaseLawAdapter(session=session)  # type: ignore[arg-type]

    result = adapter.search("", cites="32014R8888", max_results=2)

    assert isinstance(result, AdapterPayload)
    assert result.data["complete"] is False
    assert len(result.data["results"]) == 2
    assert "LIMIT 3" in session.calls[0]["params"]["query"]


def test_cites_deduplicates_a_work_matched_by_two_language_expressions() -> None:
    """Жива проба 2026-09-09: дві мовні виразки — один документ."""
    bindings = [
        {
            "celex": {"value": "62020TO0055"},
            "date": {"value": "2022-05-30"},
            "ecli": {"value": "ECLI:EU:T:2020:250"},
            "parties": {"value": "Operator B v European Commission"},
            "case": {"value": "Case T-55/20 R"},
        },
        {
            "celex": {"value": "62020TO0055"},
            "date": {"value": "2022-05-30"},
            "ecli": {"value": "ECLI:EU:T:2020:250"},
        },
    ]
    session = _FakeSession(
        [_FakeResponse(status_code=200, json_data={"results": {"bindings": bindings}})]
    )
    adapter = EuCaseLawAdapter(session=session)  # type: ignore[arg-type]

    result = adapter.search("", cites="32014R8888", max_results=10)

    assert result.data["found"] == 1
    assert result.data["results"][0]["case_name"] == "Operator B v European Commission"


def test_cites_is_reachable_by_the_search_by_citation_capability() -> None:
    """Реєстр: cites — reachable_by_id, а не запланована й неперевірена."""
    from core import legal_orders

    matching = [
        cap
        for cap in legal_orders.registered_capabilities()
        if cap.source_id == "eu_case_law_cellar"
        and cap.operation is legal_orders.Operation.SEARCH_BY_CITATION
    ]
    assert matching
    assert matching[0].layer is legal_orders.CoverageLayer.REACHABLE_BY_ID
    assert matching[0].verified_at


# ---------------------------------------------------------------------------
# Тікет 47 (29.09.2026) — рішення за номером справи: C-311/18 → ECLI і дата
# ---------------------------------------------------------------------------


def _case_number_bindings() -> list[dict[str, Any]]:
    """Форма живої видачі 29.09.2026 на кандидатах C-311/18 (Schrems II).

    Cellar віддає той самий твір двічі — з назвою справи (англійський вираз) і без
    неї; висновок ГА (CC) і рішення (CJ) — окремі твори з різними ECLI.
    """
    return [
        {
            "celex": {"value": "62018CC0311"},
            "date": {"value": "2020-04-02"},
            "ecli": {"value": "ECLI:EU:C:2020:252"},
            "parties": {
                "value": "Data Protection Commissioner v Facebook Ireland Limited and\xa0Maximillian Schrems."
            },
            "case": {"value": "Case C-311/18."},
        },
        {
            "celex": {"value": "62018CC0311"},
            "date": {"value": "2020-04-02"},
            "ecli": {"value": "ECLI:EU:C:2020:252"},
        },
        {
            "celex": {"value": "62018CJ0311"},
            "date": {"value": "2020-07-16"},
            "ecli": {"value": "ECLI:EU:C:2020:559"},
            "parties": {
                "value": "Data Protection Commissioner v Facebook Ireland Limited and\xa0Maximillian Schrems"
            },
            "case": {"value": "Case C-311/18"},
        },
        {
            "celex": {"value": "62018CJ0311"},
            "date": {"value": "2020-07-16"},
            "ecli": {"value": "ECLI:EU:C:2020:559"},
        },
    ]


def _case_number_search(
    case_number: str, bindings: list[dict[str, Any]], **options: Any
) -> tuple[Any, _FakeSession]:
    session = _FakeSession(
        [_FakeResponse(status_code=200, json_data={"results": {"bindings": bindings}})]
    )
    adapter = EuCaseLawAdapter(session=session)  # type: ignore[arg-type]
    return adapter.search("", case_number=case_number, **options), session


@pytest.mark.parametrize(
    ("case_number", "expected"),
    [
        ("C-311/18", ("62018CJ0311", "62018CO0311", "62018CC0311")),
        ("C-415/93", ("61993CJ0415", "61993CO0415", "61993CC0415")),  # 98 → 1998
        ("T-212/15", ("62015TJ0212", "62015TO0212")),  # Загальний суд — без висновків ГА
        ("C-50/00 P", ("62000CJ0050", "62000CO0050", "62000CC0050")),  # суфікс не впливає
        ("62018CJ0311", ("62018CJ0311",)),  # CELEX лишається єдиним кандидатом
    ],
)
def test_case_number_expands_to_celex_candidates(
    case_number: str, expected: tuple[str, ...]
) -> None:
    from sources.eu_case_law import case_number_celex_candidates

    assert case_number_celex_candidates(case_number) == expected


@pytest.mark.parametrize(
    "value", ["", "Data Protection Commissioner", "311/18", "C-311", "ECLI:EU:C:2020:559"]
)
def test_a_value_that_is_not_a_case_number_has_no_candidates(value: str) -> None:
    from sources.eu_case_law import case_number_celex_candidates

    assert case_number_celex_candidates(value) is None


def test_search_by_case_number_returns_ecli_and_date_of_the_judgment() -> None:
    result, session = _case_number_search("C-311/18", _case_number_bindings())

    assert isinstance(result, AdapterPayload)
    assert result.data["found"] == 2
    assert result.data["complete"] is True
    opinion, judgment = result.data["results"]
    assert judgment["celex"] == "62018CJ0311"
    assert judgment["ecli"] == "ECLI:EU:C:2020:559"
    assert judgment["date"] == "2020-07-16"
    assert judgment["document_type"] == "CJ"
    assert judgment["case_number"] == "Case C-311/18"
    assert "Data Protection Commissioner" in judgment["case_name"]
    assert opinion["celex"] == "62018CC0311"
    assert opinion["document_type"] == "CC"
    # Навігація, не доказ: тексту нема.
    assert "text" not in judgment


def test_search_by_case_number_sends_exact_celex_values_and_no_free_text() -> None:
    _result, session = _case_number_search("C-311/18", _case_number_bindings())

    query = session.calls[0]["params"]["query"]
    xsd = "^^<http://www.w3.org/2001/XMLSchema#string>"
    for celex in ("62018CJ0311", "62018CO0311", "62018CC0311"):
        assert f'"{celex}"{xsd}' in query
    assert "VALUES ?celex" in query
    # Точний пошук, а не скан усього сховища регулярним виразом.
    assert "REGEX" not in query
    assert "C-311/18" not in query


def test_search_by_case_number_states_its_limits_in_every_answer() -> None:
    from sources.eu_case_law import CASE_NUMBER_SEARCH_NOTICE

    result, _session = _case_number_search("C-311/18", _case_number_bindings())

    assert result.data["notice"] == CASE_NUMBER_SEARCH_NOTICE
    assert (
        "CELEX" in CASE_NUMBER_SEARCH_NOTICE and "об'єднаних справах" in CASE_NUMBER_SEARCH_NOTICE
    )
    assert result.data["applied_filters"]["celex_candidates"] == [
        "62018CJ0311",
        "62018CO0311",
        "62018CC0311",
    ]


def test_search_by_case_number_with_no_rows_is_an_honest_empty_answer() -> None:
    result, _session = _case_number_search("C-9999/26", [])

    assert isinstance(result, AdapterPayload)
    assert result.data["found"] == 0
    assert result.data["results"] == []
    assert result.data["notice"]


def test_search_by_case_number_applies_the_date_filters_and_says_so() -> None:
    result, _session = _case_number_search(
        "C-311/18", _case_number_bindings(), date_from="2020-06-01", date_to="2020-12-31"
    )

    assert [item["celex"] for item in result.data["results"]] == ["62018CJ0311"]
    assert result.data["applied_filters"]["date_from"] == "2020-06-01"
    assert result.data["applied_filters"]["date_to"] == "2020-12-31"


def test_search_by_case_number_refuses_a_shape_that_is_not_a_case_number() -> None:
    result, session = _case_number_search("ECLI:EU:C:2020:559", [])

    assert isinstance(result, NotCovered)
    assert result.operation == "search_by_identifier"
    assert not session.calls, "нерозпізнаний вхід джерелу не надсилається"


def test_search_by_case_number_reports_an_unavailable_source_and_not_an_empty_answer() -> None:
    session = _FakeSession([_FakeResponse(status_code=503)])
    adapter = EuCaseLawAdapter(session=session)  # type: ignore[arg-type]

    result = adapter.search("", case_number="C-311/18")

    assert isinstance(result, SourceUnavailable)


def test_case_number_search_is_reachable_by_the_search_by_identifier_capability() -> None:
    from core import legal_orders

    matching = [
        cap
        for cap in legal_orders.registered_capabilities()
        if cap.source_id == "eu_case_law_cellar"
        and cap.operation is legal_orders.Operation.SEARCH_BY_IDENTIFIER
    ]
    assert matching
    assert matching[0].layer is legal_orders.CoverageLayer.REACHABLE_BY_ID
    assert matching[0].verified_at and matching[0].verified_by
    assert "об'єднаних справах" in matching[0].limitations


def test_the_search_decisions_tool_answers_a_case_number_instead_of_not_covered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Тікет 47: `search_decisions(EU, case_number="C-311/18")` давав `not_covered`."""
    import server
    import sources as sources_registry

    session = _FakeSession(
        [
            _FakeResponse(
                status_code=200, json_data={"results": {"bindings": _case_number_bindings()}}
            )
        ]
    )
    adapter = EuCaseLawAdapter(session=session)  # type: ignore[arg-type]
    monkeypatch.setattr(sources_registry, "get_adapter", lambda adapter_id: adapter)

    answer = server.search_decisions("EU", case_number="C-311/18")

    assert answer.get("code") is None, answer
    assert answer["found"] == 2
    judgment = answer["results"][1]
    assert judgment["ecli"] == "ECLI:EU:C:2020:559"
    assert judgment["date"] == "2020-07-16"
    assert judgment["navigation_only"] is True and judgment["confirmable"] is False
    assert "об'єднаних справах" in answer["notice"]
    assert answer["applied_filters"]["declared_by_adapter"] is True
