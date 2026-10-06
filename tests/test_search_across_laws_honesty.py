"""`search_across_laws` называет свою границу числом, а не только прозой (T248).

Две вещи, найденные аудитом 2026-09-09 и закрытые здесь:

* поле ``searched_laws`` отдавало количество **фрагментов** (строк таблицы
  ``docs``), а читается оно как количество документов: на реальном горячем кеше
  это 3073 против 22, то есть агент видел «искали по трём тысячам законов»;
* поиск, который состоялся и не нашёл ничего, возвращал пустой список без
  единого слова о ручном пути — а «не нашлось» и «нормы нет» для юриста разные
  вещи.

Сети нет: индекс строится из временного каталога (принцип V).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402

LAW_ONE = (
    "Стаття 1.\nЗагальні положення\n\n"
    "1. Цей Закон визначає правові засади діяльності у сфері медіа.\n\n"
    "Стаття 4.\nВиди медіа\n\n"
    "1. Видами медіа є друковані медіа та онлайн-медіа.\n"
)
LAW_TWO = (
    "Стаття 2.\nСфера дії\n\n"
    "1. Закон поширюється на відносини у сфері публічних закупівель.\n\n"
    "Стаття 3.\nПринципи\n\n"
    "1. Закупівлі здійснюються за принципом добросовісної конкуренції.\n"
)


@pytest.fixture()
def two_documents(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Горячий кеш ровно из двух документов, разбитых на четыре статьи."""
    texts = tmp_path / "texts"
    texts.mkdir()
    for law_id, title, body in (
        ("2849-20", "Про медіа", LAW_ONE),
        ("922-19", "Про публічні закупівлі", LAW_TWO),
    ):
        (texts / f"{law_id}.json").write_text(
            json.dumps({"law_id": law_id, "title": title, "text": body}, ensure_ascii=False),
            encoding="utf-8",
        )
    monkeypatch.setenv("YURKO_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(server, "LOCAL_TEXTS_DIR", texts)
    # Каталог відкритих даних Ради — це 13 МБ із мережі (`doc.zip`); тест про
    # локальний індекс його не питає, тож джерело підміняється порожнім (тікет 50).
    monkeypatch.setattr(server.open_data_discovery, "search", lambda *a, **k: {"results": []})
    return texts


def test_searched_laws_counts_documents_and_not_fragments(two_documents: Path) -> None:
    """Два документа — значит два, сколько бы статей в них ни было."""
    answer = server.search_across_laws("UA", "друковані медіа")

    assert answer.get("code") is None, answer
    assert answer["searched_laws"] == 2
    # Проза при этом по-прежнему называет и фрагменты: число и текст согласны.
    assert "з 2 документів" in answer["search_scope"]


def test_a_search_that_found_nothing_names_the_manual_route(two_documents: Path) -> None:
    """Поиск состоялся и пуст — и это сказано словами, а не молчанием."""
    answer = server.search_across_laws("UA", "реєстрація морських суден")

    assert answer.get("code") is None, answer
    assert answer["results"] == []
    scope = answer["search_scope"]
    assert "нічого не знайдено" in scope
    assert "не означає, що норми немає" in scope
    assert "zakon.rada.gov.ua" in scope


def test_a_search_that_found_something_does_not_cry_emptiness(two_documents: Path) -> None:
    """Обратный контроль: на найденном запросе оговорки про пустоту нет."""
    answer = server.search_across_laws("UA", "публічних закупівель")

    assert answer["results"], answer
    assert "нічого не знайдено" not in answer["search_scope"]
