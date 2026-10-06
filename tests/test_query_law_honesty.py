"""`query_law` не выдаёт начало акта за ответ на запрос (T248, принцип III).

Аудит 2026-09-09 показал измерением: `search_in_text` при отсутствии совпадения
возвращал `text[:context_chars]`, и это уходило наружу как «фрагмент, отвечающий
на запрос», с провенансом и `evidence_id`. Текст при этом подлинный — ложным
было утверждение, что он отвечает на вопрос. Конституция ставит нерелевантный
ответ ниже пустого, поэтому проверяется не намерение, а поля ответа.

Сеть не трогается: чтение подменяется кешем в памяти (принцип V).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402

LAW_ID = "2849-20"
TEXT = (
    "ЗАКОН УКРАЇНИ\n\nПро медіа\n\n"
    "Стаття 1. Сфера дії Закону\n\n"
    "Цей Закон визначає правові засади діяльності у сфері медіа "
    "(тестовий текст).\n\n"
    "Стаття 4. Види медіа\n\n"
    "Видами медіа є друковані медіа та онлайн-медіа.\n"
)


@pytest.fixture()
def ua_document(monkeypatch: pytest.MonkeyPatch) -> None:
    """Один известный документ в кеше — ни одного сетевого вызова."""

    def fake_resolve(document_id: str) -> dict[str, Any] | None:
        if str(document_id).strip() != LAW_ID:
            return None
        return {
            "id": LAW_ID,
            "title": "Про медіа",
            "url": f"https://zakon.rada.gov.ua/laws/show/{LAW_ID}",
        }

    def fake_get_or_fetch(law_id: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "law_id": LAW_ID,
            "text": TEXT,
            "url": f"https://zakon.rada.gov.ua/laws/show/{LAW_ID}",
            "from_cache": True,
            "doc_kind": "law",
            "retrieved_at": "2026-09-09T00:00:00+00:00",
        }

    monkeypatch.setattr(server, "_resolve_law", fake_resolve)
    monkeypatch.setattr(server.cache, "get_or_fetch", fake_get_or_fetch)


def test_a_query_that_matches_is_reported_as_matched(ua_document: None) -> None:
    """Совпадение есть — и ответ говорит об этом полем, а не молчанием."""
    result = server.query_law("UA", LAW_ID, query="друковані медіа")

    assert result.get("query_matched") is True
    assert result.get("query_notice", "") == ""
    assert "друковані медіа" in result["text"]


def test_a_query_that_matches_nothing_is_not_dressed_as_an_answer(ua_document: None) -> None:
    """Совпадения нет — начало акта возвращается, но названо тем, что оно есть.

    До правки этот же вызов отдавал ровно те же байты **без единого поля**,
    отличающего их от найденного фрагмента: агент читал начало закона как ответ
    на вопрос, которого в тексте нет.
    """
    result = server.query_law(
        "UA", LAW_ID, query="реєстрація морських суден у портовій адміністрації"
    )

    assert result.get("query_matched") is False
    notice = result.get("query_notice", "")
    assert notice, "молчание здесь равно утверждению «это ответ на запрос»"
    assert "не знайдено" in notice
    assert "не відповідь на запит" in notice
    # Текст подлинный и по-прежнему полезен читателю — ложным было не он, а
    # утверждение о нём.
    assert result["text"].startswith("ЗАКОН УКРАЇНИ")


def test_a_query_of_stop_words_only_does_not_invent_a_fragment(ua_document: None) -> None:
    """«та або що» — не запрос; локальный индекс на такое отказывает, и здесь то же."""
    result = server.query_law("UA", LAW_ID, query="та або що")

    assert result.get("query_matched") is False
    assert result.get("query_notice", "")


def test_no_query_means_no_claim_about_a_query(ua_document: None) -> None:
    """Без запроса поле не появляется вовсе: утверждения нет — и поля нет."""
    result = server.query_law("UA", LAW_ID)

    assert result.get("query_matched") is None
    assert result.get("query_notice", "") == ""
