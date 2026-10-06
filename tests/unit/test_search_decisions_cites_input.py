"""T-cites (2026-09-09) — вход `search_decisions.cites` на границе инструмента.

`cites` — структурный запрос («документы, что цитируют акт»), а не свободный
текст: CELEX цитируемого акта проверяется контрактом (`contracts.py`) ещё до
того, как значение доходит до маршрутизации или источника. Свободный текст,
поданный в это поле, отклоняется здесь как невалидный идентификатор — этот
файл проверяет именно границу, отдельно от построения запроса
(`tests/integration/test_eu_case_law.py`) и живого прогона через инструмент.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from core.contracts import SearchDecisionsInput  # noqa: E402


@pytest.mark.parametrize(
    "value,expected",
    [
        ("32014R8888", "32014R8888"),
        (" 32014r8888 ", "32014R8888"),  # регистр и пробелы — не повод отклонять
        ("32016R0679", "32016R0679"),
    ],
)
def test_a_celex_act_identifier_is_accepted(value: str, expected: str) -> None:
    params = SearchDecisionsInput(legal_order="EU", cites=value)

    assert params.cites == expected


def test_an_empty_cites_stays_empty() -> None:
    params = SearchDecisionsInput(legal_order="EU", cites="")

    assert params.cites == ""


@pytest.mark.parametrize(
    "value",
    [
        "Regulation 8888/2014 on product registers",
        "product register regulation",
        "62016TJ0101",  # CELEX судової практики — не акт: інша операція
        "32014R8888; DROP TABLE",
    ],
)
def test_free_text_in_cites_is_rejected_before_it_reaches_a_source(value: str) -> None:
    """Відхиляється контрактом — значить, до `route`/адаптера не доходить узагалі."""
    with pytest.raises(ValidationError) as excinfo:
        SearchDecisionsInput(legal_order="EU", cites=value)

    message = str(excinfo.value)
    assert "cites" in message
    assert "CELEX" in message
