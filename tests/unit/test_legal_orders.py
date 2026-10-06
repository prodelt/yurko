"""T010 — реестр правопорядков и валидаторы источника.

Проверяется то, на чём стоит принцип VII (правопорядок — параметр, а не новый
инструмент) и принцип III (непокрытый слой обязан нести ручной путь).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from core.legal_orders import (  # noqa: E402
    KNOWN_LEGAL_ORDER_CODES,
    LEGAL_ORDERS,
    CoverageLayer,
    LegalOrder,
    LegalOrderKind,
    Source,
    SourceAccess,
    SourceLimits,
    get_legal_order,
    is_known_legal_order,
    normalize_legal_order_code,
)

# ---------------------------------------------------------------------------
# Реестр правопорядков
# ---------------------------------------------------------------------------

EXPECTED_CODES = (
    "UA",
    "EU",
    "ECHR",
    "ICJ",
    "UN",
)


def test_registry_holds_exactly_the_five_declared_orders() -> None:
    assert set(LEGAL_ORDERS) == set(EXPECTED_CODES)
    assert set(KNOWN_LEGAL_ORDER_CODES) == set(EXPECTED_CODES)


@pytest.mark.parametrize("code", EXPECTED_CODES)
def test_every_order_is_fully_described(code: str) -> None:
    order = LEGAL_ORDERS[code]

    assert order.code == code
    assert order.name_uk.strip()
    assert order.name_native.strip()
    assert order.authentic_languages, "правопорядок без аутентичного языка нечем цитировать"
    assert order.citation_style is not None


@pytest.mark.parametrize("code", EXPECTED_CODES)
def test_every_citation_style_carries_two_real_examples(code: str) -> None:
    """`data-model.md`: не менее двух реальных примеров на формат."""
    examples = LEGAL_ORDERS[code].citation_style.examples

    assert len(examples) >= 2
    assert len(set(examples)) == len(examples), "два одинаковых примера — это один пример"
    assert all(example.strip() for example in examples)


def test_states_use_two_letter_codes_and_every_code_is_upper_case() -> None:
    """Длина не различает государство и форум — `EU` тоже двухбуквенный."""
    for order in LEGAL_ORDERS.values():
        assert order.code.isalpha() and order.code.isupper()
        if order.kind is LegalOrderKind.STATE:
            assert len(order.code) == 2, f"{order.code}: государство — двухбуквенный код"


def test_a_state_code_longer_than_two_letters_is_rejected() -> None:
    style = LEGAL_ORDERS["UA"].citation_style
    with pytest.raises(ValidationError):
        LegalOrder(
            code="UKR",
            kind=LegalOrderKind.STATE,
            name_uk="Україна",
            name_native="Україна",
            authentic_languages=("uk",),
            citation_style=style,
        )


def test_lookup_is_case_and_whitespace_insensitive() -> None:
    assert normalize_legal_order_code("  eu ") == "EU"
    assert get_legal_order("echr") is LEGAL_ORDERS["ECHR"]
    assert is_known_legal_order("ua") is True


def test_unknown_code_is_answered_with_none_not_an_exception() -> None:
    """Неизвестный код — это отказ `unknown_legal_order`, который строит вызывающий."""
    assert get_legal_order("ZZ") is None
    assert is_known_legal_order("ZZ") is False


def test_a_legal_order_needs_at_least_one_authentic_language() -> None:
    style = LEGAL_ORDERS["EU"].citation_style
    with pytest.raises(ValidationError):
        LegalOrder(
            code="XX",
            kind=LegalOrderKind.STATE,
            name_uk="Ніде",
            name_native="Nowhere",
            authentic_languages=(),
            citation_style=style,
        )


# ---------------------------------------------------------------------------
# Источник и слой покрытия
# ---------------------------------------------------------------------------


def _source(**overrides: object) -> Source:
    payload: dict[str, object] = {
        "id": "test_source",
        "legal_order": "EU",
        "layer": CoverageLayer.CONNECTED,
        "access": SourceAccess.OFFICIAL_API,
        "license": "Decision 2011/833/EU",
        "license_forbids_commercial": False,
        "attribution_required": False,
        "limits": SourceLimits(),
        "source_url": "https://example.europa.eu",
    }
    payload.update(overrides)
    return Source(**payload)  # type: ignore[arg-type]


def test_a_plain_connected_source_validates() -> None:
    source = _source()

    assert source.layer is CoverageLayer.CONNECTED
    assert source.manual_path is None


def test_not_covered_without_a_manual_path_cannot_be_constructed() -> None:
    """Принцип III: без ручного пути честного отказа не получится."""
    with pytest.raises(ValidationError):
        _source(layer=CoverageLayer.NOT_COVERED, access=SourceAccess.NONE)

    with pytest.raises(ValidationError):
        _source(layer=CoverageLayer.NOT_COVERED, access=SourceAccess.NONE, manual_path="   ")


def test_not_covered_with_a_manual_path_validates() -> None:
    source = _source(
        layer=CoverageLayer.NOT_COVERED,
        access=SourceAccess.NONE,
        manual_path="Портал суду вручну; координатор ECLI — Верховний суд",
    )

    assert source.manual_path
    assert source.layer is CoverageLayer.NOT_COVERED


def test_non_commercial_licence_cannot_be_connected() -> None:
    """R-12: данные под запретом коммерческого использования не попадают в подключённый слой."""
    with pytest.raises(ValidationError):
        _source(license="CC BY-NC 4.0", license_forbids_commercial=True)


def test_non_commercial_licence_may_be_recorded_as_not_covered() -> None:
    source = _source(
        layer=CoverageLayer.NOT_COVERED,
        access=SourceAccess.NONE,
        license="CC BY-NC 4.0",
        license_forbids_commercial=True,
        manual_path="Набір даних — на сайті видавця вручну",
    )

    assert source.license_forbids_commercial is True
    assert source.layer is CoverageLayer.NOT_COVERED


def test_required_attribution_must_actually_carry_a_text() -> None:
    """Пустая обязательная атрибуция нечего воспроизвести в ответе."""
    with pytest.raises(ValidationError):
        _source(attribution_required=True, attribution=None)

    source = _source(attribution_required=True, attribution="© European Union, 1998-2026")
    assert source.attribution


def test_source_of_an_unknown_legal_order_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _source(legal_order="ZZ")


def test_reachable_by_id_needs_no_manual_path() -> None:
    source = _source(layer=CoverageLayer.REACHABLE_BY_ID, access=SourceAccess.CURATED_SET)

    assert source.manual_path is None


# ---------------------------------------------------------------------------
# Контейнер реестра источников — наполняется задачами T025–T028
# ---------------------------------------------------------------------------


def test_source_registry_is_a_container_not_a_hardcoded_list() -> None:
    from core import legal_orders

    legal_orders.clear_sources()
    try:
        assert legal_orders.registered_sources() == ()
        assert legal_orders.sources_for("EU") == ()

        source = _source()
        legal_orders.register_source(source)

        assert legal_orders.get_source("test_source") is source
        assert legal_orders.sources_for("EU") == (source,)
        assert legal_orders.sources_for("UA") == ()
    finally:
        legal_orders.clear_sources()
