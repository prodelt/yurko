"""Правопорядки, слои покрытия, источники и возможности операций (T006, T007, T120).

Правопорядок — ключевая ось всей системы и параметр каждого инструмента.
Отсюда следует главное свойство этого модуля: добавление правопорядка добавляет
значение в :data:`LEGAL_ORDERS`, а не набор инструментов (принцип VII, R-01).
При пяти правопорядках покомплектный подход дал бы под две сотни
инструментов, и качество их выбора моделью упало бы задолго до этого числа.

Реестр источников (:func:`register_source`) — контейнер, наполняемый картой
покрытия. Карта источников обязана быть единственным источником истины
о покрытии (FR-004).

Возможности операций (:class:`Capability`, T120, ADR 0002): покрытие выводится
из **проверенных** операций, а не из декларации слоя. Источник без ни одной
проверенной возможности для операции ведёт себя как ``not_covered`` для этой
операции, даже если в описании стоит ``connected``. Запланированный слой
существует только в документах; runtime знает лишь проверенное.

Валидаторы источника — это принцип III и решение R-12 в исполняемом виде:

* слой ``not_covered`` без ручного пути не конструируется — без него честного
  отказа не получится;
* лицензия с запретом коммерческого использования не может дать слой
  ``connected``;
* возможность слоя выше ``not_covered`` без даты проверки не конструируется.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = [
    "Capability",
    "CitationFormat",
    "CitationPatternKind",
    "CoverageLayer",
    "DocumentClass",
    "KNOWN_LEGAL_ORDER_CODES",
    "LEGAL_ORDERS",
    "LegalOrder",
    "LegalOrderKind",
    "Operation",
    "Source",
    "SourceAccess",
    "SourceLimits",
    "capabilities_for",
    "capabilities_of",
    "describe_coverage",
    "get_legal_order",
    "get_source",
    "is_known_legal_order",
    "normalize_legal_order_code",
    "register_capability",
    "register_default_capabilities",
    "register_default_sources",
    "register_source",
    "registered_capabilities",
    "registered_sources",
    "sources_for",
    "verified_layer",
]


class LegalOrderKind(str, Enum):
    """Государство, наднациональное объединение, международный форум или
    международное договорное право.

    ``international_forum`` не имеет государства; ``international_law`` (INTL,
    ADR 0004) — право договоров между государствами: документ относится к нему,
    а не к стране издателя публикации.
    """

    STATE = "state"
    SUPRANATIONAL = "supranational"
    INTERNATIONAL_FORUM = "international_forum"
    INTERNATIONAL_LAW = "international_law"


class CoverageLayer(str, Enum):
    """Слой покрытия — свойство источника, определяющее поведение системы.

    ``connected`` — документ ищется и читается через адаптер;
    ``reachable_by_id`` — выдаётся при известном идентификаторе;
    ``not_covered`` — ответ по существу запрещён, выдаётся ручной путь.
    """

    CONNECTED = "connected"
    REACHABLE_BY_ID = "reachable_by_id"
    NOT_COVERED = "not_covered"


_LAYER_RANK = {
    CoverageLayer.NOT_COVERED: 0,
    CoverageLayer.REACHABLE_BY_ID: 1,
    CoverageLayer.CONNECTED: 2,
}


class SourceAccess(str, Enum):
    """Способ, которым источник вообще отдаёт данные."""

    OFFICIAL_API = "official_api"
    UNOFFICIAL_INTERFACE = "unofficial_interface"
    PAGE_SCRAPE = "page_scrape"
    OFFICIAL_PAGE = "official_page"
    OFFICIAL_FILE = "official_file"
    CURATED_SET = "curated_set"
    NONE = "none"


class Operation(str, Enum):
    """Операция над источником, для которой проверяется возможность (FR-203)."""

    READ_DOCUMENT = "read_document"
    READ_FRAGMENT = "read_fragment"
    REVISION_AS_OF = "revision_as_of"
    LIST_REVISIONS = "list_revisions"
    CARD = "card"
    SEARCH_BY_IDENTIFIER = "search_by_identifier"
    SEARCH_FREE_TEXT = "search_free_text"
    TREATY_STATUS = "treaty_status"
    PUBLIC_CASE_CARD = "public_case_card"
    #: Структурный поиск «документы, которые цитируют акт» (T-cites): вход —
    #: CELEX цитируемого акта, а не текст и не номер дела искомого документа —
    #: поэтому это отдельная операция, а не `search_by_identifier`.
    SEARCH_BY_CITATION = "search_by_citation"


class DocumentClass(str, Enum):
    """Вид публичного документа, к которому относится возможность."""

    ACT = "act"
    DECISION = "decision"
    TREATY = "treaty"
    PROCEDURE = "procedure"
    GUIDANCE = "guidance"
    REGISTRY_RECORD = "registry_record"
    ANY = "any"


class CitationPatternKind(str, Enum):
    """Как в форуме устроена ссылка — и, значит, как её разбирать."""

    ACT_IDENTIFIER = "act_identifier"
    DECISION_IDENTIFIER = "decision_identifier"
    REPORTER_PAGE_PARA = "reporter_page_para"
    CASE_NUMBER_DATE = "case_number_date"


class CitationFormat(BaseModel):
    """Формат ссылки, принятый в форуме.

    Не менее двух реальных примеров: один пример не показывает, что в ссылке
    переменное, а что постоянное, и разбор по нему получается угаданный.
    """

    model_config = ConfigDict(frozen=True)

    forum: str = Field(min_length=1)
    pattern_kind: CitationPatternKind
    examples: tuple[str, ...] = Field(min_length=2)

    @field_validator("examples")
    @classmethod
    def examples_are_distinct_and_non_empty(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(item.strip() for item in value)
        if any(not item for item in cleaned):
            raise ValueError("пустая строка не является примером ссылки")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("два одинаковых примера — это один пример")
        return cleaned


class LegalOrder(BaseModel):
    """Право какого сообщества применяется."""

    model_config = ConfigDict(frozen=True)

    code: str = Field(min_length=2, max_length=8)
    kind: LegalOrderKind
    name_uk: str = Field(min_length=1)
    name_native: str = Field(min_length=1)
    #: Языки, в которых публикуется аутентичный текст **по общему правилу**
    #: правопорядка. Аутентичность конкретного документа определяется самим
    #: документом (ADR 0007): договор может назначить иной язык превалирующим.
    authentic_languages: tuple[str, ...] = Field(min_length=1)
    citation_style: CitationFormat

    @field_validator("code")
    @classmethod
    def code_is_upper_case(cls, value: str) -> str:
        return value.strip().upper()

    @model_validator(mode="after")
    def code_shape_matches_the_kind(self) -> "LegalOrder":
        """Государство — двухбуквенный код; форум — мнемонический.

        Длина не различает их: ``EU`` тоже двухбуквенный. Различает то, что код
        государства обязан быть кодом государства по ISO 3166-1 alpha-2, а
        мнемоника форума — нет, поэтому здесь проверяется только форма кода
        государства, а не длина мнемоники.
        """
        if not self.code.isalpha() or not self.code.isupper():
            raise ValueError(f"{self.code}: код правопорядка — заглавные буквы без разделителей")
        if self.kind is LegalOrderKind.STATE and len(self.code) != 2:
            raise ValueError(
                f"{self.code}: государство обозначается двухбуквенным кодом ISO 3166-1"
            )
        return self


class SourceLimits(BaseModel):
    """Известные ограничения источника.

    Ограничение выдачи обходится постранично либо сопровождается сообщением
    о неполноте; усечённый результат не выдаётся за полный.
    """

    model_config = ConfigDict(frozen=True)

    max_records_per_request: int | None = None
    rate_limit: str | None = None
    technical_restrictions: str | None = None
    notes: str | None = None


class Source(BaseModel):
    """Внешний официальный поставщик данных."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(min_length=1)
    legal_order: str = Field(min_length=2)
    #: Декларированный (целевой) слой. Runtime использует :func:`verified_layer`.
    layer: CoverageLayer
    access: SourceAccess
    license: str = Field(min_length=1)
    license_forbids_commercial: bool = False
    attribution_required: bool = False
    attribution: str | None = None
    limits: SourceLimits = Field(default_factory=SourceLimits)
    manual_path: str | None = None
    source_url: str = Field(min_length=1)
    description: str = ""
    #: Адаптер, обслуживающий источник; по умолчанию совпадает с ``id``.
    #: Коллекция (напр. публичная карточка дела Суда ЕС) указывает на адаптер ЕС
    #: (ADR 0002), а не заводит второй читатель.
    adapter_id: str | None = None

    @field_validator("legal_order")
    @classmethod
    def legal_order_is_known(cls, value: str) -> str:
        code = normalize_legal_order_code(value)
        if code not in LEGAL_ORDERS:
            raise ValueError(
                f"неизвестный правопорядок {value!r}; известные: "
                f"{', '.join(KNOWN_LEGAL_ORDER_CODES)}"
            )
        return code

    @field_validator("manual_path", "attribution", "adapter_id")
    @classmethod
    def blank_is_absent(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    @model_validator(mode="after")
    def not_covered_requires_a_manual_path(self) -> "Source":
        if self.layer is CoverageLayer.NOT_COVERED and not self.manual_path:
            raise ValueError(
                f"источник {self.id!r}: слой not_covered обязан нести ручной путь — "
                "без него честного отказа не получится (принцип III)"
            )
        return self

    @model_validator(mode="after")
    def non_commercial_licence_cannot_be_connected(self) -> "Source":
        if self.license_forbids_commercial and self.layer is CoverageLayer.CONNECTED:
            raise ValueError(
                f"источник {self.id!r}: лицензия {self.license!r} запрещает коммерческое "
                "использование, поэтому подключённый слой ему недоступен (R-12)"
            )
        return self

    @model_validator(mode="after")
    def required_attribution_must_have_a_text(self) -> "Source":
        if self.attribution_required and not self.attribution:
            raise ValueError(
                f"источник {self.id!r}: атрибуция объявлена обязательной, но её текст пуст — "
                "воспроизводить в ответе нечего"
            )
        return self

    @property
    def answers_on_the_merits(self) -> bool:
        """Можно ли по этому источнику отвечать по существу (по проверенным операциям)."""
        return verified_layer(self) is not CoverageLayer.NOT_COVERED

    @property
    def effective_adapter_id(self) -> str:
        return self.adapter_id or self.id

    def truncation_failure(self, received: int) -> Any:
        """Отказ ``partial_result``, если ``received`` достиг лимита источника.

        ``None``, если лимита нет или выдача под него не попала — усечённый
        результат не может быть подан за полный (раздел «Ограничения
        источников» конституции), но и не усечённая выдача не обязана нести
        отказ, которого не заслуживает (T048, T053).

        Отложенный импорт :class:`contracts.PartialResult` — контракт уже
        импортирует этот модуль на уровне модуля, прямой импорт наверху
        замкнул бы цикл.
        """
        limit = self.limits.max_records_per_request
        if limit is None or received < limit:
            return None
        from core.contracts import PartialResult

        return PartialResult(
            received=received,
            limited_by=(
                f"джерело {self.id} обмежує видачу {limit} записами на запит"
                + (f" ({self.limits.notes})" if self.limits.notes else "")
            ),
        )


class Capability(BaseModel):
    """Проверенная возможность операции над источником (T120, FR-203).

    Слой выше ``not_covered`` без даты и способа проверки не конструируется:
    «планируется» — не runtime-состояние.
    """

    model_config = ConfigDict(frozen=True)

    source_id: str = Field(min_length=1)
    operation: Operation
    document_class: DocumentClass = DocumentClass.ANY
    layer: CoverageLayer
    verified_at: str | None = None
    verified_by: str = ""
    limitations: str = ""
    #: Требует ли операция отправки свободного текста источнику (ADR 0009).
    free_text_egress: bool = False

    @model_validator(mode="after")
    def a_covered_capability_names_its_verification(self) -> "Capability":
        if self.layer is not CoverageLayer.NOT_COVERED and not (self.verified_at or "").strip():
            raise ValueError(
                f"возможность {self.source_id}/{self.operation.value}: слой "
                f"{self.layer.value} без verified_at — запланированное не является проверенным"
            )
        return self

    @property
    def key(self) -> tuple[str, Operation, DocumentClass]:
        return (self.source_id, self.operation, self.document_class)


def _format(forum: str, kind: CitationPatternKind, *examples: str) -> CitationFormat:
    return CitationFormat(forum=forum, pattern_kind=kind, examples=tuple(examples))


#: 24 официальных языка ЕС — все равно аутентичны (ст. 55 ДЕС, Регламент № 1);
#: ``uk`` среди них нет (research/08).
_EU_LANGUAGES: tuple[str, ...] = (
    "bg", "cs", "da", "de", "el", "en", "es", "et", "fi", "fr", "ga", "hr",
    "hu", "it", "lt", "lv", "mt", "nl", "pl", "pt", "ro", "sk", "sl", "sv",
)  # fmt: skip


#: Пять правопорядков.
LEGAL_ORDERS: Mapping[str, LegalOrder] = {
    order.code: order
    for order in (
        LegalOrder(
            code="UA",
            kind=LegalOrderKind.STATE,
            name_uk="Україна",
            name_native="Україна",
            authentic_languages=("uk",),
            citation_style=_format(
                "UA",
                CitationPatternKind.ACT_IDENTIFIER,
                "стаття 5 Закону України «Про публічні закупівлі» № 922-VIII",
                "стаття 625 Цивільного кодексу України № 435-IV",
            ),
        ),
        LegalOrder(
            code="EU",
            kind=LegalOrderKind.SUPRANATIONAL,
            name_uk="Європейський Союз",
            name_native="European Union",
            authentic_languages=_EU_LANGUAGES,
            citation_style=_format(
                "EU",
                CitationPatternKind.ACT_IDENTIFIER,
                "32016R0679",
                "http://data.europa.eu/eli/reg/2016/679/oj",
            ),
        ),
        LegalOrder(
            code="ECHR",
            kind=LegalOrderKind.INTERNATIONAL_FORUM,
            name_uk="Європейський суд з прав людини",
            name_native="European Court of Human Rights",
            authentic_languages=("en", "fr"),
            citation_style=_format(
                "ECHR",
                CitationPatternKind.DECISION_IDENTIFIER,
                "ECLI:CE:ECHR:2018:1109JUD007140910",
                "Beuze v. Belgium [GC], no. 71409/10, 9 November 2018",
            ),
        ),
        LegalOrder(
            code="ICJ",
            kind=LegalOrderKind.INTERNATIONAL_FORUM,
            name_uk="Міжнародний суд ООН",
            name_native="International Court of Justice",
            authentic_languages=("en", "fr"),
            citation_style=_format(
                "ICJ",
                CitationPatternKind.REPORTER_PAGE_PARA,
                "I.C.J. Reports 1986, p. 14, para. 186",
                "I.C.J. Reports 1970, p. 3, para. 33",
            ),
        ),
        LegalOrder(
            code="UN",
            kind=LegalOrderKind.INTERNATIONAL_FORUM,
            name_uk="Договірна система ООН",
            name_native="United Nations treaty system",
            authentic_languages=("en", "fr", "es", "ru", "ar", "zh"),
            citation_style=_format(
                "UN",
                CitationPatternKind.ACT_IDENTIFIER,
                "United Nations, Treaty Series, vol. 1155, p. 331",
                "United Nations, Treaty Series, vol. 993, No. I-14531",
            ),
        ),
    )
}

#: Список, который уходит в отказ ``unknown_legal_order``.
KNOWN_LEGAL_ORDER_CODES: tuple[str, ...] = tuple(LEGAL_ORDERS)


def normalize_legal_order_code(value: Any) -> str:
    """Привести значение к виду кода правопорядка. Не проверяет известность."""
    return str(value or "").strip().upper()


def get_legal_order(code: Any) -> LegalOrder | None:
    """Вернуть правопорядок по коду или ``None``.

    Неизвестный код — это отказ ``unknown_legal_order``, который строит
    вызывающий; исключение здесь превратило бы ответ в аварию.
    """
    return LEGAL_ORDERS.get(normalize_legal_order_code(code))


def is_known_legal_order(code: Any) -> bool:
    return normalize_legal_order_code(code) in LEGAL_ORDERS


# ---------------------------------------------------------------------------
# Реестр источников
# ---------------------------------------------------------------------------

_SOURCES: dict[str, Source] = {}
_CAPABILITIES: dict[tuple[str, Operation, DocumentClass], Capability] = {}


def register_source(source: Source) -> Source:
    """Внести источник в реестр. Повторная регистрация заменяет запись."""
    _SOURCES[source.id] = source
    return source


def get_source(source_id: str) -> Source | None:
    return _SOURCES.get(str(source_id or "").strip())


def sources_for(legal_order: Any) -> tuple[Source, ...]:
    """Все источники правопорядка в порядке убывания полезности проверенного слоя.

    Порядок значим: маршрутизация берёт первый подходящий, а подключённый слой
    отвечает лучше, чем достижимый по идентификатору.
    """
    code = normalize_legal_order_code(legal_order)
    matching = [source for source in _SOURCES.values() if source.legal_order == code]
    return tuple(
        sorted(
            matching,
            key=lambda source: (
                -_LAYER_RANK[verified_layer(source)],
                -_LAYER_RANK[source.layer],
                source.id,
            ),
        )
    )


def registered_sources() -> tuple[Source, ...]:
    return tuple(_SOURCES[key] for key in sorted(_SOURCES))


def clear_sources() -> None:
    """Опустошить реестр. Для тестов, которым нужна известная точка отсчёта."""
    _SOURCES.clear()


# ---------------------------------------------------------------------------
# Реестр возможностей (T120)
# ---------------------------------------------------------------------------


def register_capability(capability: Capability) -> Capability:
    """Внести проверенную возможность. Повторная регистрация заменяет запись."""
    _CAPABILITIES[capability.key] = capability
    return capability


def registered_capabilities() -> tuple[Capability, ...]:
    return tuple(
        _CAPABILITIES[key]
        for key in sorted(_CAPABILITIES, key=lambda k: (k[0], k[1].value, k[2].value))
    )


def capabilities_of(source_id: str) -> tuple[Capability, ...]:
    wanted = str(source_id or "").strip()
    return tuple(cap for cap in registered_capabilities() if cap.source_id == wanted)


def capabilities_for(
    legal_order: Any,
    operation: Operation,
    document_class: DocumentClass = DocumentClass.ANY,
) -> tuple[Capability, ...]:
    """Возможности операции по правопорядку, лучшие слои первыми.

    Возможность класса ``any`` подходит любому виду документа; возможность
    конкретного класса — только ему.
    """
    code = normalize_legal_order_code(legal_order)
    source_ids = {source.id for source in _SOURCES.values() if source.legal_order == code}
    found = [
        cap
        for cap in _CAPABILITIES.values()
        if cap.source_id in source_ids
        and cap.operation is operation
        and (
            cap.document_class is DocumentClass.ANY
            or document_class is DocumentClass.ANY
            or cap.document_class is document_class
        )
    ]
    return tuple(sorted(found, key=lambda cap: (-_LAYER_RANK[cap.layer], cap.source_id)))


def verified_layer(source: Source) -> CoverageLayer:
    """Слой, доказанный проверенными возможностями, — то, что видит runtime."""
    best = CoverageLayer.NOT_COVERED
    for cap in _CAPABILITIES.values():
        if cap.source_id == source.id and _LAYER_RANK[cap.layer] > _LAYER_RANK[best]:
            best = cap.layer
    return best


def _adapter_facts(source: Source) -> dict[str, Any]:
    """Что известно об источнике из его адаптера — до попытки чтения (T183).

    Слой в карте и работающий код — разные вещи: маршрутизация проверяет ещё
    и то, зарегистрирован ли адаптер, поэтому карта могла показать слой, а
    ``route`` ответить «адаптер не подключён». Планировщик задачи, строящий
    план по карте, обещал бы недостижимое. Формат идентификатора и поддержка
    фильтра дат до этого были видны только внутри отказа, то есть узнавались
    ошибкой.

    Импорт реестра адаптеров локальный: ``routing`` импортирует этот модуль,
    и обратный импорт наверху дал бы цикл.
    """
    from sources import get_adapter

    adapter = get_adapter(source.effective_adapter_id)
    # Украинское ядро подключено кодом, не подчиняющимся контракту адаптеров;
    # признак загрузки для него считается так же, как в маршрутизации.
    wired = adapter is not None or source.legal_order == "UA"
    return {
        "adapter_wired": wired,
        "id_format_hint": str(getattr(adapter, "id_format_hint", "") or ""),
        "supports_date_filter": bool(getattr(adapter, "supports_date_filter", False)),
    }


def describe_coverage(legal_order: Any = None) -> tuple[dict[str, Any], ...]:
    """Карта покрытия в виде, готовом для инструмента ``list_coverage`` (T029).

    ``layer`` — проверенный слой (по возможностям), ``declared_layer`` —
    декларированный. Расхождение между ними видно юристу, а не скрыто.
    ``adapter_wired`` — есть ли за слоем работающий код: слой без адаптера
    отвечает отказом маршрута, и знать об этом надо до плана, а не после.
    """
    if legal_order and normalize_legal_order_code(legal_order) != "*":
        code = normalize_legal_order_code(legal_order)
        sources = sources_for(code)
    else:
        sources = registered_sources()
    return tuple(
        {
            "id": source.id,
            "legal_order": source.legal_order,
            "layer": verified_layer(source).value,
            "declared_layer": source.layer.value,
            "access": source.access.value,
            "license": source.license,
            "license_forbids_commercial": source.license_forbids_commercial,
            "attribution_required": source.attribution_required,
            "attribution": source.attribution,
            "limits": source.limits.model_dump(mode="json"),
            "manual_path": source.manual_path,
            "source_url": source.source_url,
            "description": source.description,
            "adapter_id": source.effective_adapter_id,
            **_adapter_facts(source),
            "answers_on_the_merits": source.answers_on_the_merits,
            "capabilities": [
                {
                    "operation": cap.operation.value,
                    "document_class": cap.document_class.value,
                    "layer": cap.layer.value,
                    "verified_at": cap.verified_at,
                    "verified_by": cap.verified_by,
                    "limitations": cap.limitations,
                    "free_text_egress": cap.free_text_egress,
                }
                for cap in capabilities_of(source.id)
            ],
        }
        for source in sources
    )


# ---------------------------------------------------------------------------
# T025–T028, T066, T120 — карта источников
#
# Каждая запись со ссылкой на срез исследования, из которого взят факт
# (`specs/001-eu-intl-law-expansion/research/01`–`06`,
# `specs/002-public-law-evidence/research/07`–`13`), а не на общее
# представление о том, что в ЕС открыто.
# ---------------------------------------------------------------------------

_EU_ATTRIBUTION = (
    "© Європейський Союз, https://eur-lex.europa.eu/ — відтворення дозволене за "
    "умови зазначення джерела (Commission Decision 2011/833/EU)"
)

_DEFAULT_SOURCES: tuple[Source, ...] = (
    # -- T025: право ЕС (research/01-eu-sources.md) -------------------------
    Source(
        id="eu_law_eurlex_cellar",
        legal_order="EU",
        layer=CoverageLayer.CONNECTED,
        access=SourceAccess.OFFICIAL_API,
        license="Commission Decision 2011/833/EU (де-факто CC BY 4.0)",
        attribution_required=True,
        attribution=_EU_ATTRIBUTION,
        limits=SourceLimits(
            max_records_per_request=10_000,
            notes=(
                "Ліміт 10 000 записів на пошуковий запит діє з 01.01.2026 "
                "(eur-lex.europa.eu/content/help/data-reuse/webservice.html)"
            ),
        ),
        manual_path=(
            "EUR-Lex вручну: https://eur-lex.europa.eu/ — пошук за CELEX/ELI, "
            "консолідовані тексти в розділі Consolidated texts"
        ),
        source_url="https://publications.europa.eu/webapi/rdf/sparql",
        description=(
            "EUR-Lex через Cellar SPARQL (без ключа, >2.7 млн work-записів) та REST "
            "з content negotiation до Akoma Ntoso/XHTML на рівні статті; ідентифікатори "
            "CELEX (мовонезалежний) та ELI зі stable URI й subdivisions"
        ),
    ),
    Source(
        id="eu_case_law_cellar",
        legal_order="EU",
        layer=CoverageLayer.CONNECTED,
        access=SourceAccess.OFFICIAL_API,
        license="Commission Decision 2011/833/EU (де-факто CC BY 4.0)",
        attribution_required=True,
        attribution=_EU_ATTRIBUTION,
        manual_path="InfoCuria вручну: https://curia.europa.eu/juris/ за номером справи або ECLI",
        source_url="https://publications.europa.eu/webapi/rdf/sparql",
        description=(
            "Практика Суду ЄС: власного офіційного API/bulk у CURIA (InfoCuria) "
            "немає, фактичний шлях — через EUR-Lex/Cellar SPARQL. ECLI присвоєно "
            "всім рішенням з 1954; 57 103 судові акти станом на 07.2026 (34 261 "
            "JUDG, 8 362 ORDER, 14 480 OPIN_AG)"
        ),
    ),
    Source(
        id="eu_cjeu_public_case_card",
        legal_order="EU",
        layer=CoverageLayer.REACHABLE_BY_ID,
        access=SourceAccess.OFFICIAL_PAGE,
        license="умови повторного використання curia.europa.eu; лише публічні відомості",
        manual_path=(
            "Публічна картка провадження вручну: "
            "https://juris.curia.europa.eu/juris/liste.jsf?num=C-NNN/YY&language=en "
            "(curia.europa.eu/juris/ з 2026 перенаправляє в SPA InfoCuria) та повідомлення "
            "в OJ C через EUR-Lex; закриті відомості e-Curia не читаються"
        ),
        source_url="https://juris.curia.europa.eu/juris/",
        description=(
            "Публічна картка відкритого провадження Суду ЄС за номером справи: "
            "опубліковані документи й посилання з датою отримання; "
            "відсутність публікації не означає неподання. "
            "Канали: Cellar SPARQL, а коли публікації там немає — легасі-картка CURIA "
            "juris.curia.europa.eu (дата подання, суд, предмет, мова, документи; "
            "перевірено живою пробою 15.09.2026)"
        ),
        adapter_id="eu_case_law_cellar",
    ),
    Source(
        id="eu_registry_ted",
        legal_order="EU",
        layer=CoverageLayer.REACHABLE_BY_ID,
        access=SourceAccess.OFFICIAL_API,
        license="EU reuse policy для відкритих даних; ключ потрібен лише для непублікованих",
        limits=SourceLimits(
            notes=(
                "~700 тис. повідомлень/рік, ≈€700 млрд; XML bulk дозволено robots.txt, "
                "динамічні пошукові URL — заблоковано"
            )
        ),
        manual_path="https://ted.europa.eu вручну за номером повідомлення",
        source_url="https://docs.ted.europa.eu",
        description="TED + eForms — реєстр публічних закупівель ЄС",
    ),
    Source(
        id="eu_registry_euipo",
        legal_order="EU",
        layer=CoverageLayer.REACHABLE_BY_ID,
        access=SourceAccess.OFFICIAL_API,
        license="EUIPO open data terms; безкоштовна реєстрація, OAuth2",
        manual_path="https://euipo.europa.eu/eSearch/ вручну",
        source_url="https://www.euipo.europa.eu",
        description=("Реєстр торговельних марок ЄС: 3 221 315 заявок TM з 1996"),
    ),
    Source(
        id="eu_registry_epo_ops",
        legal_order="EU",
        layer=CoverageLayer.REACHABLE_BY_ID,
        access=SourceAccess.OFFICIAL_API,
        license="EPO Open Patent Services terms; OAuth2",
        limits=SourceLimits(max_records_per_request=2_000, rate_limit="4 ГБ/тиждень безкоштовно"),
        manual_path="https://worldwide.espacenet.com вручну",
        source_url="https://www.epo.org/en/searching-for-patents/data/web-services/ops",
        description="EPO OPS — реєстр патентних заявок",
    ),
    Source(
        id="eu_registry_gleif",
        legal_order="EU",
        layer=CoverageLayer.REACHABLE_BY_ID,
        access=SourceAccess.OFFICIAL_API,
        license="CC0",
        limits=SourceLimits(max_records_per_request=200, notes="+ повний дамп без обмежень"),
        manual_path="https://search.gleif.org вручну за LEI",
        source_url="https://www.gleif.org",
        description="GLEIF — реєстр LEI, без реєстрації",
    ),
    # -- T026: украинское ядро — существующий живой канал --------------------
    Source(
        id="ua_rada_open_data",
        legal_order="UA",
        layer=CoverageLayer.CONNECTED,
        access=SourceAccess.OFFICIAL_API,
        license=(
            "нормативно-правові акти не є об'єктом авторського права "
            "(ст. 8 Закону України «Про авторське право і суміжні права»)"
        ),
        manual_path="https://zakon.rada.gov.ua/laws/show/<id> вручну",
        source_url="https://zakon.rada.gov.ua",
        description=(
            "Живий канал відкритих даних Верховної Ради України (rada_open_data.py) "
            "з фолбеком на друковану сторінку — існуюче українське ядро, тут лише "
            "внесене до карти покриття"
        ),
    ),
    Source(
        id="ua_court_register_edrsr",
        legal_order="UA",
        layer=CoverageLayer.CONNECTED,
        access=SourceAccess.OFFICIAL_PAGE,
        license="Єдиний державний реєстр судових рішень — відкритий доступ (ЗУ № 3262-IV)",
        limits=SourceLimits(
            technical_restrictions="антибот-заглушка на частині запитів; форма пошуку HTML"
        ),
        manual_path="https://reyestr.court.gov.ua/ вручну за номером справи або id рішення",
        source_url="https://reyestr.court.gov.ua",
        description=(
            "ЄДРСР: пошук за номером справи та читання рішення за id /Review/<id> "
            "(court_registry.py); незалежний від Ради та ембедингів канал"
        ),
    ),
    Source(
        id="ua_debtors_register",
        legal_order="UA",
        layer=CoverageLayer.CONNECTED,
        access=SourceAccess.OFFICIAL_API,
        license="Єдиний реєстр боржників — відкриті дані Мін'юсту",
        manual_path="https://erb.minjust.gov.ua вручну",
        source_url="https://erb.minjust.gov.ua",
        description="Єдиний реєстр боржників (state_registries.py); пошук за кодом/назвою",
    ),
    Source(
        id="ua_prozorro",
        legal_order="UA",
        layer=CoverageLayer.CONNECTED,
        access=SourceAccess.OFFICIAL_API,
        license="Prozorro public API — відкриті дані",
        manual_path="https://prozorro.gov.ua вручну за номером закупівлі",
        source_url="https://public-api.prozorro.gov.ua",
        description="Prozorro (state_registries.py): тендер за номером/ідентифікатором",
    ),
    Source(
        id="ua_open_data_catalog",
        legal_order="UA",
        layer=CoverageLayer.CONNECTED,
        access=SourceAccess.OFFICIAL_API,
        license="data.gov.ua CKAN — відкриті дані",
        manual_path="https://data.gov.ua вручну",
        source_url="https://data.gov.ua",
        description="Каталог відкритих даних (state_registries.py); пошук наборів",
    ),
    # -- T027: международные источники ---------------------------------------
    Source(
        id="echr_hudoc",
        legal_order="ECHR",
        layer=CoverageLayer.REACHABLE_BY_ID,
        access=SourceAccess.UNOFFICIAL_INTERFACE,
        license="офіційна ліцензія не підтверджена; дані у відкритому доступі на hudoc.echr.coe.int",
        manual_path=(
            "https://hudoc.echr.coe.int — ручний пошук за номером справи, ECLI або назвою "
            "сторін; офіційного API немає, інтерфейс може змінитися без попередження"
        ),
        source_url="https://hudoc.echr.coe.int",
        description=(
            "~50 000 справ; офіційного API немає, фактичний доступ — де-факто JSON "
            "через URL-запити. ECLI — поле пошуку. Рішення, окрема думка, резюме та "
            "переклади розрізняються"
        ),
    ),
    Source(
        id="icj_official_archive",
        legal_order="ICJ",
        layer=CoverageLayer.REACHABLE_BY_ID,
        access=SourceAccess.OFFICIAL_FILE,
        license="офіційний архів icj-cij.org; ліцензія на повторне використання не вказана",
        manual_path=(
            "https://www.icj-cij.org/case/<номер справи> вручну; офіційний PDF рішення "
            "з розділу case-related/<номер справи>/"
        ),
        source_url="https://www.icj-cij.org",
        description=(
            "Офіційного API немає; читання офіційного PDF за ідентифікатором документа "
            "з витягом параграфів. Академічний корпус CD-ICJ — довідковий, не офіційне "
            "джерело"
        ),
    ),
    Source(
        id="intl_arbitration_pca_icsid",
        legal_order="UN",
        layer=CoverageLayer.NOT_COVERED,
        access=SourceAccess.NONE,
        license="доступу немає; ICSID з 2026 інтегровано в Jus Mundi (комерція)",
        manual_path=(
            "PCA: pcacases.com/web/search — публікація процесуальних документів лише "
            "за згодою сторін. ICSID: власна база, з 2026 контент — через Jus Mundi "
            "(платно)"
        ),
        source_url="https://pcacases.com",
        description=("Постійна палата третейського суду та ICSID — публічного API немає"),
    ),
)


def register_default_sources() -> tuple[Source, ...]:
    """(Пере)наполнить реестр каталогом, описанным в этом модуле.

    Идемпотентно: :func:`register_source` заменяет запись по ``id``, поэтому
    повторный вызов после чужого ``clear_sources()`` восстанавливает ровно
    это состояние, а не накапливает дубликаты.
    """
    for source in _DEFAULT_SOURCES:
        register_source(source)
    return _DEFAULT_SOURCES


# ---------------------------------------------------------------------------
# Проверенные возможности (T120). Каждая запись — факт приёмки: дата, кем и
# каким прогоном. Без записи здесь операция для источника не покрыта, что бы
# ни говорил декларированный слой. Даты и способ приёмки — .planning/release-002-live.md
# и specs/002-public-law-evidence/research/12–13.
# ---------------------------------------------------------------------------

_LIVE_ACCEPTANCE = "2026-09-07"
#: Вторая живая приёмка (набор 004): проверкой считается прогон, в выдаче
#: которого есть заведомо известный документ, а не факт ответа источника.
#: Прогон 2026-09-07 таким не был — отсюда и находки T215/T216.
_RELEVANCE_ACCEPTANCE = "2026-09-08"
_RELEVANCE_BY = "координатор 004, живий прогін із перевіркою релевантності видачі"
_ACCEPTED_BY = "координатор 002, живий прогон з локальної машини"
#: Третья приёмка (T249). Аудит 2026-09-09 показал записи, у которых стоит
#: ``verified_at``, а прогона за ним в репозитории нет ни одного: операции без
#: инструмента, источники, которых маршрутизация никогда не выбирает, и реестры,
#: живой вызов которых давал ``source_unavailable``. Каждая такая запись здесь
#: либо получила пробу с выводом источника, либо опущена — скрипт
#: ``scripts/probe_registry.py``.
_PROBE_ACCEPTANCE = "2026-09-09"
_PROBE_BY = "ведучий 004, проба scripts/probe_registry.py з виводом джерела"
#: Приёмка T-cites (2026-09-09): «документи, що цитують акт» через сам
#: інструмент `search_decisions(cites=...)`, а не сирим SPARQL повз нього.
_CITES_ACCEPTANCE = "2026-09-09"
_CITES_BY = (
    "ведучий 003, живий прогін search_decisions(legal_order='EU', "
    "cites=<CELEX регламенту>) через MCP-інструмент"
)
#: Пошук рішень Суду ЄС за номером справи (тікет 47): живий прогін через сам
#: інструмент, а не сирим SPARQL повз нього.
_CASE_NUMBER_ACCEPTANCE = "2026-09-29"
_CASE_NUMBER_BY = (
    "живий прогін search_decisions(legal_order='EU', case_number=...) через "
    "MCP-інструмент на чотирьох номерах справ Суду і Загального суду, з суфіксом "
    "виду провадження і без: номер → CELEX, ECLI, дата й назва справи"
)
#: Приёмка T-icj-card (2026-09-09): get_case("ICJ", <номер справи>) через сам
#: MCP-инструмент — после починки расхождения имени операции (public_case_card
#: у маршрутизации, card в реестре).
_ICJ_PUBLIC_CARD_ACCEPTANCE = "2026-09-09"
_ICJ_PUBLIC_CARD_BY = (
    "ведучий 003, живий прогін get_case(legal_order='ICJ', case_number=<номер>) "
    "через MCP-інструмент після виправлення відповідності операцій"
)
#: Приймання другого каналу картки Суду ЄС (2026-09-15): get_case через сам
#: інструмент у коді гілки. Відкрита справа без публікацій → curia_legacy_card
#: з датою подання, судом, предметом і мовою, 0 документів; C-605/26 → 1
#: документ (запит 2026-06-05, PDF); опублікована справа → cellar_sparql;
#: C-9999/26 → case_found=false.
_CJEU_CARD_ACCEPTANCE = "2026-09-15"
_CJEU_CARD_BY = (
    "інженер mcp/eu-case-card-cellar, живий прогін get_case(legal_order='eu', "
    "case_number='C-605/26' | опублікована справа | 'C-9999/26') з локальної машини"
)

#: Тікет 21: картка документа EU з Cellar SPARQL.
_EU_CARD_ACCEPTANCE = "2026-09-16"
_EU_CARD_BY = (
    "інженер mcp/eli-eu-metadata, живий прогін get_law_metadata(legal_order='eu', "
    "document_id='32024Q02173') з локальної машини"
)


def _cap(
    source_id: str,
    operation: Operation,
    document_class: DocumentClass = DocumentClass.ANY,
    *,
    layer: CoverageLayer = CoverageLayer.REACHABLE_BY_ID,
    # Умолчания здесь нет намеренно (аудит 2026-09-09). Со значением по
    # умолчанию новая запись становилась «проверенной» датой чужой приёмки
    # без единого прогона за собой: так одна из записей `read_fragment`
    # числилась проверенной 2026-09-07 координатором 002, хотя первый живой
    # прогон этой операции состоялся 2026-09-09.
    # Обязательный аргумент заставляет назвать дату и прогон, а `None` —
    # прямо сказать «не проверено».
    verified_at: str | None,
    verified_by: str = _ACCEPTED_BY,
    limitations: str = "",
    free_text_egress: bool = False,
) -> Capability:
    # Умолчание ``verified_by`` называет приёмку 002 от 2026-09-07 поимённо,
    # поэтому оно вправе стоять только рядом с этой датой. Иначе запись,
    # добавленная с новой датой и без своего проверяющего, молча приписывала бы
    # прогон координатору 002 — половина того самого дефекта, ради которого у
    # ``verified_at`` убрано умолчание (аудит рецензента 2026-09-09).
    if verified_by == _ACCEPTED_BY and verified_at not in (None, _LIVE_ACCEPTANCE):
        raise ValueError(
            f"{source_id}/{operation.value}: verified_at={verified_at!r} без своего "
            "verified_by. Назовите, кто и каким прогоном принял эту возможность: "
            "умолчание принадлежит приёмке 002 от " + _LIVE_ACCEPTANCE
        )
    return Capability(
        source_id=source_id,
        operation=operation,
        document_class=document_class,
        layer=layer,
        verified_at=verified_at,
        verified_by="" if verified_at is None else verified_by,
        limitations=limitations,
        free_text_egress=free_text_egress,
    )


_CONNECTED = CoverageLayer.CONNECTED
_REACHABLE = CoverageLayer.REACHABLE_BY_ID
_NOT_COVERED = CoverageLayer.NOT_COVERED

_DEFAULT_CAPABILITIES: tuple[Capability, ...] = (
    # -- UA: существующее ядро, живой канал измерен ранее и повторно 2026-09-07 --
    # Проба 2026-09-09: `query_law('UA', <id>)` вернул текст закона,
    # `get_law_metadata('UA', <id>)` — карточку с тем же id и url. До неё за
    # обеими записями стояла дата 2026-09-07, под которой чтений UA в журналах нет.
    _cap(
        "ua_rada_open_data",
        Operation.READ_DOCUMENT,
        DocumentClass.ACT,
        layer=_CONNECTED,
        verified_at=_PROBE_ACCEPTANCE,
        verified_by=_PROBE_BY,
    ),
    _cap(
        "ua_rada_open_data",
        Operation.READ_FRAGMENT,
        DocumentClass.ACT,
        layer=_CONNECTED,
        verified_at=_LIVE_ACCEPTANCE,
    ),
    _cap(
        "ua_rada_open_data",
        Operation.CARD,
        DocumentClass.ACT,
        layer=_CONNECTED,
        verified_at=_PROBE_ACCEPTANCE,
        verified_by=_PROBE_BY,
    ),
    # Рішення власника, записане тут навмисно (тікет 25). У Ради **два канали,
    # два хости й два профілі User-Agent, і вони не взаємозамінні**:
    #
    # * `data.rada.gov.ua` віддає тексти й масиви і відповідає лише на точний
    #   `User-Agent: OpenData`; будь-яке інше значення дає 302 на публічний сайт
    #   і 34 КБ оболонки замість закону. Сюди тікет 15 перевів читання й каталог,
    #   бо на `zakon.rada.gov.ua` стоїть `robots.txt: Disallow: /`.
    # * `zakon.rada.gov.ua` несе **єдиний робочий повнотекстовий пошук**, і на
    #   `OpenData` він відповідає 403. Браузерний UA там — не залишок старої
    #   машини й не обхід захисту, а єдиний спосіб, яким цей рушій узагалі
    #   відповідає.
    #
    # Тому браузерний UA на `zakon` лишається свідомо, і зводити обидва канали
    # до одного профілю не треба. Ризику для поставки він не несе: вільний текст
    # у профілі `legal` джерелу не надсилається взагалі (ADR 0009), а пошук по UA
    # йде локальним індексом; живий канал доступний лише в engineering-профілі.
    # Виміряна поведінка за цими виборами — `docs/research-rada-search.md`.
    _cap(
        "ua_rada_open_data",
        Operation.SEARCH_BY_IDENTIFIER,
        DocumentClass.ACT,
        layer=_CONNECTED,
        limitations=(
            "локальний індекс/каталог карток; живий повнотекстовий пошук — лише "
            "в engineering-профілі й лише на zakon.rada.gov.ua з браузерним UA"
        ),
        verified_at=_LIVE_ACCEPTANCE,
    ),
    # Опущено 2026-09-09 (T249). Запись стояла `connected` с датой 2026-09-07, но
    # прогона за ней нет и быть не могло: в профиле поставки `legal` свободный
    # текст источнику не отправляется вовсе, а `search_across_laws` для UA идёт в
    # локальный индекс, то есть в другой источник. Возможность источника этим не
    # опровергнута — опровергнуто утверждение, что продукт её использует.
    _cap(
        "ua_rada_open_data",
        Operation.SEARCH_FREE_TEXT,
        DocumentClass.ACT,
        layer=_NOT_COVERED,
        verified_at=None,
        free_text_egress=True,
        limitations=(
            "надсилає рядок запиту zakon.rada.gov.ua і тому в legal-профілі "
            "заблоковано; у поставці цим шляхом не ходять — пошук по "
            "UA йде локальним індексом. Живий канал доступний лише в "
            "engineering-профілі й окремим прогоном не перевірявся. "
            "Браузерний User-Agent на zakon.rada.gov.ua лишено свідомо: єдиний "
            "робочий повнотекстовий рушій живе саме там і на UA «OpenData» "
            "відповідає 403, тоді як читання й каталог ідуть на "
            "data.rada.gov.ua з «OpenData»"
        ),
    ),
    _cap(
        "ua_rada_open_data",
        Operation.REVISION_AS_OF,
        DocumentClass.ACT,
        layer=_NOT_COVERED,
        verified_at=None,
        limitations="редакція на дату для UA не реалізована; as_of → revision_unknown",
    ),
    # ЄДРСР: reyestr.court.gov.ua не приймає TCP-з'єднання на 443 і 80 з
    # перевіреної мережі (2026-09-07, дві незалежні проби). Живий виклик через
    # сервер дає source_unavailable. Форма пошуку й поля дат не спостерігалися
    # взагалі, тому жодна операція реєстру не заявляється як перевірена. Це стан
    # мережі на дату, а не доказ зникнення реєстру і не доказ відсутності
    # практики; потрібна інша мережа або ручний шлях (рішення власника).
    _cap(
        "ua_court_register_edrsr",
        Operation.READ_DOCUMENT,
        DocumentClass.DECISION,
        layer=_NOT_COVERED,
        verified_at=None,
        limitations=(
            "реєстр недоступний по TCP з перевіреної мережі — 2026-09-07 і "
            "повторно 2026-09-16 (з'єднання на 443 не встановлюється)"
        ),
    ),
    _cap(
        "ua_court_register_edrsr",
        Operation.SEARCH_BY_IDENTIFIER,
        DocumentClass.DECISION,
        layer=_NOT_COVERED,
        verified_at=None,
        limitations=(
            "реєстр недоступний по TCP з перевіреної мережі (2026-09-07); "
            "відсутність результату не доводить відсутності практики"
        ),
    ),
    _cap(
        "ua_court_register_edrsr",
        Operation.SEARCH_FREE_TEXT,
        DocumentClass.DECISION,
        layer=_NOT_COVERED,
        verified_at=None,
        free_text_egress=True,
        limitations="у legal-профілі заблоковано; реєстр недоступний з перевіреної мережі",
    ),
    # Опущены 2026-09-09 (T249). Обещание `connected` держалось на дате
    # 2026-09-07, под которой сам отчёт того дня писал обратное: живой вызов дал
    # `source_unavailable` и запись «надо либо подтвердить, либо понизить». Проба
    # 2026-09-09 через инструменты поставки повторила тот же результат дословно:
    # `search_debtors(code='00032129')` → `source_unavailable` «Debtors register
    # is temporarily unavailable», `search_tenders('UA-2024-01-15-000001')` →
    # `source_unavailable` «Prozorro search is temporarily unavailable». Это
    # состояние сети на две даты, а не вечный инвариант: вернётся источник —
    # вернётся и запись, но отдельным прогоном, а не по памяти.
    _cap(
        "ua_debtors_register",
        Operation.SEARCH_BY_IDENTIFIER,
        DocumentClass.REGISTRY_RECORD,
        layer=_NOT_COVERED,
        verified_at=None,
        limitations=(
            "за кодом юрособи або РНОКПП; реєстр працює, але пошук стереже "
            "reCAPTCHA v3. Проба 16.09.2026: фронтенд шле POST на "
            "/listDebtorsEndpoint із тілом {searchType, paging, filter, "
            "reCaptchaToken}, ключ капчі бере з /getConfig, а сервер токен "
            "перевіряє — без дійсного 403 «Forbidden» однаково на відсутній, "
            "порожній і підроблений. Капчу за людину не розв'язуємо, "
            "тож межа свідома, а не тимчасова. Попередні проби (07.09, 09.09) "
            "читали це як недоступність джерела через власний хибний URL "
            "/api/erbexternal/searchdebtorsexternal, якого сайт не кличе. "
            "Ручний шлях: https://erb.minjust.gov.ua у браузері"
        ),
    ),
    _cap(
        "ua_debtors_register",
        Operation.SEARCH_FREE_TEXT,
        DocumentClass.REGISTRY_RECORD,
        layer=_NOT_COVERED,
        verified_at=None,
        free_text_egress=True,
        limitations=(
            "за назвою; у legal-профілі заблоковано взагалі, тому в поставці "
            "не використовується, а джерело до того ж недоступне"
        ),
    ),
    # Тікет 24 (16.09.2026). Три попередні проби записали Prozorro в недоступні,
    # і причина щоразу вгадувалась, бо відмова була одна на всі випадки. Насправді
    # розбіжностей було три, і жодна не в джерелі: пошук порталу приймає POST, а
    # не GET (на GET — 405 сторінкою HTML); його хост `prozorro.gov.ua` не стояв
    # у списку сторожа виходу, тож запит не доходив до мережі; а читання картки
    # взагалі не мало оголошеної можливості, хоча працювало.
    _cap(
        "ua_prozorro",
        Operation.SEARCH_BY_IDENTIFIER,
        DocumentClass.REGISTRY_RECORD,
        layer=_REACHABLE,
        verified_at="2026-09-16",
        verified_by="жива проба 16.09.2026: UA-2026-09-14-005923-a знайдено, неіснуючий номер дав порожню видачу",
        limitations=(
            "за номером закупівлі UA-…; пошук порталу віддає сторінками по 20, "
            "решта відсікається на нашому боці. Номер, якого немає, — порожня "
            "видача, а не відмова"
        ),
    ),
    _cap(
        "ua_prozorro",
        Operation.READ_DOCUMENT,
        DocumentClass.REGISTRY_RECORD,
        layer=_REACHABLE,
        verified_at="2026-09-16",
        verified_by="жива проба 16.09.2026: get_tender за внутрішнім id 09076ffc… віддав картку тендера",
        limitations=(
            "за внутрішнім id CDB (32 hex) або номером UA-…; стислий підсумок "
            "тендера, не повний документ. Невідомий id — not_found, а не "
            "недоступність джерела"
        ),
    ),
    _cap(
        "ua_prozorro",
        Operation.SEARCH_FREE_TEXT,
        DocumentClass.REGISTRY_RECORD,
        layer=_NOT_COVERED,
        verified_at=None,
        free_text_egress=True,
        limitations="у legal-профілі заблоковано; джерело до того ж недоступне",
    ),
    _cap(
        "ua_open_data_catalog",
        Operation.SEARCH_FREE_TEXT,
        DocumentClass.REGISTRY_RECORD,
        layer=_CONNECTED,
        free_text_egress=True,
        limitations="у legal-профілі заблоковано",
        verified_at=_LIVE_ACCEPTANCE,
    ),
    # -- EU: Cellar, живий прогон 2026-09-07 (research/13) ---------------------
    # Проба 2026-09-09: `query_law('EU', <CELEX регламенту>)` вернул текст
    # регламенту цілком — до неё под датой 2026-09-07 стояли только
    # постатейные чтения, то есть read_fragment.
    _cap(
        "eu_law_eurlex_cellar",
        Operation.READ_DOCUMENT,
        DocumentClass.ACT,
        layer=_CONNECTED,
        verified_at=_PROBE_ACCEPTANCE,
        verified_by=_PROBE_BY,
    ),
    _cap(
        "eu_law_eurlex_cellar",
        Operation.READ_FRAGMENT,
        DocumentClass.ACT,
        layer=_CONNECTED,
        verified_at=_LIVE_ACCEPTANCE,
    ),
    _cap(
        "eu_law_eurlex_cellar",
        Operation.READ_DOCUMENT,
        DocumentClass.PROCEDURE,
        layer=_CONNECTED,
        verified_at=_LIVE_ACCEPTANCE,
    ),
    _cap(
        "eu_law_eurlex_cellar",
        Operation.READ_FRAGMENT,
        DocumentClass.PROCEDURE,
        layer=_CONNECTED,
        verified_at=_LIVE_ACCEPTANCE,
    ),
    # До 2026-09-16 (T249) здесь стоял `not_covered`: `EuLawAdapter` не
    # переопределял `card`. Тикет 21 добавил карточку одним SPARQL-запросом
    # Cellar; живая проба `get_law_metadata` на 32024Q02173 и одном регламенте.
    # Слой `connected`, как у чтения EU этим же источником выше: тот же Cellar,
    # тот же транспорт, проверено живым прогоном.
    *(
        _cap(
            "eu_law_eurlex_cellar",
            Operation.CARD,
            document_class,
            layer=_CONNECTED,
            verified_at=_EU_CARD_ACCEPTANCE,
            verified_by=_EU_CARD_BY,
            limitations=(
                "картка з метаданих Cellar (CELEX, ELI, тип, дати, OJ, мови, остання "
                "консолідація); назва — англійського виразу; картка не доводить цитату"
            ),
        )
        for document_class in (DocumentClass.ACT, DocumentClass.PROCEDURE)
    ),
    # Опущено 2026-09-09 (T249): операции `list_revisions` в продукте нет ни
    # одного инструмента — grep по server.py даёт ноль. Возможность, которую
    # нельзя вызвать, не может быть проверена прогоном, а значит не может стоять
    # выше `not_covered`: карта покрытия читается юристом как обещание.
    _cap(
        "eu_law_eurlex_cellar",
        Operation.LIST_REVISIONS,
        DocumentClass.ACT,
        layer=_NOT_COVERED,
        verified_at=None,
        limitations=(
            "інструмента, який викликав би цю операцію, у сервері немає; "
            "перелік консолідацій береться вручну на eur-lex.europa.eu"
        ),
    ),
    _cap(
        "eu_law_eurlex_cellar",
        Operation.REVISION_AS_OF,
        DocumentClass.ACT,
        layer=_REACHABLE,
        limitations=(
            "лише для актів з доведеною хронологією консолідацій у Cellar; без повної "
            "межі інтервалу — revision_unknown"
        ),
        verified_at=_LIVE_ACCEPTANCE,
    ),
    # Опущено 2026-09-09 (T249): в поставке этим путём не ходят. `legal` блокирует
    # свободный текст, а `search_across_laws` для правопорядка, отличного от UA,
    # отказывает сразу (server.py). Прогона возможности нет ни одного.
    _cap(
        "eu_law_eurlex_cellar",
        Operation.SEARCH_FREE_TEXT,
        DocumentClass.ACT,
        layer=_NOT_COVERED,
        verified_at=None,
        free_text_egress=True,
        limitations=(
            "SPARQL CONTAINS за назвою; у legal-профілі заблоковано, а пошук "
            "по актах ЄС у поставці відмовляє. Ручний шлях — пошук EUR-Lex"
        ),
    ),
    # Читання рішень Суду ЄС прийнято живим прогоном 2026-09-07:
    # ECLI:EU:C:2021:798 → CELEX 62019CJ0487, пункт 45 і повний текст.
    # Прогін 2026-09-08 додав три родини розмітки і вхід за CELEX:
    # рішення 1990-х (стара, div#TexteOnly), 2000-х (середня,
    # C01PointnumeroteAltN) і новіші за CELEX (сучасна). Картка провадження від
    # номера справи читається і для XX сторіччя, і для Загального суду, а номер
    # апеляції розбирається із суфіксом.
    _cap(
        "eu_case_law_cellar",
        Operation.READ_DOCUMENT,
        DocumentClass.DECISION,
        layer=_REACHABLE,
        verified_at=_RELEVANCE_ACCEPTANCE,
        verified_by=_RELEVANCE_BY,
        limitations=(
            "ідентифікатор — ECLI або CELEX судової практики; подання перебирається "
            "(xhtml, потім text/html), бо рішення, старші за середину 2012 р., "
            "у xhtml не публікуються зовсім і давали хибний not_found; вичерпаний "
            "перебір дає not_covered «не опубліковано в машиночитаному вигляді», "
            "а не «ідентифікатора немає»; лише 24 офіційні мови ЄС"
        ),
    ),
    _cap(
        "eu_case_law_cellar",
        Operation.READ_FRAGMENT,
        DocumentClass.DECISION,
        layer=_REACHABLE,
        verified_at=_RELEVANCE_ACCEPTANCE,
        verified_by=_RELEVANCE_BY,
        limitations=(
            "локатор — номер пункту рішення; три родини розмітки: coj-* (id=pointN), "
            "C01PointnumeroteAltN, div#TexteOnly після якоря #MO"
        ),
    ),
    _cap(
        "eu_case_law_cellar",
        Operation.SEARCH_FREE_TEXT,
        DocumentClass.DECISION,
        layer=_NOT_COVERED,
        verified_at=None,
        free_text_egress=True,
        limitations="у legal-профілі заблоковано; живого прогону не приймали",
    ),
    # T-cites: «документи, що цитують акт» — структурний пошук за CELEX
    # цитованого акта (`?work cdm:work_cites_work ?cited`, Cellar SPARQL).
    # Прийнято живим прогоном через сам інструмент 2026-09-09:
    # `search_decisions(legal_order="EU", cites=<CELEX регламенту>)` повернув
    # 20 рядків першої сторінки з кількох сотень документів Суду ЄС і
    # Загального суду, що цитують цей регламент.
    _cap(
        "eu_case_law_cellar",
        Operation.SEARCH_BY_CITATION,
        DocumentClass.DECISION,
        layer=_REACHABLE,
        verified_at=_CITES_ACCEPTANCE,
        verified_by=_CITES_BY,
        limitations=(
            "вхід — лише CELEX акта (вільний текст відхиляється до відправлення "
            "джерелу); фільтр суду за префіксом CELEX (C/T) і "
            "date_from/date_to за work_date_document — включно; чесний "
            "complete/truncated через LIMIT+1"
        ),
    ),
    # Тікет 47: рішення за номером справи. Номер (C-311/18) розкривається в CELEX
    # за правилом Cellar (6 + рік + вид + номер), джерело питається про ці точні
    # CELEX — вільний текст йому не йде.
    _cap(
        "eu_case_law_cellar",
        Operation.SEARCH_BY_IDENTIFIER,
        DocumentClass.DECISION,
        layer=_REACHABLE,
        verified_at=_CASE_NUMBER_ACCEPTANCE,
        verified_by=_CASE_NUMBER_BY,
        limitations=(
            "вхід — номер справи (C-NNN/YY, T-NNN/YY, F-NNN/YY, з суфіксом виду "
            "провадження) або CELEX судової практики; видача — рішення (J), ухвала (O) "
            "і, для Суду, висновок генерального адвоката (C) з ECLI, датою й назвою "
            "справи. В об'єднаних справах CELEX має лише перша справа: за номером "
            "другої рішення може не знайтися. Повідомлення в OJ C і резюме не "
            "шукаються; вільний текст джерелу не надсилається"
        ),
    ),
    _cap(
        "eu_cjeu_public_case_card",
        Operation.PUBLIC_CASE_CARD,
        DocumentClass.DECISION,
        verified_at=_CJEU_CARD_ACCEPTANCE,
        verified_by=_CJEU_CARD_BY,
        layer=_REACHABLE,
        limitations=(
            "два канали: Cellar SPARQL (публікації за формою CELEX) і, коли їх немає, "
            "легасі-картка CURIA juris.curia.europa.eu (liste/fiche/documents.jsf) — "
            "дата подання, суд, що звернувся, держава, предмет, мова, документи з "
            "датами й посиланнями; поле channel називає канал. Легасі-сторінка без "
            "контракту: зміна розмітки чи недоступність — source_unavailable з "
            "причиною curia_legacy:* і ручним шляхом. InfoCuria SPA і її бекенд не "
            "використовуються; e-Curia — ніколи"
        ),
    ),
    # -- ECHR / ICJ (research/13) ----------------------------------------------
    _cap(
        "echr_hudoc",
        Operation.READ_DOCUMENT,
        DocumentClass.DECISION,
        layer=_REACHABLE,
        verified_at=_RELEVANCE_ACCEPTANCE,
        verified_by=_RELEVANCE_BY,
        limitations=(
            "ECLI рішення → itemid HUDOC → повний текст; ECLI зводиться до верхнього "
            "регістру — у змішаному HUDOC віддає нуль записів"
        ),
    ),
    _cap(
        "echr_hudoc",
        Operation.READ_FRAGMENT,
        DocumentClass.DECISION,
        layer=_REACHABLE,
        verified_at=_RELEVANCE_ACCEPTANCE,
        verified_by=_RELEVANCE_BY,
    ),
    _cap(
        "echr_hudoc",
        Operation.SEARCH_BY_IDENTIFIER,
        DocumentClass.DECISION,
        layer=_REACHABLE,
        verified_at=_RELEVANCE_ACCEPTANCE,
        verified_by=_RELEVANCE_BY,
        limitations=(
            "форма запиту appno:\"NNNNN/YY\"; п'ять номерів — п'ять різних видач, у "
            "кожній є запитана заява. Номер іде голим: суфікс «+», склейка через «;» і форма без "
            "слеша дають нуль записів. Видача без запитаного ідентифікатора — відказ, "
            "а не результат"
        ),
    ),
    _cap(
        "echr_hudoc",
        Operation.SEARCH_FREE_TEXT,
        DocumentClass.DECISION,
        layer=_NOT_COVERED,
        verified_at=_RELEVANCE_ACCEPTANCE,
        verified_by=_RELEVANCE_BY,
        free_text_egress=True,
        limitations=(
            "прогін 2026-09-08 показав, що операція не фільтрує: два різні запити "
            "contains(docname, …) повернули ту саму "
            "десятку сторонніх записів. Шар непокритий не через брак проби, а тому що "
            "видача не стосується запиту. Заборона вільного тексту у legal-профілі — "
            "окрема, самостійна підстава"
        ),
    ),
    _cap(
        "icj_official_archive",
        Operation.READ_DOCUMENT,
        DocumentClass.DECISION,
        layer=_NOT_COVERED,
        verified_at=None,
        limitations=(
            "офіційний PDF закритий Cloudflare JS-challenge (403 будь-якому клієнту, "
            "2026-09-07; повторна проба 2026-09-09 — той самий «Just a moment…», "
            "403); захист не обходиться. Картка справи читається окремими операціями "
            "card/public_case_card. Ручний шлях — завантажити PDF з icj-cij.org вручну"
        ),
    ),
    _cap(
        "icj_official_archive",
        Operation.READ_FRAGMENT,
        DocumentClass.DECISION,
        layer=_NOT_COVERED,
        verified_at=None,
        limitations="без повного тексту (див. read_document) параграф не читається",
    ),
    _cap(
        "icj_official_archive",
        Operation.CARD,
        DocumentClass.DECISION,
        layer=_REACHABLE,
        limitations=(
            "картка справи icj-cij.org/case/<N> віддає перелік офіційних файлів; "
            "карткою цитату не підтверджують (confirmable=false)"
        ),
        verified_at=_LIVE_ACCEPTANCE,
    ),
    # Той самий факт, що й Operation.CARD вище, під іменем, яке шукає інструмент
    # get_case (T-icj-card, 2026-09-09): маршрутизація get_case завжди питає
    # public_case_card і кличе adapter.public_card(), тоді як реєстр ніс лише
    # card — інструмент і реєстр називали одну можливість по-різному, і
    # get_case("ICJ", <номер>) відмовляв not_covered, хоча card() працює. Виправлено
    # додаванням цього запису та методу IcjAdapter.public_card (тонкий псевдонім
    # card()); Operation.CARD/get_law_metadata лишені без змін.
    _cap(
        "icj_official_archive",
        Operation.PUBLIC_CASE_CARD,
        DocumentClass.DECISION,
        layer=_REACHABLE,
        limitations=(
            "те саме, що card(): перелік офіційних PDF зі сторінки справи; "
            "карткою цитату не підтверджують (confirmable=false)"
        ),
        verified_at=_ICJ_PUBLIC_CARD_ACCEPTANCE,
        verified_by=_ICJ_PUBLIC_CARD_BY,
    ),
)


def register_default_capabilities() -> tuple[Capability, ...]:
    for capability in _DEFAULT_CAPABILITIES:
        register_capability(capability)
    return _DEFAULT_CAPABILITIES


# Наполнить реестры при импорте: карта покрытия обязана быть готова к работе
# без отдельного шага инициализации (T025–T028).
register_default_sources()
register_default_capabilities()
