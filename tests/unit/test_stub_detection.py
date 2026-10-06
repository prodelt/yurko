"""Оглавление опознаётся сочетанием признаков, а не словом (T221).

Конституция требует двух вещей сразу, и вторая обычно вспоминается позже:
запись, не являющаяся документом, MUST распознаваться — и настоящий документ
MUST NOT объявляться подменой. Живой прогон 2026-09-08 нашёл вторую половину
нарушенной: длинное решение Загального суду (232 КБ, 333 строки сплошной
прозы) несёт подлинный заголовок «Table of contents», и адаптер отдавал
`upstream_stub_detected` вместо текста настоящего решения.

Сеть здесь не нужна: предмет проверки — правило распознавания.
"""

from __future__ import annotations

from sources.stub_detection import StubKind, detect_stub

#: Настоящее оглавление: ссылки целиком, сплошного текста нет.
_CONTENTS_PAGE = "Table of contents\n" + "\n".join(
    f"{index}. Розділ про щось коротке" for index in range(1, 15)
)

#: Тот же по форме перечень на украинском.
_DOCUMENT_LIST = "Перелік документів\n" + "\n".join(f"- пункт {index}" for index in range(1, 15))

#: Документ, у которого заголовок такой же, а содержание — сплошной текст.
_JUDGMENT_WITH_A_CONTENTS_HEADING = (
    "JUDGMENT OF THE COURT OF FIRST INSTANCE\n"
    "Table of contents\n"
    "Background to the dispute\n"
    "Procedure and forms of order sought\n"
    + "\n".join(
        "It must thus be concluded that the applicant has been afforded an effective remedy "
        "before a court having full jurisdiction to review, in law and on the facts, the "
        "individual decisions adopted by the Commission under the contested regulation "
        f"and the procedure followed in adopting them "
        f"(paragraph {index})."
        for index in range(1, 6)
    )
)


def test_a_real_contents_page_is_still_recognised() -> None:
    verdict = detect_stub(_CONTENTS_PAGE)
    assert verdict is not None
    assert verdict.kind is StubKind.TABLE_OF_CONTENTS


def test_a_document_list_is_still_recognised() -> None:
    verdict = detect_stub(_DOCUMENT_LIST)
    assert verdict is not None
    assert verdict.kind is StubKind.TABLE_OF_CONTENTS


def test_a_judgment_is_not_a_stub_merely_because_it_has_a_contents_heading() -> None:
    """Слово в заголовке не доказывает ничего там, где есть сплошной текст."""
    assert detect_stub(_JUDGMENT_WITH_A_CONTENTS_HEADING) is None


def test_the_shape_heuristic_still_catches_an_unlabelled_contents_page() -> None:
    """Перечень без заголовка ловится формой — эта половина правила не тронута."""
    unlabelled = "\n".join(f"{index}. Стаття про щось" for index in range(1, 15))
    verdict = detect_stub(unlabelled)
    assert verdict is not None
    assert verdict.kind is StubKind.TABLE_OF_CONTENTS
