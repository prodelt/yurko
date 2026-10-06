"""T048 — усечённая выдача: либо постраничный обход, либо честный `partial_result`.

Полный постраничный обход живёт в адаптерах (`sources/base.py`, T053) — вне
этой роли. Здесь проверяется то, что принадлежит карте покрытия:
`SourceLimits.max_records_per_request`, объявленный для источника,
превращается в типизированный отказ `partial_result` с указанием, сколько
получено и чем ограничено, а не тонет молча в успешном ответе.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from core import legal_orders  # noqa: E402
from core.contracts import FailureCode  # noqa: E402
from core.legal_orders import Source, SourceAccess, CoverageLayer, SourceLimits  # noqa: E402


@pytest.fixture(autouse=True)
def _default_registry() -> None:
    legal_orders.register_default_sources()


def _source(**overrides: object) -> Source:
    payload: dict[str, object] = {
        "id": "test_limited_source",
        "legal_order": "EU",
        "layer": CoverageLayer.CONNECTED,
        "access": SourceAccess.OFFICIAL_API,
        "license": "Decision 2011/833/EU",
        "limits": SourceLimits(max_records_per_request=10_000),
        "source_url": "https://example.europa.eu",
    }
    payload.update(overrides)
    return Source(**payload)  # type: ignore[arg-type]


def test_a_source_without_a_limit_never_reports_truncation() -> None:
    source = _source(limits=SourceLimits())
    assert source.truncation_failure(received=1_000_000) is None


def test_receiving_fewer_records_than_the_limit_is_not_truncated() -> None:
    source = _source()
    assert source.truncation_failure(received=9_999) is None


def test_hitting_the_limit_produces_a_partial_result_with_the_count_and_the_cause() -> None:
    source = _source()
    failure = source.truncation_failure(received=10_000)

    assert failure is not None
    output = failure.as_output()
    assert output["code"] == FailureCode.PARTIAL_RESULT.value
    assert output["details"]["received"] == 10_000
    assert "test_limited_source" in output["details"]["limited_by"]
    assert "10000" in output["details"]["limited_by"] or "10 000" in output["details"]["limited_by"]


def test_partial_result_never_claims_completeness_in_its_explanation() -> None:
    source = _source()
    failure = source.truncation_failure(received=10_000)
    assert failure is not None
    assert "не гарант" in failure.explain().lower() or "не повна" in failure.explain().lower()


# ---------------------------------------------------------------------------
# Реальные лимиты из карты покрытия (research/01-eu-sources.md §1, §4)
# ---------------------------------------------------------------------------


def test_eurlex_declares_the_ten_thousand_record_limit_from_2026() -> None:
    source = legal_orders.get_source("eu_law_eurlex_cellar")
    assert source is not None
    assert source.limits.max_records_per_request == 10_000

    assert source.truncation_failure(received=10_000) is not None
    assert source.truncation_failure(received=500) is None


def test_epo_ops_declares_its_two_thousand_record_limit() -> None:
    source = legal_orders.get_source("eu_registry_epo_ops")
    assert source is not None
    assert source.limits.max_records_per_request == 2_000

    failure = source.truncation_failure(received=2_000)
    assert failure is not None
    assert failure.as_output()["code"] == FailureCode.PARTIAL_RESULT.value
