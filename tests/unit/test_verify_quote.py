"""`verify_quote`: читання тими самими читачами і стисла відповідь (тікет 17).

Мережі тут немає: підставляється адаптер, але маршрут — справжній, тобто той
самий, яким ходять ``get_article`` і ``get_decision``. Перевіряється саме те,
що операція не заводить власного розбору, власного стану й власної пам'яті
доказів.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core import citations  # noqa: E402
from core import quote_check  # noqa: E402
from core import source_cache  # noqa: E402
from core.legal_orders import DocumentClass  # noqa: E402
from core.provenance import (  # noqa: E402
    ContentKind,
    ProvenanceStamp,
    PublicationKind,
    SourceChannel,
)
from sources import load_adapters  # noqa: E402
from sources.base import AdapterPayload  # noqa: E402

#: Маршрут пропускає джерело лише з підключеним адаптером, тому бойові адаптери
#: реєструються; підставним лишається сам ``fetch``, а не карта покриття.
load_adapters()

ARTICLE_5A = (
    "1. By way of derogation from Article 2, the competent authorities of the "
    "Member States may authorise the exemption of certain registered products or "
    "packaging materials, provided that the conditions laid down in this "
    "Article are met."
)

JUDGMENT = (
    "45. It follows that the registration of products constitutes a preventive "
    "measure which is not intended to penalise the operators concerned for their "
    "trade.\n"
    "46. Nevertheless, the restriction must be proportionate to the objective "
    "pursued."
)


class _TextAdapter:
    """Адаптер, що віддає заздалегідь відомий текст."""

    def __init__(self, text: str, *, locator: str = "5a", language: str = "en") -> None:
        self.text = text
        self.locator = locator
        self.language = language
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def fetch(self, document_id: str, **options: Any) -> AdapterPayload:
        self.calls.append((document_id, dict(options)))
        return AdapterPayload(
            data={"text": self.text, "title": "Regulation 8888/2014", "locator": self.locator},
            provenance=ProvenanceStamp(
                source_channel=SourceChannel.LIVE,
                source_url="https://publications.europa.eu/resource/celex/32014R8888",
                language=self.language,
                citation_format="CELEX 32014R8888, article 5a",
                version_id="02014R8888-20260807",
                stale=False,
                is_authentic_version=True,
                is_translation=False,
                publication_kind=PublicationKind.OFFICIAL_JOURNAL,
                content_kind=ContentKind.FRAGMENT,
            ),
        )


class _FailingAdapter:
    """Адаптер, що віддає типізовану відмову."""

    def __init__(self, failure: Any) -> None:
        self.failure = failure

    def fetch(self, document_id: str, **options: Any) -> Any:
        return self.failure


def _verify(adapter: Any, **kwargs: Any) -> dict[str, Any]:
    params = {
        "legal_order": "EU",
        "document_id": "32014R8888",
        "path": "5a",
        "quote": "",
        "session_id": "test-session",
        "document_class": DocumentClass.ACT,
        "adapters": {"eu_law_eurlex_cellar": adapter, "eu_case_law_cellar": adapter},
    }
    params.update(kwargs)
    return quote_check.verify(**params)


class TestStatuses:
    """Кожен статус досяжний і названий."""

    def test_exact(self) -> None:
        result = _verify(
            _TextAdapter(ARTICLE_5A), quote="the exemption of certain registered products"
        )

        assert result["status"] == "exact"

    def test_normalized_with_typographic_quotes(self) -> None:
        text = "The term “registered products” is defined in Article 1 of the Regulation."

        result = _verify(
            _TextAdapter(text),
            quote='The term "registered products" is defined in Article 1',
        )

        assert result["status"] == "normalized"
        assert "quotes" in result["normalizations"]

    def test_mismatch_marks_the_difference(self) -> None:
        result = _verify(
            _TextAdapter(ARTICLE_5A),
            quote="the competent authorities of the Member States shall authorise",
        )

        assert result["status"] == "mismatch"
        assert "{+shall+}" in result["differences"]
        assert "[-may-]" in result["differences"]

    def test_empty_text_from_the_reader_is_path_not_found(self) -> None:
        result = _verify(_TextAdapter(""), quote="the exemption of certain registered products")

        assert result["status"] == "path_not_found"

    def test_source_unavailable_is_typed(self) -> None:
        from core.contracts import SourceUnavailable

        failure = SourceUnavailable(
            source="eu_law_eurlex_cellar",
            retry_after="за 10 хвилин",
            reason="timeout",
        )

        result = _verify(
            _FailingAdapter(failure), quote="the exemption of certain registered products"
        )

        assert result["status"] == "source_unavailable"
        assert result["code"] == "source_unavailable"
        assert result["details"]["manual_path"]

    def test_not_found_from_the_source_is_path_not_found(self) -> None:
        from core.contracts import NotFound

        failure = NotFound(identifier="5a", id_format_hint="CELEX, напр. 32014R8888")

        result = _verify(
            _FailingAdapter(failure), quote="the exemption of certain registered products"
        )

        assert result["status"] == "path_not_found"

    def test_not_covered_is_typed(self) -> None:
        """Джерело є в карті покриття, але операція для нього не перевірена."""
        result = quote_check.verify(
            legal_order="EU",
            document_id="32014R8888",
            path="5a",
            quote="the exemption of certain registered products",
            session_id="test-session",
            document_class=DocumentClass.REGISTRY_RECORD,
        )

        assert result["status"] == "not_covered"
        assert result["code"] == "not_covered"
        assert result["details"]["manual_path"]

    def test_a_missing_document_is_not_reported_as_a_missing_path(self) -> None:
        """``not_found`` про документ і ``path_not_found`` про адресу — різні відповіді."""
        from core.contracts import NotFound

        failure = NotFound(identifier="32014R9999", id_format_hint="CELEX")

        result = _verify(
            _FailingAdapter(failure),
            document_id="32014R9999",
            quote="the exemption of certain registered products",
        )

        assert result["status"] == "not_found"

    def test_unknown_legal_order_is_refused_before_any_read(self) -> None:
        result = quote_check.verify(
            legal_order="ZZ",
            document_id="32014R8888",
            path="5a",
            quote="the exemption of certain registered products",
            session_id="test-session",
            document_class=DocumentClass.ACT,
        )

        assert result["code"] == "unknown_legal_order"


class TestAnswerShape:
    """Відповідь стисла: фрагмент із контекстом, а не весь текст одиниці."""

    def test_the_whole_unit_text_is_not_returned(self) -> None:
        result = _verify(
            _TextAdapter(ARTICLE_5A), quote="the exemption of certain registered products"
        )

        assert "text" not in result
        assert len(result["fragment"]) < len(ARTICLE_5A)

    def test_provenance_fields_are_present(self) -> None:
        result = _verify(
            _TextAdapter(ARTICLE_5A), quote="the exemption of certain registered products"
        )

        assert result["source_url"].startswith("https://")
        assert result["fetched_at"]
        assert result["language"] == "en"
        assert result["revision"] == "02014R8888-20260807"
        assert result["authenticity"] == "authentic"

    def test_position_is_reported(self) -> None:
        quote = "the exemption of certain registered products"

        result = _verify(_TextAdapter(ARTICLE_5A), quote=quote)

        assert ARTICLE_5A[result["position"] : result["position"] + len(quote)] == quote

    def test_the_notice_warns_that_a_text_match_proves_nothing_about_the_proposition(
        self,
    ) -> None:
        result = _verify(
            _TextAdapter(ARTICLE_5A), quote="the exemption of certain registered products"
        )

        assert "тез" in result["notice"].lower()


class TestNoState:
    """Операція без стану: пам'ять доказів не поповнюється."""

    def test_no_evidence_is_registered(self) -> None:
        before = citations.SESSION_TEXTS.count("verify-quote-session")

        _verify(
            _TextAdapter(ARTICLE_5A),
            quote="the exemption of certain registered products",
            session_id="verify-quote-session",
        )

        assert citations.SESSION_TEXTS.count("verify-quote-session") == before

    def test_no_evidence_id_leaks_into_the_answer(self) -> None:
        result = _verify(
            _TextAdapter(ARTICLE_5A), quote="the exemption of certain registered products"
        )

        assert "evidence_id" not in result

    def test_the_public_text_cache_is_not_overwritten(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Звірка не гасить факт «джерело змінилося» (D-14).

        Кеш публічних текстів відповідає на питання «той самий текст чи інший».
        Якщо звірка перезапише кеш своїм читанням, наступний ``get_article``
        скаже ``same`` там, де джерело насправді змінилося, — і зміна норми
        зникне мовчки. Тому шлях читання звірки кеш лише читає.
        """
        from core.legal_orders import Operation
        from core.routing import ReadRequest, perform_read

        monkeypatch.setenv(source_cache.CACHE_DIR_ENV, str(tmp_path))
        changed = ARTICLE_5A.replace("may authorise", "shall authorise")

        def _read(text: str) -> dict[str, Any]:
            return perform_read(
                ReadRequest(
                    legal_order="EU",
                    operation=Operation.READ_FRAGMENT,
                    document_class=DocumentClass.ACT,
                    document_id="32014R8888",
                    path="5a",
                    tool="get_article",
                ),
                session_id="cache-session",
                adapters={"eu_law_eurlex_cellar": _TextAdapter(text)},
            )

        assert _read(ARTICLE_5A)["cache_state"] == "unknown"

        _verify(
            _TextAdapter(changed),
            quote="the exemption of certain registered products",
            session_id="cache-session",
        )

        assert _read(changed)["cache_state"] == "changed"


class TestDecisions:
    """Рішення: порожній ``path`` означає пошук по всьому тексту."""

    def test_empty_path_searches_the_whole_decision(self) -> None:
        adapter = _TextAdapter(JUDGMENT, locator="")

        result = _verify(
            adapter,
            document_id="62019CJ0487",
            path="",
            quote="a preventive measure which is not intended to penalise",
        )

        assert result["status"] == "exact"

    def test_a_paragraph_of_a_decision_is_read_as_a_decision(self) -> None:
        adapter = _TextAdapter(JUDGMENT, locator="46")

        result = _verify(
            adapter,
            document_id="ECLI:EU:C:2021:798",
            path="46",
            quote="the restriction must be proportionate to the objective pursued",
        )

        assert result["status"] == "exact"
        assert result["document_class"] == "decision"

    def test_the_answer_says_the_class_was_guessed_from_the_identifier(self) -> None:
        """Здогад названий здогадом: юрист бачить, яким читачем це прочитано."""
        result = _verify(
            _TextAdapter(JUDGMENT, locator="46"),
            document_id="ECLI:EU:C:2021:798",
            path="46",
            quote="the restriction must be proportionate to the objective pursued",
        )

        assert result["document_class_guessed"] is True

    def test_an_explicit_act_identifier_is_not_guessed_twice(self) -> None:
        adapter = _TextAdapter(ARTICLE_5A)

        result = _verify(adapter, quote="the exemption of certain registered products")

        assert result["document_class"] == "act"
        assert len(adapter.calls) == 1


class TestValidation:
    """Коротка цитата — помилка входу, а не статус."""

    def test_a_quote_shorter_than_fifteen_characters_is_an_input_error(self) -> None:
        result = _verify(_TextAdapter(ARTICLE_5A), quote="exempted")

        assert result["code"] == "invalid_input"


@pytest.mark.parametrize(
    ("text", "quote"),
    [
        (
            "Účastníci řízení mají právo nahlížet do spisu.",
            "Účastníci řízení mají právo nahlížet",
        ),
        (
            "Реєстрація\n" "товарів є заходом.",
            "Реєстрація " "товарів є заходом.",
        ),
    ],
    ids=["czech", "cyrillic"],
)
def test_diacritics_survive_the_round_trip(text: str, quote: str) -> None:
    result = _verify(_TextAdapter(text, language="cs"), quote=quote)

    assert result["status"] == "normalized"
    assert result["fragment"] in text
