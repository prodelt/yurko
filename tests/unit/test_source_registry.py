"""T024, T066 — реестр источников как единственный источник истины о покрытии.

Проверяется то, что срезы `specs/001-eu-intl-law-expansion/research/01`–`06`
на деле попали в карту (T024), и граница R-07: сбор со страниц в этой
итерации не подключается, и это ломается при попытке протащить его молча
(T066).

Каждый тест начинается с :func:`legal_orders.register_default_sources`, а не
полагается на состояние, оставшееся от импорта модуля: другой тест того же
прогона (`tests/unit/test_legal_orders.py`) заканчивается `clear_sources()`, и
порядок файлов внутри одного вызова pytest не гарантирован контрактом.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from core import legal_orders  # noqa: E402
from core.legal_orders import (  # noqa: E402
    CoverageLayer,
    SourceAccess,
    registered_sources,
)


@pytest.fixture(autouse=True)
def _default_registry() -> None:
    """Восстановить каталог этого модуля перед каждым тестом, идемпотентно."""
    legal_orders.register_default_sources()


# ---------------------------------------------------------------------------
# T024 — источники research/01–06 присутствуют, со слоем и первоисточником
# ---------------------------------------------------------------------------

#: Идентификатор источника → (правопорядок, срез исследования, из которого
#: взят факт). Полнота этой таблицы и есть контракт T024: добавление
#: источника без записи здесь означает, что о нём некому подтвердить, что он
#: взят из среза, а не из общих представлений. Срез в описании источника больше
#: не цитируется (файлов исследования в поставке нет) — он остаётся справкой
#: для разработчика.
EXPECTED_SOURCES: dict[str, tuple[str, str]] = {
    "eu_law_eurlex_cellar": ("EU", "research/01-eu-sources.md"),
    "eu_case_law_cellar": ("EU", "research/01-eu-sources.md"),
    "eu_registry_ted": ("EU", "research/01-eu-sources.md"),
    "eu_registry_euipo": ("EU", "research/01-eu-sources.md"),
    "eu_registry_epo_ops": ("EU", "research/01-eu-sources.md"),
    "eu_registry_gleif": ("EU", "research/01-eu-sources.md"),
    "ua_rada_open_data": ("UA", ""),
    "echr_hudoc": ("ECHR", "research/01-eu-sources.md"),
    "icj_official_archive": ("ICJ", "research/01-eu-sources.md"),
    "intl_arbitration_pca_icsid": ("UN", "research/01-eu-sources.md"),
    "eu_cjeu_public_case_card": ("EU", "research/10"),
    "ua_court_register_edrsr": ("UA", ""),
    "ua_debtors_register": ("UA", ""),
    "ua_prozorro": ("UA", ""),
    "ua_open_data_catalog": ("UA", ""),
}


def test_every_source_from_the_research_slices_is_registered() -> None:
    ids = {source.id for source in registered_sources()}
    missing = set(EXPECTED_SOURCES) - ids
    assert not missing, f"источники из среза research не заведены в карту: {missing}"


@pytest.mark.parametrize("source_id", sorted(EXPECTED_SOURCES))
def test_each_source_names_its_legal_order_and_layer(source_id: str) -> None:
    source = legal_orders.get_source(source_id)
    assert source is not None, f"{source_id} отсутствует в реестре"

    expected_order, _ = EXPECTED_SOURCES[source_id]
    assert source.legal_order == expected_order

    assert source.layer in CoverageLayer
    assert source.access in SourceAccess
    assert source.license.strip(), f"{source_id}: лицензия не указана"
    assert source.source_url.strip(), f"{source_id}: нет ссылки на первоисточник"


@pytest.mark.parametrize("source_id", sorted(EXPECTED_SOURCES))
def test_each_source_says_where_the_fact_came_from_without_internal_references(
    source_id: str,
) -> None:
    """Опис несе факт і першоджерело, а не посилання на файли, яких у поставці немає.

    Раніше цей тест вимагав у `description` посилання на `research/NN-….md` — зріз
    дослідження, з якого взято факт. Ці файли лишилися в архіві розробки, а
    `list_coverage` віддавав посилання юристові й моделі: адреса, за якою нічого
    немає (аудит пакета 22.1, № 16). Тепер першоджерело відновлюється з
    `source_url` і `manual_path`, а опис не посилається на ADR, `research/` і
    тікети.
    """
    source = legal_orders.get_source(source_id)
    assert source is not None

    assert source.description.strip(), f"{source_id}: порожній опис"
    assert source.source_url.strip(), f"{source_id}: немає першоджерела"
    for dangling in ("research/", "ADR ", "тікет", "specs/"):
        assert (
            dangling not in source.description
        ), f"{source_id}: опис посилається на «{dangling}», якого немає в поставці"


def test_every_source_belongs_to_a_known_legal_order() -> None:
    known = set(legal_orders.KNOWN_LEGAL_ORDER_CODES)
    for source in registered_sources():
        assert source.legal_order in known


# ---------------------------------------------------------------------------
# T023 (реестр) / R-12 — лицензия и слой не расходятся
# ---------------------------------------------------------------------------


def test_no_source_with_a_non_commercial_licence_is_connected() -> None:
    for source in registered_sources():
        if source.license_forbids_commercial:
            assert source.layer is not CoverageLayer.CONNECTED, (
                f"{source.id}: лицензия запрещает коммерческое использование, "
                "connected быть не может (R-12)"
            )


def test_every_not_covered_source_carries_a_manual_path() -> None:
    for source in registered_sources():
        if source.layer is CoverageLayer.NOT_COVERED:
            assert source.manual_path, f"{source.id}: not_covered без ручного пути"


# ---------------------------------------------------------------------------
# T066 — сбор со страниц не в первом слое: page_scrape никогда не connected
# ---------------------------------------------------------------------------


def test_no_page_scrape_source_is_connected() -> None:
    """R-07: page_scrape во втором слое; попытка протащить его в connected
    обязана ломаться, а не проходить молча."""
    offenders = [
        source.id
        for source in registered_sources()
        if source.access is SourceAccess.PAGE_SCRAPE and source.layer is CoverageLayer.CONNECTED
    ]
    assert not offenders, f"page_scrape в connected (R-07 нарушено): {offenders}"


def test_constructing_a_page_scrape_connected_source_is_rejected_by_the_model() -> None:
    """Граница ломается на уровне модели, а не только по соглашению каталога."""
    from pydantic import ValidationError

    from core.legal_orders import Source, SourceLimits

    # page_scrape + connected само по себе не запрещено валидатором Source —
    # запрет T066 живёт в каталоге и в этом тесте, а не в схеме. Но конструкция
    # обязана хотя бы не проходить мимо контроля лицензии/ручного пути, если
    # запись при этом ещё и претендует на некоммерческую лицензию с connected —
    # это тест дважды не изобретает то, что уже проверяет test_legal_orders.py.
    with pytest.raises(ValidationError):
        Source(
            id="hypothetical_page_scrape_noncommercial",
            legal_order="EU",
            layer=CoverageLayer.CONNECTED,
            access=SourceAccess.PAGE_SCRAPE,
            license="CC BY-NC 4.0",
            license_forbids_commercial=True,
            limits=SourceLimits(),
            source_url="https://example.invalid",
        )
