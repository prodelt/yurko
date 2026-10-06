"""Document и Unit — сущности из `data-model.md`, часть T016.

Две базовые сущности, на которых стоит всё остальное: `Unit` — минимальная
единица цитирования, `Document` — акт, решение, договор или мягкое право.
Правила, которые здесь проверяются, взяты из `data-model.md` дословно:

- при `has_canonical_text = false` документ не может быть источником цитаты,
  только отсылкой (R-09, FR-005);
- при `is_authentic_version = false` текст обязан быть помечен переводом и
  не может подаваться как норма (принцип V);
- `Unit.text` — только полученный от источника (принцип II).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from core.contracts import DocumentType, Revision, Unit  # noqa: E402

# ---------------------------------------------------------------------------
# Document
# ---------------------------------------------------------------------------


def test_the_four_document_types_exist() -> None:
    assert {member.value for member in DocumentType} == {"act", "decision", "treaty", "soft_law"}


# ---------------------------------------------------------------------------
# Unit
# ---------------------------------------------------------------------------


def _unit(**overrides: object) -> Unit:
    payload: dict[str, object] = {
        "document_id": "32014R8888",
        "path": "Article 2(1)",
        "text": "The Council shall adopt the measures set out in this Regulation.",
        "revision": Revision(
            document_id="32014R8888",
            valid_from="2014-03-17",
            valid_to=None,
            consolidation_id="02014R8888-20260101",
        ),
    }
    payload.update(overrides)
    return Unit(**payload)  # type: ignore[arg-type]


def test_a_unit_of_an_act_validates() -> None:
    unit = _unit()

    assert unit.path == "Article 2(1)"
    assert unit.revision is not None


def test_a_unit_needs_a_document_and_a_path() -> None:
    with pytest.raises(ValidationError):
        _unit(document_id="")

    with pytest.raises(ValidationError):
        _unit(path="   ")


def test_unit_text_may_not_be_empty() -> None:
    """Принцип II: текст только полученный от источника; пустой — не текст нормы."""
    with pytest.raises(ValidationError):
        _unit(text="")

    with pytest.raises(ValidationError):
        _unit(text="   ")


def test_a_unit_of_an_act_requires_a_revision() -> None:
    """`data-model.md`: revision обязательна для актов."""
    with pytest.raises(ValidationError):
        _unit(revision=None, doc_type=DocumentType.ACT)


def test_a_unit_of_a_decision_needs_no_revision() -> None:
    """Решение не переиздаётся редакциями — у него нет интервала действия."""
    unit = _unit(revision=None, doc_type=DocumentType.DECISION)

    assert unit.revision is None


def test_a_unit_is_immutable_once_built() -> None:
    unit = _unit()

    with pytest.raises(ValidationError):
        unit.text = "подменённый текст"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Revision
# ---------------------------------------------------------------------------


def test_a_current_revision_has_no_upper_bound() -> None:
    revision = Revision(document_id="d", valid_from="2014-03-17", valid_to=None)

    assert revision.is_current is True


def test_a_superseded_revision_has_both_bounds() -> None:
    revision = Revision(document_id="d", valid_from="2014-03-17", valid_to="2015-03-13")

    assert revision.is_current is False
    assert revision.covers("2014-06-01") is True
    assert revision.covers("2015-06-01") is False


def test_a_revision_that_ends_before_it_starts_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Revision(document_id="d", valid_from="2015-03-13", valid_to="2014-03-17")


def test_valid_from_is_required() -> None:
    with pytest.raises(ValidationError):
        Revision(document_id="d", valid_from="", valid_to=None)


# ---------------------------------------------------------------------------
# T312 — п'ять інструментів мут-корту і `lesson_review` зареєстровані в
# `server.py` (contracts/moot.md). Поведінка і чотири межі (заборона
# експорту звіту, `consent_missing`, `budget_undeclared`, `untyped_act`)
# перевірені окремо в ``tests/unit/test_moot_tools.py``; тут — лише те, що
# сам файл цього тесту вже перевіряє про інші інструменти: реєстрація на
# MCP-сервері під очікуваним іменем.
# ---------------------------------------------------------------------------

_T312_TOOL_NAMES = (
    "moot_begin",
    "moot_round",
    "moot_state",
    "moot_report",
    "lesson_review",
)
