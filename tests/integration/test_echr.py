"""T065/T146 — інтеграційний тест адаптера ЄСПЛ на мокнутому HTTP.

Мережа тут мокнута (Constitution V): жоден тест не звертається до живого HUDOC.
Відповіді query API повторюють реальну форму, підтверджену живою пробою
2026-09-07 (``research/13-runtime-probes-2026-09-07.md``, розділ D): кожен
результат — ``{"columns": {...}, "rank": ...}``, а не плаский словник, і
колонка ``content`` серед них не приходить ніколи — тому текст рішення
адаптер добуває з PDF (``pypdf``), і саме це тут перевіряється: ``fetch``
повертає СПРАВЖНІЙ текст, а не ``conclusion``/``docname`` замість нього (T146).

Розбір PDF мокнуто окремо (``_extract_pdf_text``) — генерувати справжній PDF
із заданим текстом для тесту зайве, коли межа «байти з мережі → текст» і так
чітко відокремлена в модулі; перевіряється те, що адаптер робить з текстом
після розбору, а не сам ``pypdf``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import requests

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import sources.echr as echr_module  # noqa: E402
from core.contracts import (
    FailureCode,
    NotFound,
    SourceUnavailable,
    UpstreamStubDetected,
)  # noqa: E402
from core.provenance import ContentKind, SourceChannel  # noqa: E402
from sources.base import AdapterPayload  # noqa: E402
from sources.echr import BINDING_NOTICE, MANUAL_PATH, EchrAdapter  # noqa: E402


class _Session:
    """Мінімальна заміна ``requests`` з чергою відповідей або винятком."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._queue: list[Any] = []

    def queue(self, *items: Any) -> None:
        self._queue.extend(items)

    def get(self, url: str, **kwargs: Any) -> Any:
        self.calls.append({"url": url, **kwargs})
        if not self._queue:
            raise AssertionError(f"unexpected request to {url}")
        item = self._queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _json_response(
    status_code: int = 200, payload: Any = None, *, bad_json: bool = False
) -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.content = b""
    if bad_json:
        response.json.side_effect = ValueError("not json")
    else:
        response.json.return_value = payload if payload is not None else {"results": []}
    return response


def _pdf_response(status_code: int = 200, content: bytes = b"%PDF-1.4 fake pdf bytes") -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.content = content
    return response


#: Форма, підтверджена живою пробою: результат обгорнутий у "columns".
HIT_COLUMNS = {
    "itemid": "001-187186",
    "ecli": "ECLI:CE:ECHR:2018:1109JUD007140910",
    "appno": "71409/10",
    "docname": "CASE OF BEUZE v. BELGIUM",
    "kpdate": "2018-11-09",
    "conclusion": "No violation of Article 6",
    "languageisocode": "ENG",
    "doctype": "HEJUD",
    "doctypebranch": "GRANDCHAMBER",
    "documentcollectionid2": "CASELAW;JUDGMENTS;GRANDCHAMBER;ENG",
}


def _hit(**overrides: Any) -> dict[str, Any]:
    return {"columns": {**HIT_COLUMNS, **overrides}, "rank": "1"}


FAKE_JUDGMENT_TEXT = (
    "GRAND CHAMBER\n"
    "CASE OF BEUZE v. BELGIUM\n"
    "JUDGMENT\n"
    "PROCEDURE\n"
    "1.  The case originated in an application against the Kingdom of Belgium "
    "lodged with the Court under Article 34 of the Convention.\n"
    "2.  The applicant was represented by Mr X, a lawyer practising in Brussels.\n"
    "3.  The Government were represented by their Agent.\n"
    "1.  The applicant complained under Article 6 of unfairness at trial.\n"
    "4.  The application was communicated to the Government.\n"
    "5.  The Grand Chamber held a hearing on the merits of the case in public.\n"
    "FOR THESE REASONS, THE COURT\n"
    "1.  Holds, unanimously, that there has been a violation of Article 6.\n"
    "SEPARATE OPINIONS\n"
    "JOINT DISSENTING OPINION OF JUDGES A AND B\n"
    "1.  We disagree with the majority for the following reasons.\n"
) * 2  # довжина понад поріг заглушки (500 символів)


@pytest.fixture(autouse=True)
def _patch_pdf_extraction(monkeypatch: pytest.MonkeyPatch) -> None:
    """Розбір PDF мокнутий на рівні модуля — межа мережі, а не pypdf, тестується тут."""
    monkeypatch.setattr(echr_module, "_extract_pdf_text", lambda data: FAKE_JUDGMENT_TEXT)


@pytest.fixture
def adapter() -> tuple[EchrAdapter, _Session]:
    session = _Session()
    return EchrAdapter(session=session), session


# ---------------------------------------------------------------------------
# Успішний шлях — T146: справжній текст, а не conclusion/docname
# ---------------------------------------------------------------------------


def test_fetch_returns_real_pdf_text_not_conclusion_or_docname(
    adapter: tuple[EchrAdapter, _Session],
) -> None:
    instance, session = adapter
    session.queue(_json_response(200, {"results": [_hit()]}), _pdf_response())

    result = instance.fetch("ECLI:CE:ECHR:2018:1109JUD007140910")

    assert isinstance(result, AdapterPayload)
    output = result.as_output()
    assert output["decision_id"] == "ECLI:CE:ECHR:2018:1109JUD007140910"
    assert "PROCEDURE" in output["text"]
    assert output["text"] != HIT_COLUMNS["conclusion"]
    assert output["text"] != HIT_COLUMNS["docname"]
    # резюме йде окремим, явно позначеним полем — не замість тексту
    assert output["conclusion_summary"] == HIT_COLUMNS["conclusion"]
    assert output["binding_scope"] == "parties_and_case_only"
    assert output["binding_notice"] == BINDING_NOTICE
    assert "сторін" in output["binding_notice"] and "справи" in output["binding_notice"]
    assert output["source_channel"] == SourceChannel.LIVE.value
    assert output["is_authentic_version"] is True
    assert output["content_kind"] == ContentKind.FULL_TEXT.value
    assert output["document_type"] == "judgment"
    assert output["chamber"] == "grand_chamber"
    assert output["has_separate_opinions"] is True


def test_fetch_by_paragraph_locator_returns_only_that_paragraph(
    adapter: tuple[EchrAdapter, _Session],
) -> None:
    instance, session = adapter
    session.queue(_json_response(200, {"results": [_hit()]}), _pdf_response())

    result = instance.fetch("001-187186", path="§ 4")

    assert isinstance(result, AdapterPayload)
    output = result.as_output()
    assert "communicated to the Government" in output["text"]
    assert "hearing on the merits" not in output["text"]
    assert output["content_kind"] == ContentKind.FRAGMENT.value
    assert output["locator"] == "§4"


def test_fetch_ignores_nested_renumbered_lists_when_locating_a_paragraph(
    adapter: tuple[EchrAdapter, _Session],
) -> None:
    """Абзац «1.» усередині тексту (вкладений перелік) не підміняє справжній § 1."""
    instance, session = adapter
    session.queue(_json_response(200, {"results": [_hit()]}), _pdf_response())

    result = instance.fetch("001-187186", path="1")

    assert isinstance(result, AdapterPayload)
    output = result.as_output()
    assert "case originated" in output["text"]
    assert "unfairness at trial" not in output["text"]


def test_fetch_does_not_return_a_paragraph_from_the_separate_opinion(
    adapter: tuple[EchrAdapter, _Session],
) -> None:
    """Абзаци доданої окремої думки (після межі) не є частиною тексту рішення."""
    instance, session = adapter
    session.queue(_json_response(200, {"results": [_hit()]}), _pdf_response())

    # "6." не існує в тексті рішення (лише в оперативній частині й опінії, з іншою нумерацією)
    result = instance.fetch("001-187186", path="6")

    assert isinstance(result, NotFound)


def test_search_returns_summaries(adapter: tuple[EchrAdapter, _Session]) -> None:
    instance, session = adapter
    session.queue(_json_response(200, {"results": [_hit()]}))

    result = instance.search("Beuze")

    assert isinstance(result, AdapterPayload)
    output = result.as_output()
    assert output["found"] == 1
    assert output["results"][0]["ecli"] == HIT_COLUMNS["ecli"]
    assert output["results"][0]["document_type"] == "judgment"


def test_fetch_language_mismatch_is_not_found_not_substituted(
    adapter: tuple[EchrAdapter, _Session],
) -> None:
    """ECLI ділять оригінал і переклади; запитана мова без запису — not_found, не підміна."""
    instance, session = adapter
    session.queue(_json_response(200, {"results": [_hit(languageisocode="ENG")]}))

    result = instance.fetch("ECLI:CE:ECHR:2018:1109JUD007140910", language="uk")

    assert isinstance(result, NotFound)
    assert "переклад" in result.message.lower()
    assert "ENG" in result.message  # наявні мови названі чесно


def test_fetch_picks_authentic_english_over_unofficial_translation_by_default(
    adapter: tuple[EchrAdapter, _Session],
) -> None:
    instance, session = adapter
    session.queue(
        _json_response(
            200,
            {
                "results": [
                    _hit(languageisocode="RUS", itemid="001-999999"),
                    _hit(languageisocode="ENG", itemid="001-187186"),
                ]
            },
        ),
        _pdf_response(),
    )

    result = instance.fetch("ECLI:CE:ECHR:2018:1109JUD007140910")

    assert isinstance(result, AdapterPayload)
    output = result.as_output()
    assert output["language"] == "en"
    assert output["is_authentic_version"] is True
    assert output["is_translation"] is False


# ---------------------------------------------------------------------------
# T064 — інтерфейс без гарантій: зникнення не є винятком
# ---------------------------------------------------------------------------


def test_connection_error_becomes_source_unavailable_with_manual_path(
    adapter: tuple[EchrAdapter, _Session],
) -> None:
    instance, session = adapter
    # Дві: обірване з'єднання спільний транспорт повторює один раз свіжим
    # (тікет 23); відмову бачить лише друга обірваність поспіль.
    session.queue(requests.ConnectionError("boom"), requests.ConnectionError("boom"))

    result = instance.fetch("001-187186")

    assert len(session.calls) == 2
    assert isinstance(result, SourceUnavailable)
    assert result.failure_code == FailureCode.SOURCE_UNAVAILABLE
    assert result.source == "echr_hudoc"
    assert MANUAL_PATH.split(" ")[0] in result.message  # ссылка на ручной путь видна юристу


def test_non_200_becomes_source_unavailable(adapter: tuple[EchrAdapter, _Session]) -> None:
    instance, session = adapter
    session.queue(_json_response(503))

    result = instance.search("anything")

    assert isinstance(result, SourceUnavailable)
    assert "503" in result.message


def test_broken_json_becomes_source_unavailable_not_an_exception(
    adapter: tuple[EchrAdapter, _Session],
) -> None:
    instance, session = adapter
    session.queue(_json_response(200, bad_json=True))

    result = instance.fetch("001-187186")

    assert isinstance(result, SourceUnavailable)


def test_pdf_conversion_failure_after_found_metadata_is_source_unavailable(
    adapter: tuple[EchrAdapter, _Session],
) -> None:
    """Метадані знайдено, але жоден зі шляхів конвертації PDF не спрацював."""
    instance, session = adapter
    session.queue(
        _json_response(200, {"results": [_hit()]}),
        _pdf_response(status_code=404, content=b"not a pdf"),
        _pdf_response(status_code=404, content=b"not a pdf"),
    )

    result = instance.fetch("001-187186")

    assert isinstance(result, SourceUnavailable)
    assert "PDF" in result.message


def test_an_unexpected_exception_is_caught_at_the_adapter_boundary(
    adapter: tuple[EchrAdapter, _Session],
) -> None:
    """Страховка decorator'а typed_failures — не спосіб не думати про помилки, а сітка."""
    instance, session = adapter

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("truly unexpected")

    session.get = _boom  # type: ignore[assignment]

    result = instance.fetch("001-187186")

    assert isinstance(result, SourceUnavailable)
    assert result.source == "echr_hudoc"


# ---------------------------------------------------------------------------
# T215 — форма клаузи і захист від мовчазної підміни видачі
#
# Живі проби 2026-09-08: HUDOC на клаузу,
# якої не розуміє, відповідає 200 OK і добіркою за замовчуванням — не помилкою
# і не порожнечею. Тому тут перевіряються дві різні речі: що запит будується
# формою, яка фільтрує, і що видача без запитаного ідентифікатора не видається
# за відповідь, хоч би якою була форма запиту.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("needle", "clause"),
    [
        # номер заяви: єдина форма, яку HUDOC застосовує (жива проба)
        ("14038/88", 'appno:"14038/88"'),
        ("74025/01", 'appno:"74025/01"'),
        # суфікс «+» знімається: з ним видача порожня
        ("55508/07+", 'appno:"55508/07"'),
        # ECLI і itemid — точний збіг, як було
        ("ECLI:CE:ECHR:1989:0707JUD001403888", '(ecli="ECLI:CE:ECHR:1989:0707JUD001403888")'),
        ("001-57619", '(itemid="001-57619")'),
        # решта — запасний варіант, який HUDOC не фільтрує; закритий на рівні
        # можливості (search_free_text не покрито), а не тут
        (
            "right to a fair trial",
            'contains(docname,"right to a fair trial") or contains(appno,"right to a fair trial")',
        ),
    ],
)
def test_search_clause_form_per_identifier_kind(needle: str, clause: str) -> None:
    assert echr_module._search_clause(needle) == clause


def test_ecli_in_mixed_case_is_sent_canonically_uppercase(
    adapter: tuple[EchrAdapter, _Session],
) -> None:
    """HUDOC на ECLI не у верхньому регістрі віддає нуль — це був би хибний not_found."""
    instance, session = adapter
    session.queue(_json_response(200, {"results": [_hit()]}), _pdf_response())

    result = instance.fetch("ECLI:CE:echr:2018:1109JUD007140910")

    assert isinstance(result, AdapterPayload)
    assert session.calls[0]["params"]["query"] == '(ecli="ECLI:CE:ECHR:2018:1109JUD007140910")'


def test_search_by_application_number_uses_appno_clause_not_contains(
    adapter: tuple[EchrAdapter, _Session],
) -> None:
    instance, session = adapter
    session.queue(_json_response(200, {"results": [_hit(appno="14038/88")]}))

    result = instance.search("", case_number="14038/88")

    assert isinstance(result, AdapterPayload)
    assert session.calls[0]["params"]["query"] == 'appno:"14038/88"'
    assert "contains(" not in session.calls[0]["params"]["query"]


def test_search_by_identifier_rejects_a_result_set_without_the_requested_number(
    adapter: tuple[EchrAdapter, _Session],
) -> None:
    """Головна перевірка T215: видача є, запитаної заяви в ній немає — відказ, не видача.

    Саме так поводився HUDOC на стару клаузу: десять чужих справ на будь-який
    номер, ÜLGER v. TURKEY першою. Результат, що не стосується запиту, гірший
    за порожній (принцип III), тому він не доходить до юриста.
    """
    instance, session = adapter
    session.queue(
        _json_response(
            200,
            {
                "results": [
                    _hit(appno="28505/95", docname="ÜLGER v. TURKEY", itemid="001-5371"),
                    _hit(appno="31872/19", docname="CASE OF SAIDOV v. RUSSIA"),
                ]
            },
        )
    )

    result = instance.search("", case_number="14038/88")

    assert isinstance(result, SourceUnavailable)
    assert "фільтр не застосовано" in result.message
    assert "14038/88" in result.message
    assert MANUAL_PATH.split(" ")[0] in result.message


def test_search_by_identifier_accepts_a_result_set_that_has_the_requested_number(
    adapter: tuple[EchrAdapter, _Session],
) -> None:
    instance, session = adapter
    session.queue(
        _json_response(
            200,
            {
                "results": [
                    _hit(appno="28505/95", docname="ÜLGER v. TURKEY"),
                    _hit(appno="14038/88", docname="Soering v. the United Kingdom"),
                ]
            },
        )
    )

    result = instance.search("", case_number="14038/88")

    assert isinstance(result, AdapterPayload)
    assert result.as_output()["found"] == 2


def test_joined_case_with_several_application_numbers_passes_the_check() -> None:
    """Об'єднана справа: HUDOC віддає appno склейкою, запитаний номер усередині."""
    hits = [{"appno": "31253/96;14038/88;37112/97", "itemid": "001-162333"}]

    assert echr_module.results_contain_identifier(hits, "14038/88") is True
    assert echr_module.results_contain_identifier(hits, "37112/97") is True
    assert echr_module.results_contain_identifier(hits, "99999/99") is False


def test_identifier_check_normalizes_the_form_of_the_number() -> None:
    hits = [{"appno": "55508/07;29520/09"}]

    assert echr_module.results_contain_identifier(hits, "55508/07+") is True
    assert echr_module.results_contain_identifier(hits, " 55508/07 ") is True
    # без слеша це вже не номер заяви за формою — звіряти нема з чим
    assert echr_module.results_contain_identifier(hits, "5550807") is False


def test_identifier_check_matches_ecli_and_itemid_and_tolerates_missing_fields() -> None:
    # ecli приходить і як None (жива проба, запис 002-11858) — це не має падати
    hits = [
        {"appno": "14038/88", "ecli": None, "itemid": "002-9402"},
        {"appno": "14038/88", "ecli": "ECLI:CE:ECHR:1989:0707JUD001403888", "itemid": "001-57619"},
    ]

    assert echr_module.results_contain_identifier(hits, "ECLI:CE:ECHR:1989:0707JUD001403888")
    assert echr_module.results_contain_identifier(hits, "001-57619")
    assert not echr_module.results_contain_identifier(hits, "001-99999999")


def test_identifier_check_refuses_a_form_it_cannot_verify() -> None:
    """Невпізнана форма — False: непідтверджену видачу не можна видавати за відповідь."""
    assert (
        echr_module.results_contain_identifier(
            [{"docname": "right to a fair trial"}], "right to a fair trial"
        )
        is False
    )


def test_identifier_kind_names_the_three_forms() -> None:
    assert echr_module.identifier_kind("ECLI:CE:ECHR:1989:0707JUD001403888") == "ecli"
    assert echr_module.identifier_kind("001-57619") == "itemid"
    assert echr_module.identifier_kind("14038/88") == "appno"
    assert echr_module.identifier_kind("right to a fair trial") is None


def test_search_by_a_nonexistent_number_is_not_found_not_a_default_result_set(
    adapter: tuple[EchrAdapter, _Session],
) -> None:
    """`appno:"99999/99"` живцем віддає нуль записів — це чесне «немає такого»."""
    instance, session = adapter
    session.queue(_json_response(200, {"results": []}))

    result = instance.search("", case_number="99999/99")

    assert isinstance(result, NotFound)
    assert result.identifier == "99999/99"


def test_fetch_rejects_a_result_set_about_another_document(
    adapter: tuple[EchrAdapter, _Session],
) -> None:
    """Читання за itemid теж звіряється: чужий документ не видається за запитаний."""
    instance, session = adapter
    session.queue(_json_response(200, {"results": [_hit(itemid="001-5371", appno="28505/95")]}))

    result = instance.fetch("001-57619")

    assert isinstance(result, SourceUnavailable)
    assert "фільтр не застосовано" in result.message


def test_free_text_search_is_not_subjected_to_the_identifier_check(
    adapter: tuple[EchrAdapter, _Session],
) -> None:
    """Звірка стосується пошуку за ідентифікатором; вільний текст закритий інакше."""
    instance, session = adapter
    session.queue(_json_response(200, {"results": [_hit()]}))

    result = instance.search("Beuze")

    assert isinstance(result, AdapterPayload)


# ---------------------------------------------------------------------------
# Інше
# ---------------------------------------------------------------------------


def test_empty_result_set_is_not_found_not_source_unavailable(
    adapter: tuple[EchrAdapter, _Session],
) -> None:
    instance, session = adapter
    session.queue(_json_response(200, {"results": []}))

    result = instance.fetch("001-999999")

    assert isinstance(result, NotFound)


def test_malformed_identifier_is_not_found_before_any_network_call(
    adapter: tuple[EchrAdapter, _Session],
) -> None:
    instance, session = adapter

    result = instance.fetch("не є ідентифікатором HUDOC")

    assert isinstance(result, NotFound)
    assert session.calls == []


def test_stub_response_is_rejected(
    adapter: tuple[EchrAdapter, _Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, session = adapter
    session.queue(
        _json_response(200, {"results": [_hit()]}),
        _pdf_response(content=b"%PDF-1.4 Just a moment... checking your browser"),
    )
    monkeypatch.setattr(
        echr_module,
        "_extract_pdf_text",
        lambda data: "Please verify you are a human — checking your browser",
    )

    result = instance.fetch("001-187186")

    assert isinstance(result, UpstreamStubDetected)


def test_policy_declares_unofficial_interface_and_a_manual_fallback() -> None:
    instance = EchrAdapter()
    source = instance.policy()

    assert source.access.value == "unofficial_interface"
    assert source.manual_path == MANUAL_PATH


def test_policy_declares_date_filter_unsupported() -> None:
    assert EchrAdapter().supports_date_filter is False


def test_health_uses_exact_match_and_sort_param() -> None:
    session = _Session()
    session.queue(_json_response(200, {"results": [_hit()]}))
    instance = EchrAdapter(session=session)

    health = instance.health()

    assert health.ok is True
    call = session.calls[0]
    assert call["params"]["sort"] == ""
    assert call["params"]["query"] == '(itemid="001-114082")'


def test_health_reports_down_without_raising() -> None:
    session = _Session()
    session.queue(requests.ConnectionError("boom"), requests.ConnectionError("boom"))
    instance = EchrAdapter(session=session)

    health = instance.health()

    assert health.ok is False
