"""T023, T045 — контракт `list_coverage` и честный отказ `not_covered`.

`list_coverage` (T029) регистрируется координатором как обёртка над
:func:`legal_orders.describe_coverage`; здесь проверяется сама функция —
полнота полей, отсутствие расхождений с лицензией, а также поведение отказа,
которое строит :func:`routing.route` из того же реестра (T030), не из
отдельного списка (FR-004).

Пустой результат поиска и непокрытие — разные ответы, различимые по коду:
это здесь проверяется явно (T045), а не молчаливо подразумевается.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from core import legal_orders  # noqa: E402
from core import routing  # noqa: E402
from core.contracts import FailureCode, NotFound  # noqa: E402
from core.legal_orders import CoverageLayer  # noqa: E402


@pytest.fixture(autouse=True)
def _default_registry() -> None:
    """Карта покрытия и адаптеры поднимаются самим файлом, а не соседями.

    Адаптеры здесь не роскошь: `route` отвечает «канала нет» для источника без
    зарегистрированного адаптера, и без этой строки результат маршрутизации
    зависел бы от того, импортировал ли кто-то раньше `server.py`. Один и тот же
    тест проходил в полном прогоне и падал в одиночном — а порядок файлов не
    должен решать, покрыт правопорядок или нет.
    """
    import sources

    legal_orders.register_default_sources()
    sources.load_adapters()


# ---------------------------------------------------------------------------
# T023 — контракт list_coverage (через legal_orders.describe_coverage)
# ---------------------------------------------------------------------------

_REQUIRED_FIELDS = (
    "id",
    "legal_order",
    "layer",
    "access",
    "license",
    "license_forbids_commercial",
    "attribution_required",
    "attribution",
    "limits",
    "manual_path",
    "source_url",
    "answers_on_the_merits",
    # T183: слой без работающего кода — обещание, которое маршрут отклонит.
    "adapter_wired",
    "id_format_hint",
    "supports_date_filter",
)


def test_list_coverage_returns_every_registered_source() -> None:
    rows = legal_orders.describe_coverage()
    assert len(rows) == len(legal_orders.registered_sources())
    assert {row["id"] for row in rows} == {s.id for s in legal_orders.registered_sources()}


def test_every_row_carries_the_full_field_set() -> None:
    for row in legal_orders.describe_coverage():
        missing = [field for field in _REQUIRED_FIELDS if field not in row]
        assert not missing, f"{row.get('id')}: не хватает полей {missing}"


def test_not_covered_rows_carry_a_non_empty_manual_path() -> None:
    for row in legal_orders.describe_coverage():
        if row["layer"] == CoverageLayer.NOT_COVERED.value:
            assert row["manual_path"], f"{row['id']}: not_covered без ручного пути"


def test_no_row_with_a_non_commercial_licence_sits_in_connected() -> None:
    for row in legal_orders.describe_coverage():
        if row["license_forbids_commercial"]:
            assert (
                row["layer"] != CoverageLayer.CONNECTED.value
            ), f"{row['id']}: некомерційна ліцензія в шарі connected (R-12)"


def test_filtering_by_legal_order_narrows_the_listing() -> None:
    all_rows = legal_orders.describe_coverage()
    eu_rows = legal_orders.describe_coverage("EU")

    assert 0 < len(eu_rows) < len(all_rows)
    assert all(row["legal_order"] == "EU" for row in eu_rows)


def test_a_source_requiring_attribution_carries_a_non_empty_attribution_text() -> None:
    """FR-031: обязательная атрибуция обязана быть воспроизводима в ответе —
    источник, требующий её, не может отдавать пустое поле."""
    rows = legal_orders.describe_coverage()
    required = [row for row in rows if row["attribution_required"]]
    assert required, "в карте должен быть хотя бы один источник с обязательной атрибуцией"
    for row in required:
        assert row["attribution"], f"{row['id']}: attribution_required=true, но attribution пуст"


# ---------------------------------------------------------------------------
# T045 — not_covered с ручным путём; пустота и непокрытие различимы по коду
# ---------------------------------------------------------------------------


def test_the_map_records_why_rada_search_keeps_a_browser_user_agent() -> None:
    """Тікет 25: рішення власника живе в карті, а не лише у звіті тікета.

    Тікет 15 перевів читання Ради на ``data.rada.gov.ua`` з ``User-Agent:
    OpenData``, бо на ``zakon.rada.gov.ua`` стоїть ``robots.txt: Disallow: /``.
    Але живий повнотекстовий пошук існує **лише** на ``zakon``, і на
    ``OpenData`` він відповідає 403 — тож браузерний UA там лишається свідомо.
    Без цього запису кожне наступне рев'ю бачить «браузерний UA» поруч із
    «перевели на OpenData» і починає те саме розслідування заново.
    """
    legal_orders.register_default_sources()
    caps = {
        cap.operation: cap
        for cap in legal_orders.registered_capabilities()
        if cap.source_id == "ua_rada_open_data"
    }

    free_text = caps[legal_orders.Operation.SEARCH_FREE_TEXT]
    said = free_text.limitations.lower()

    assert "браузерн" in said, "рішення не назване словами користувача"
    assert "403" in said, "не сказано, чим відповідає zakon на OpenData"
    assert "свідом" in said or "навмисн" in said, "не сказано, що це рішення, а не недогляд"


def test_the_decision_reaches_the_lawyer_through_list_coverage() -> None:
    """Запис має бути видно у видачі інструмента, а не лише у вихідному коді."""
    legal_orders.register_default_sources()

    rows = legal_orders.describe_coverage("UA")
    rada = next(row for row in rows if row["id"] == "ua_rada_open_data")

    text = json.dumps(rada, ensure_ascii=False).lower()
    assert "браузерн" in text and "403" in text


def test_a_request_into_the_not_covered_layer_gets_a_refusal_with_a_manual_path() -> None:
    """Правопорядок без единого покрытого источника отвечает отказом с действием.

    Пример — UN: в карте у него только непокрытая запись PCA/ICSID с ручным
    путём. Если UN когда-нибудь подключат, пример обязан смениться на другой
    честный, а не удобный.
    """
    decision = routing.route("UN", subject="get_article")

    assert not decision.allowed
    output = decision.as_output()
    assert output is not None
    assert output["code"] == FailureCode.NOT_COVERED.value
    assert output["details"]["manual_path"]


def test_coverage_of_one_operation_does_not_cover_the_neighbouring_one() -> None:
    """Частично покрытый правопорядок отказывает по каждой непокрытой операции.

    ЕСПЧ читает решения HUDOC по идентификатору — и только их. Поиск свободным
    текстом и чтение акта у него не покрыты, и покрытие чтения не должно молча
    распространяться на соседа: именно так выглядит непокрытие, выданное за
    покрытие (принцип III).
    """
    from core.legal_orders import DocumentClass, Operation

    reading = routing.route(
        "ECHR",
        subject="get_decision",
        operation=Operation.READ_DOCUMENT,
        document_class=DocumentClass.DECISION,
    )
    assert reading.allowed, "чтение по идентификатору принято живым прогоном"

    for operation, document_class in (
        (Operation.SEARCH_FREE_TEXT, DocumentClass.DECISION),
        (Operation.READ_FRAGMENT, DocumentClass.ACT),
    ):
        decision = routing.route(
            "ECHR",
            subject="probe",
            operation=operation,
            document_class=document_class,
        )
        assert not decision.allowed, f"{operation.value}/{document_class.value} не покрыта"
        output = decision.as_output()
        assert output is not None
        assert output["code"] == FailureCode.NOT_COVERED.value
        assert output["details"]["manual_path"]


def test_empty_search_result_and_not_covered_are_different_codes() -> None:
    """Пустота — «искали и не нашли», непокрытие — «здесь не искали и не
    могли». Смешивать их запрещено (принцип III)."""
    not_covered_output = routing.route("UN", subject="search_articles").as_output()
    assert not_covered_output is not None
    assert not_covered_output["code"] == FailureCode.NOT_COVERED.value

    # Пустой результат поиска строится другим типом отказа (`not_found`), а не
    # `not_covered` — коды у них разные по конструкции, не только по тексту.
    empty_result = NotFound(identifier="122-VIII", id_format_hint="номер закону")
    assert empty_result.as_output()["code"] == FailureCode.NOT_FOUND.value
    assert empty_result.as_output()["code"] != not_covered_output["code"]


def test_a_known_legal_order_without_a_wired_adapter_is_not_covered_not_found() -> None:
    """Правопорядок числится в карте как connected, но адаптер не подключён.

    Смешивать это с «документ не знайдено» нельзя: юрист получил бы «такого
    документа немає» там, где на самом деле немає каналу зовсім.

    Условие создаётся явно, а не заимствуется у текущего состояния сборки:
    раньше тест опирался на то, что Phase 6 ещё не приехала, и стал ложным,
    как только адаптеры связали. Инвариант при этом никуда не делся — карта,
    обещающая подключённый источник без адаптера, обязана давать not_covered.
    """
    import sources

    # Реестр снимается целиком и возвращается целиком: `load_adapters` его не
    # восстанавливает — модули адаптеров уже импортированы, и повторный импорт
    # ничего не регистрирует. Украинские читатели вдобавок регистрируются при
    # импорте `server.py`, поэтому опустошение реестра без снимка оставляло
    # украинский канал без адаптера до конца прогона, и соседние тесты падали
    # в зависимости от того, кто первым импортировал сервер.
    saved = dict(sources._ADAPTERS)
    sources.clear_adapters()
    try:
        decision = routing.route("EU", subject="get_article")

        assert not decision.allowed
        assert decision.failure is not None
        assert decision.failure.as_output()["code"] == FailureCode.NOT_COVERED.value
    finally:
        sources.clear_adapters()
        for adapter in saved.values():
            sources.register_adapter(adapter)


def test_the_ukrainian_core_remains_routable() -> None:
    """Существующий канал не должен пострадать от заполнения карты покрытия."""
    decision = routing.route("UA", subject="get_article")
    assert decision.allowed
    assert decision.source is not None
    assert decision.source.legal_order == "UA"


def test_an_unknown_legal_order_gives_unknown_legal_order_not_not_covered() -> None:
    decision = routing.route("ZZ", subject="get_article")
    assert not decision.allowed
    assert decision.failure is not None
    assert decision.failure.as_output()["code"] == FailureCode.UNKNOWN_LEGAL_ORDER.value


# ---------------------------------------------------------------------------
# T050 — `reachable_by_id`: слой, которого достаточно чтению по идентификатору
# ---------------------------------------------------------------------------


def test_reachable_by_id_layer_is_requestable_separately_from_connected() -> None:
    """Чтению по идентификатору хватает reachable_by_id — вызывающий вправе
    сузить требуемые слои, не требуя connected там, где он не нужен."""
    only_connected = routing.route("ICJ", subject="get_decision", layers=(CoverageLayer.CONNECTED,))
    with_reachable = routing.route(
        "ICJ",
        subject="get_decision",
        layers=(CoverageLayer.REACHABLE_BY_ID,),
        # Реестр по умолчанию требует ещё и написанного адаптера (Phase 6);
        # здесь проверяется само сужение множества слоёв, поэтому источник
        # подставляется явно, в обход этой проверки — как и предусмотрено
        # `route`'s docstring для тестов.
        sources=legal_orders.sources_for("ICJ"),
    )

    assert not only_connected.allowed, "ICJ не имеет источника уровня connected"
    assert with_reachable.allowed, "ICJ имеет источник уровня reachable_by_id"
    assert with_reachable.source is not None
    assert with_reachable.source.layer is CoverageLayer.REACHABLE_BY_ID


# ---------------------------------------------------------------------------
# T054 — недоступность одного источника не влияет на остальные
# ---------------------------------------------------------------------------


def test_a_not_covered_legal_order_does_not_affect_routing_of_another() -> None:
    """Принцип IX: маршрутизация — чистая функция входа, без разделяемого
    состояния между вызовами для разных правопорядков."""
    before = routing.route("UA", subject="get_article")
    routing.route("UN", subject="get_article")  # UN не покрыт — не должен ничего испортить
    routing.route("ZZ", subject="get_article")  # неизвестный код — тоже
    after = routing.route("UA", subject="get_article")

    assert before.allowed and after.allowed
    assert before.source is not None and after.source is not None
    assert before.source.id == after.source.id


def test_every_legal_order_is_routed_independently_in_one_pass() -> None:
    """Прогон по всем правопорядкам не должен зависеть от порядка вызовов."""
    codes = ("UA", "EU", "ECHR", "ICJ", "UN")
    first_pass = {code: routing.route(code, subject="probe").allowed for code in codes}
    second_pass = {code: routing.route(code, subject="probe").allowed for code in reversed(codes)}
    assert first_pass == second_pass

    # Украинское ядро маршрутизируется; UN остаётся непокрытым и молча
    # покрытым не станет.
    assert first_pass["UA"] is True
    assert first_pass["UN"] is False


# ---------------------------------------------------------------------------
# T183 — карта показывает, есть ли за слоем работающий код
# ---------------------------------------------------------------------------


def test_the_map_says_whether_the_adapter_is_actually_wired() -> None:
    """Слой в карте и загруженный адаптер — разные вещи, и обе видны.

    Без признака загрузки карта показывала слой, а `route` отвечала «адаптер
    не подключён»: планировщик задачи обещал бы то, что маршрут потом отклонит.
    """
    import sources
    from sources.echr import EchrAdapter

    sources.register_adapter(EchrAdapter())
    rows = {row["id"]: row for row in legal_orders.describe_coverage("ECHR")}

    echr = rows["echr_hudoc"]
    assert echr["adapter_wired"] is True
    assert echr["id_format_hint"], "источник с адаптером обязан назвать форму идентификатора"
    assert echr["supports_date_filter"] is False, "HUDOC фильтр дат не подтвердил (ADR 0008)"


def test_a_source_without_a_registered_adapter_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    """Незагруженный адаптер назван прямо, а не выводится из пустого ответа.

    Реестр адаптеров подменяется, а не опустошается: `clear_adapters` в общем
    процессе оставил бы соседние тесты без источников — `load_adapters`
    ничего не восстановит, потому что модули уже импортированы.
    """
    import sources

    monkeypatch.setattr(sources, "get_adapter", lambda source_id: None)

    row = {r["id"]: r for r in legal_orders.describe_coverage("ECHR")}["echr_hudoc"]

    assert row["adapter_wired"] is False
    assert row["id_format_hint"] == ""
