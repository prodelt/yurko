"""T-icj-card (2026-09-09) — расхождение имени операции — системная ловушка.

``get_case("ICJ", "143")`` отказывал ``not_covered`` не потому, что источник
не читается: ``sources/icj.py`` полностью реализует картку дела, а
``list_coverage`` показывал операцию ``card`` слоя ``reachable_by_id``. Дыра
была в том, что маршрутизация инструмента ``get_case`` ищет ОПЕРАЦИЮ
``public_case_card`` и зовёт метод ``adapter.public_card`` — имя, которого
реестр для ``icj_official_archive`` не нёс вовсе (нёс только ``card``, для
другого инструмента, ``get_law_metadata``). Реестр называл возможность одним
именем, код маршрутизации искал её под другим.

Это не разовая опечатка одного источника, а форма ошибки, которая повторится
для любого источника: канонический список ``{Operation: имя_метода}`` внизу
собран из фактического кода диспетчеризации ``server.py`` (какой метод адаптера
каждый инструмент реально зовёт для каждой операции). Тест проверяет обратное
направление для КАЖДОГО зарегистрированного покрытого (не ``not_covered``)
источника: возможность объявлена — значит, адаптер должен нести метод, который
инструмент действительно позовёт. Отсутствие метода при заявленном покрытии —
ровно то расхождение, которое случилось с ICJ, только пойманное здесь, а не
живым прогоном юриста.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from core import legal_orders  # noqa: E402
import sources  # noqa: E402
from core.legal_orders import CoverageLayer, Operation  # noqa: E402

#: Операция → имя метода адаптера, которое для неё реально зовёт server.py
#: (см. вызовы ``adapter.<метод>(...)`` и ``getattr(adapter, "<метод>", None)``
#: в обработчиках инструментов: ``get_law_metadata`` → ``card``, ``get_case`` →
#: ``public_card``, ``get_treaty_status`` → ``relationship``, ``search_decisions``
#: → ``search`` (по трём операциям — идентификатор, свободный текст, цитаты),
#: чтение документа/фрагмента/редакции на дату → ``fetch``).
#: ``list_revisions`` сюда не входит: ни один инструмент его не диспетчеризует
#: отдельным методом на дату этого теста — только читает через ``fetch``.
_OPERATION_METHOD: dict[Operation, str] = {
    Operation.CARD: "card",
    Operation.PUBLIC_CASE_CARD: "public_card",
    Operation.TREATY_STATUS: "relationship",
    Operation.SEARCH_BY_IDENTIFIER: "search",
    Operation.SEARCH_FREE_TEXT: "search",
    Operation.SEARCH_BY_CITATION: "search",
    Operation.READ_DOCUMENT: "fetch",
    Operation.READ_FRAGMENT: "fetch",
    Operation.REVISION_AS_OF: "fetch",
}


@pytest.fixture(autouse=True)
def _fresh_registry() -> None:
    """Восстановить реестр правопорядков и загрузить адаптеры перед тестом.

    Собственное состояние, а не снятое во время сбора тестов: соседний файл
    того же прогона заканчивается ``clear_sources()``/``clear_adapters()``
    (см. docstring ``tests/unit/test_source_registry.py``), и порядок файлов
    внутри одного вызова pytest не гарантирован контрактом.
    """
    legal_orders.register_default_sources()
    sources.load_adapters()


def test_every_covered_capability_has_the_adapter_method_the_tool_will_call() -> None:
    mismatches: list[str] = []
    for cap in legal_orders.registered_capabilities():
        if cap.layer is CoverageLayer.NOT_COVERED:
            continue
        method_name = _OPERATION_METHOD.get(cap.operation)
        if method_name is None:
            continue
        source = legal_orders.get_source(cap.source_id)
        if source is None:  # pragma: no cover - реестр внутренне согласован
            mismatches.append(f"{cap.source_id}/{cap.operation.value}: источник не найден")
            continue
        adapter = sources.get_adapter(source.effective_adapter_id)
        if adapter is None:
            # Это уже ловит другая проверка (`adapter_wired`) — здесь не дублируем.
            continue
        if not callable(getattr(adapter, method_name, None)):
            mismatches.append(
                f"{cap.source_id}: возможность {cap.operation.value} объявлена покрытой "
                f"(слой {cap.layer.value}), но адаптер {type(adapter).__name__} не несёт "
                f"метода {method_name}() — инструмент, маршрутизирующий эту операцию, "
                "его не найдёт (та же форма дефекта, что была у icj_official_archive/"
                "public_case_card, T-icj-card)"
            )
    assert not mismatches, "\n".join(mismatches)


def test_icj_public_case_card_is_registered_under_the_name_get_case_uses() -> None:
    """Регрессия конкретного случая: имя операции и метод больше не расходятся."""
    matching = [
        cap
        for cap in legal_orders.registered_capabilities()
        if cap.source_id == "icj_official_archive" and cap.operation is Operation.PUBLIC_CASE_CARD
    ]
    assert matching, "icj_official_archive должен нести Operation.PUBLIC_CASE_CARD"
    assert matching[0].layer is not CoverageLayer.NOT_COVERED

    adapter = sources.get_adapter("icj_official_archive")
    assert adapter is not None
    assert callable(getattr(adapter, "public_card", None))
