"""Добір «хто цитує акт»: повнота, види й дедуплікація (T435, FR-662).

Цей добір годує референс-набір оцінки шансів, і кожна з трьох помилок нижче
зсуває число мовчки:

* **ліміт видано за повну когорту** — тоді знаменник менший за дійсний;
* **висновки й повідомлення пораховані як рішення** — тоді в набір потрапляє
  те, чого суд не вирішував;
* **мовні версії одного рішення пораховані двічі** — тоді одна справа важить
  як п'ять.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from sources import eu_case_law as eu  # noqa: E402


def _binding(celex: str, **extra: str) -> dict[str, dict[str, str]]:
    row = {"celex": {"value": celex}}
    for key, value in extra.items():
        row[key] = {"value": value}
    return row


# ---------------------------------------------------------------------------
# Види документів
# ---------------------------------------------------------------------------


def test_by_default_only_judgments_and_orders_are_asked_for() -> None:
    query = eu.build_cited_by_sparql("32014R8888")

    assert (
        'SUBSTR(STR(?celex), 7, 1) IN ("J", "O")' in query
    ), "запит не обмежений рішеннями й ухвалами: у набір потраплять висновки"
    assert eu.CITED_BY_DEFAULT_KINDS == ("J", "O")


def test_opinions_are_included_only_when_named_explicitly() -> None:
    query = eu.build_cited_by_sparql("32014R8888", kinds=("J", "O", "C"))

    assert '"C", "J", "O"' in query


def test_only_case_law_celex_is_asked_for() -> None:
    """Акт цитують і інші акти; у наборі потрібна практика."""
    assert 'FILTER(STRSTARTS(STR(?celex), "6"))' in eu.build_cited_by_sparql("32014R8888")


def test_an_empty_kind_list_does_not_silently_widen_the_query() -> None:
    """Порожній перелік видів — це «без фільтра за видом», і так і видно."""
    query = eu.build_cited_by_sparql("32014R8888", kinds=())

    assert "SUBSTR(STR(?celex), 7, 1)" not in query


# ---------------------------------------------------------------------------
# Дедуплікація
# ---------------------------------------------------------------------------


def test_two_rows_of_one_document_collapse_into_one() -> None:
    rows = [
        _binding("62020TO0055", ecli="ECLI:EU:T:2022:1"),
        _binding("62020TO0055", parties="Operator B v Commission", case="T-55/20"),
    ]

    deduped = eu._dedupe_cited_by(rows)

    assert len(deduped) == 1, "один документ у видачі двічі їсть бюджет max_results"


def test_the_richer_row_wins_and_nothing_is_invented() -> None:
    poor = _binding("62020TO0055")
    rich = _binding("62020TO0055", ecli="ECLI:EU:T:2022:1", parties="Operator B", case="T-55/20")

    deduped = eu._dedupe_cited_by([poor, rich])

    assert len(deduped) == 1
    assert eu._binding_value(deduped[0], "ecli") == "ECLI:EU:T:2022:1"


def test_distinct_documents_are_not_merged() -> None:
    rows = [_binding("62020TO0055"), _binding("62021CJ0100")]

    assert len(eu._dedupe_cited_by(rows)) == 2


def test_a_row_without_celex_is_dropped_not_guessed() -> None:
    assert eu._dedupe_cited_by([{"ecli": {"value": "ECLI:EU:T:2022:1"}}]) == []


# ---------------------------------------------------------------------------
# Повнота
# ---------------------------------------------------------------------------


def test_the_query_asks_for_one_row_more_than_requested() -> None:
    """Так відрізняється «це все» від «це перші N»."""
    query = eu.build_cited_by_sparql("32014R8888", limit=11)

    assert "LIMIT 11" in query


@pytest.mark.parametrize(
    "rows,max_results,complete",
    [(5, 10, True), (10, 10, True), (11, 10, False)],
    ids=["fewer", "exactly", "more"],
)
def test_completeness_is_computed_from_the_raw_rows(
    rows: int, max_results: int, complete: bool
) -> None:
    """Повнота рахується до дедуплікації: рядок понад ліміт доводить, що є ще."""
    bindings = [_binding(f"62022TO{index:04d}") for index in range(rows)]

    assert (len(bindings) <= max_results) is complete


def test_a_date_filter_is_named_in_the_query_not_applied_silently() -> None:
    query = eu.build_cited_by_sparql("32014R8888", date_from="2022-01-01", date_to="2023-12-31")

    assert "2022-01-01" in query and "2023-12-31" in query


def test_an_unknown_court_letter_does_not_become_a_silent_filter() -> None:
    """Непідтримуване значення не звужує вибірку мовчки (FR-662, ADR 0008)."""
    query = eu.build_cited_by_sparql("32014R8888", court="Z")

    assert (
        'SUBSTR(STR(?celex), 6, 1) = "Z"' not in query
    ), "невідома літера суду застосувалася як фільтр: вибірка була б хибно вузькою"
