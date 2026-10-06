"""Сверка цитаты: три смысловых исхода, разбор по форумам, сессионная память
доказательств (T035–T041, T043; расширено T122, T127, T128, T130, T131).

Это единственное свойство, отличающее продукт от бесплатных аналогов. Измеренные
коммерческие юридические системы дают от 17 до 33 процентов ответов с выдуманными
ссылками; зафиксировано около 1490 судебных решений, где вскрылись сочинённые
моделью цитаты, с санкциями вплоть до приостановления лицензии адвоката.

Правила, ради которых модуль устроен именно так:

* **Похожий фрагмент не предлагается.** При ``not_confirmed`` ответ не содержит
  ни замены, ни поля, куда её можно было бы положить: юрист процитирует
  подсказанное (принцип II).
* **Нераспознанная ссылка не выпадает молча.** :func:`parse_citation` всегда
  возвращает :class:`Citation`; нераспознанная даёт диагностику
  ``unrecognized_format`` с перечнем ожидаемых форматов форума (R-04).
* **Сверки по памяти не существует.** Если текста в памяти сессии нет, диагностика
  ``text_not_fetched``: ни догадки по идентификатору, ни похода в индекс.
* **Канонического текста может не быть — но это надо доказать.** Исход
  ``no_canonical_text`` выдаётся только по записи реестра обычных норм,
  проверенной юристом (basis_id, reviewer, verified_at). Пустая выдача,
  недоступность источника и неизвестная редакция этим исходом не являются
  (ADR 0002, закрытие ревью H1).
* **Смысловых исходов три** (contracts/envelope-and-errors.md): ``confirmed``,
  ``not_confirmed``, ``no_canonical_text``. ``text_not_fetched`` и
  ``unrecognized_format`` — диагностические причины неподтверждённости.
* **Идентичность доказательства** — правопорядок, документ, редакция, язык,
  хеш содержания, локатор, сессия транспорта (FR-205). Другая сессия, другая
  редакция, другой язык или другой хеш — другое доказательство; подмена одного
  другим не подтверждает ничего.
* **Пустой набор ссылок не подтверждён.** ``verify_answer`` без единой ссылки
  даёт ``verification_complete=false`` и ``all_confirmed=false``.

Слой живёт в сервере и работает сам по себе (R-03, FR-008): исход зависит только
от полученного текста и разбора ссылки. Ничего, что можно было бы отключить
настройкой или обойти указанием, здесь нет — гарантия, которую можно нарушить
указанием, гарантией не является.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from core.contracts import NoCanonicalText, Unit
from core.legal_orders import (
    CitationFormat,
    normalize_legal_order_code,
)

__all__ = [
    "AmbiguousEvidence",
    "CachedText",
    "CitationFormat",
    "CitationMeta",
    "CITATION_META_KEYS",
    "CustomaryNorm",
    "CUSTOMARY_NORMS_PATH",
    "EvidenceCapacityExceeded",
    "SESSION_TEXTS",
    "SessionTextCache",
    "load_customary_norms",
    "normalize_document_id",
    "normalize_subdivision",
    "register_customary_norm",
]


# ---------------------------------------------------------------------------
# Нормализация адресов
# ---------------------------------------------------------------------------

#: Ярлык подразделения снимается, адрес — нет. «Article 5(1)» и «стаття 5(1)» —
#: одно и то же место документа, «5(1)» и «5(2)» — разные, «5a» и «5(1)(a)» — тоже.
_SUBDIVISION_LABEL = re.compile(
    r"^(?:article|articles|art\.?|стаття|статті|ст\.?|пункт|п\.?|частина|ч\.?|"
    r"paragraph|paragraphs|para\.?|paras\.?|point|pants|§+|no\.?|nos\.?)\s*",
    re.IGNORECASE,
)
_SUBDIVISION_NOISE = re.compile(r"[\s ]+")


def normalize_subdivision(value: Any) -> str:
    """Свести адрес подразделения к сравнимому виду.

    Снимается только ярлык («стаття», ``Article``, ``§``) и пробелы. Сам адрес
    остаётся как есть: разница между ``5(1)`` и ``5(2)`` — это разница между
    двумя нормами, а ``5a`` — самостоятельная статья, не пункт (a) статьи 5.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    previous = None
    while previous != text:
        previous = text
        text = _SUBDIVISION_LABEL.sub("", text).strip()
    text = _SUBDIVISION_NOISE.sub("", text)
    return text.strip(" .,;").lower()


def normalize_document_id(value: Any) -> str:
    """Свести идентификатор документа к сравнимому виду (регистр, хвостовой слеш)."""
    text = str(value or "").strip()
    if not text:
        return ""
    text = text.rstrip("/")
    return text.lower()


# ---------------------------------------------------------------------------
# Citation — ссылка, как её привела модель, и её разбор
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Разбор ссылок по форумам (T035, R-04, T128)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Извлечение ссылок из текста ответа (T043)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# SessionTextCache — доказательства, полученные в этой сессии (T036, FR-010, T122, T130)
# ---------------------------------------------------------------------------


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


#: Реквизиты документа, которые адаптер уже кладёт в ``data``, — белый список.
#: Ключ, которого источник не дал, сюда не попадает; поле, которого нет в этом
#: перечне, в доказательство не проезжает вовсе. Список закрыт намеренно: ссылку
#: печатает сервер из полученного, и «а вдруг пригодится» здесь означало бы, что
#: в юридическую ссылку попадёт что угодно из ответа источника.
CITATION_META_KEYS: tuple[str, ...] = (
    "ecli",
    "case_name",
    "case_number",
    "court",
    "judgment_date",
    "appno",
    "chamber",
    "document_type",
    "title",
    "celex",
    "act_title",
    "revision_notice",
    "amending_acts",
    "consolidation_id",
    "oj_ref",
    "act_date",
    "short_name",
    "name_uk",
    "publication",
    "signed_on",
    "in_force_from",
    "consolidated_as_of",
)

_ISO_DATETIME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})[T ]\d{2}:\d{2}")


def _date_only(value: str) -> str:
    """``2001-11-21T00:00:00`` → ``2001-11-21``; всё прочее — как пришло.

    Источник (HUDOC) отдаёт дату решения как полночь по местному времени, и
    печатать «21 листопада 2001 р. 00:00:00» в судебной ссылке нельзя. Это не
    домысливание значения, а отбрасывание нулевого времени, которого в дате
    решения нет.
    """
    match = _ISO_DATETIME_RE.match(value)
    return match.group(1) if match else value


class CitationMeta(BaseModel):
    """Реквизиты документа, по которым юрист находит его в официальном источнике.

    Отдельная замороженная модель, а не словарь: :class:`CachedText` заморожен
    и хешируется ключом сессии, а словарь нехешируем. ``extra="forbid"``
    держит белый список закрытым — реквизит, которого адаптер не дал, здесь
    пустой, и рендер печатает его отсутствие, а не выдумывает значение.

    В ``content_hash`` и ``evidence_id`` эти поля не входят и входить не могут:
    идентичность доказательства стоит на тексте фрагмента, и добавление
    метаданных не смеет переименовать ни одно уже существующее доказательство.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Судебная практика.
    ecli: str = ""
    case_name: str = ""
    case_number: str = ""
    court: str = ""
    judgment_date: str = ""
    appno: str = ""
    chamber: str = ""
    document_type: str = ""
    #: Заголовок, которым источник назвал прочитанное (статья, решение, акт).
    title: str = ""
    #: Законодательство.
    celex: str = ""
    act_title: str = ""
    #: Редакция: оговорка источника и перечень изменяющих актов.
    revision_notice: str = ""
    amending_acts: tuple[str, ...] = ()
    consolidation_id: str = ""
    #: Реквизиты, которых требует стандарт ссылки FR-504 и которым до набора
    #: 005 в доказательстве не было места. Их отсутствие и есть измеренная
    #: причина: на весь пакет из семи документов пришлась **одна** адреса
    #: источника и **ноль** ссылок с пинпоинтом до подпункта. Поле, которого
    #: нет, источник заполнить не может — поэтому они здесь, а сборка ссылки
    #: (:mod:`references`) отказывает, называя, откуда взять недостающее.
    oj_ref: str = ""
    act_date: str = ""
    short_name: str = ""
    name_uk: str = ""
    publication: str = ""
    signed_on: str = ""
    in_force_from: str = ""
    consolidated_as_of: str = ""

    @field_validator(
        "ecli",
        "case_name",
        "case_number",
        "court",
        "judgment_date",
        "appno",
        "chamber",
        "document_type",
        "title",
        "celex",
        "act_title",
        "revision_notice",
        "consolidation_id",
        "oj_ref",
        "act_date",
        "short_name",
        "name_uk",
        "publication",
        "signed_on",
        "in_force_from",
        "consolidated_as_of",
        mode="before",
    )
    @classmethod
    def _clean(cls, value: Any) -> str:
        return "" if value is None else str(value).strip()

    @field_validator("amending_acts", mode="before")
    @classmethod
    def _clean_acts(cls, value: Any) -> tuple[str, ...]:
        if value is None or isinstance(value, (str, bytes)):
            return ()
        try:
            items = tuple(value)
        except TypeError:
            return ()
        return tuple(str(item).strip() for item in items if str(item).strip())

    @field_validator("judgment_date")
    @classmethod
    def _drop_zero_time(cls, value: str) -> str:
        return _date_only(value)

    @classmethod
    def from_source_data(cls, data: Mapping[str, Any] | None) -> "CitationMeta":
        """Реквизиты из ответа адаптера по белому списку.

        Пустое значение не пишется: «суд: —» в судебной ссылке хуже, чем
        отсутствие строки о суде. Вложенный блок ``revision`` разбирается
        отдельно — консолидация и перечень изменяющих актов лежат в нём, а не
        рядом с остальными полями.
        """
        if not isinstance(data, Mapping):
            return cls()
        found: dict[str, Any] = {}
        revision = data.get("revision")
        if isinstance(revision, Mapping):
            for key in ("consolidation_id", "amending_acts"):
                value = revision.get(key)
                if value:
                    found[key] = value
        for key in CITATION_META_KEYS:
            value = data.get(key)
            if value is None:
                continue
            if isinstance(value, str) and not value.strip():
                continue
            found[key] = value
        try:
            return cls(**found)
        except ValueError:
            # Реквизиты не могут отменить чтение: непригодное значение
            # отбрасывается, а не превращается в отказ выдать прочитанный текст.
            return cls()

    @property
    def is_empty(self) -> bool:
        """Ни одного реквизита: ссылка деградирует до идентификатора."""
        return not any(self.model_dump().values())


class CachedText(BaseModel):
    """Фрагмент, полученный в сессии, и всё, что о нём известно (Fragment из data-model).

    Полей, связывающих запись с человеком, здесь нет и быть не может:
    ``extra="forbid"`` превращает попытку добавить их в ошибку, а не в
    незамеченное расширение схемы (принцип VI). Идентичность неизменяема.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    path: str = ""
    text: str = Field(min_length=1)
    source_url: str = ""
    fetched_at: str = Field(min_length=1)
    legal_order: str = ""
    version_id: str = ""
    language: str = ""
    content_hash: str = ""
    evidence_id: str = ""
    publication_kind: str = "unknown"
    content_kind: str = "unknown"
    locator: str = ""
    #: Другие идентификаторы того же документа (например, «icj reports 2012, p. 99»
    #: для официального PDF ICJ) — чтобы ссылка форума нашла зарегистрированный текст.
    aliases: tuple[str, ...] = ()
    #: Реквизиты, которыми документ ищут в официальном источнике: ECLI, название
    #: дела, суд, дата, номер заявления, редакция. Приходят от адаптера через
    #: ``routing.perform_read`` и печатаются в ссылке; в ``content_hash`` и
    #: ``evidence_id`` не входят — доказательство остаётся тем же самым.
    meta: CitationMeta = CitationMeta()

    @field_validator("session_id", "document_id", "text", "fetched_at")
    @classmethod
    def not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError(
                "запись памяти доказательств без сессии, документа, текста или времени"
            )
        return stripped

    @model_validator(mode="after")
    def identity_is_derived_from_content(self) -> "CachedText":
        """Хеш и evidence_id вычисляются из содержимого, а не задаются извне."""
        digest = _content_hash(self.text)
        if self.content_hash and self.content_hash != digest:
            raise ValueError(
                "content_hash не совпадает с полученным текстом: фрагмент с другим хешем — "
                "другое доказательство"
            )
        object.__setattr__(self, "content_hash", digest)
        if not self.evidence_id:
            raw = "|".join(
                (
                    self.session_id,
                    normalize_legal_order_code(self.legal_order),
                    normalize_document_id(self.document_id),
                    self.version_id,
                    self.language.lower(),
                    digest,
                    normalize_subdivision(self.path),
                )
            )
            object.__setattr__(self, "evidence_id", "ev-" + _content_hash(raw)[:24])
        return self

    @property
    def unit_ref(self) -> str:
        """Что именно получено: документ и, если есть, его подразделение."""
        return f"{self.document_id}#{self.path}" if self.path else self.document_id

    @property
    def document_keys(self) -> tuple[str, ...]:
        return (normalize_document_id(self.document_id),) + tuple(
            normalize_document_id(alias) for alias in self.aliases if alias
        )


class AmbiguousEvidence:
    """В сессии несколько доказательств под один адрес: разные редакции/языки/хеши.

    Выбрать одно из них молча — подмена. Вызывающий обязан указать
    ``evidence_id`` либо редакцию/язык в ссылке.
    """

    __slots__ = ("candidates",)

    def __init__(self, candidates: tuple[CachedText, ...]) -> None:
        self.candidates = candidates

    def describe(self) -> str:
        return "; ".join(
            f"{entry.evidence_id} (редакція {entry.version_id or '—'}, мова {entry.language or '—'})"
            for entry in self.candidates
        )


class EvidenceCapacityExceeded(RuntimeError):
    """Лимит памяти доказательств сессии: фрагмент не регистрируется и не выдаётся."""


class EvidenceWriteForbidden(RuntimeError):
    """Запись доказательства без квитанции чтения.

    Доказательством является текст, полученный от источника, а не текст,
    который кто-то принёс (принцип II). Квитанцию выпускает только путь
    чтения, поэтому запись без неё — не ошибка вызова, а попытка обойти
    единственную точку регистрации.
    """


#: Ключ конструктора квитанции. Единственный законный способ её получить —
#: :func:`issue_receipt`; воспроизвести объект «похожей формы» снаружи можно
#: (Python не даёт настоящей приватности), но не по неосторожности.
_RECEIPT_KEY = object()

#: Каналы, чтение по которым порождает доказательство: живой источник и горячий
#: кеш полученного. ``curated`` — наш выверенный набор, а не ответ источника.
_RECEIPT_CHANNELS = frozenset({"live", "index"})


class FetchReceipt:
    """Квитанция чтения: подтверждение, что текст пришёл от источника.

    Несёт канал, адрес источника, момент получения и хеш текста — то, что
    выдаёт штамп происхождения и чего нет у текста, восстановленного по памяти
    модели.
    """

    __slots__ = ("source_channel", "source_url", "fetched_at", "content_hash")

    source_channel: str
    source_url: str
    fetched_at: str
    content_hash: str

    def __init__(
        self,
        key: Any,
        *,
        source_channel: str,
        source_url: str,
        fetched_at: str,
        content_hash: str,
    ) -> None:
        if key is not _RECEIPT_KEY:
            raise EvidenceWriteForbidden(
                "квитанцию чтения выпускает только issue_receipt по штампу происхождения"
            )
        object.__setattr__(self, "source_channel", source_channel)
        object.__setattr__(self, "source_url", source_url)
        object.__setattr__(self, "fetched_at", fetched_at)
        object.__setattr__(self, "content_hash", content_hash)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("квитанция чтения неизменяема")

    def __repr__(self) -> str:
        return (
            f"FetchReceipt(source_channel={self.source_channel!r}, source_url={self.source_url!r})"
        )


def issue_receipt(stamp: Any) -> FetchReceipt:
    """Выпустить квитанцию по штампу происхождения полученного текста.

    Вызывается ровно из одного места — ``routing.perform_read`` после ответа
    адаптера. Штамп канала ``curated`` квитанции не даёт: выверенный нами
    набор не является ответом источника на этот запрос.
    """
    channel = getattr(stamp, "source_channel", None)
    value = str(getattr(channel, "value", channel) or "")
    if value not in _RECEIPT_CHANNELS:
        raise EvidenceWriteForbidden(
            f"канал {value!r} не порождает доказательства: квитанция выпускается "
            f"только для {', '.join(sorted(_RECEIPT_CHANNELS))}"
        )
    return FetchReceipt(
        _RECEIPT_KEY,
        source_channel=value,
        source_url=str(getattr(stamp, "source_url", "") or ""),
        fetched_at=str(getattr(stamp, "fetched_at", "") or ""),
        content_hash=str(getattr(stamp, "content_hash", "") or ""),
    )


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


#: Лимиты сессии: фрагмент до выдачи регистрируется, лимит достигнут — явный
#: отказ, ранее выданные фрагменты не вытесняются незаметно (ADR 0002).
DEFAULT_MAX_ENTRIES_PER_SESSION = 400
DEFAULT_MAX_CHARS_PER_SESSION = 6_000_000
#: Сессия без обращений дольше этого срока забывается целиком.
DEFAULT_SESSION_TTL_SECONDS = 8 * 3600
#: Квота одного агента внутри сессии (T180). Лимит сессии общий для всех, кто
#: в ней читает, поэтому один исследователь, увлёкшийся чтением, до появления
#: квоты забирал память у остальных, и каждый следующий получал
#: ``evidence_capacity`` без указания на то, чьё это чтение. Значение выбрано
#: так, чтобы в общий лимит помещалось не менее трёх агентов, читающих на
#: полную квоту; чтение без ``agent_id`` квотой не ограничено и расходует
#: только общий лимит.
DEFAULT_MAX_ENTRIES_PER_AGENT = 120


class SessionTextCache:
    """Доказательства сессии в памяти процесса — и нигде больше.

    Содержимое не переживает сессию: ключ включает ``session_id``,
    :meth:`end_session` его убирает, истечение TTL — тоже, а способа записать
    память куда-либо у класса нет. Материалы конкретного дела сюда не попадают,
    потому что не загружаются вовсе (принцип VI).

    ``require_receipt`` закрывает запись для кода, который читал не у источника.
    Память процесса (:data:`SESSION_TEXTS`) создаётся с ним, и тогда
    :meth:`remember` требует :class:`FetchReceipt` из ``issue_receipt`` —
    квитанцию, которую выпускает только ``routing.perform_read``. Это граница
    для in-process кода, живущего в том же процессе (агентный слой, скрипты):
    Python настоящей приватности не даёт, и обойти её умышленно можно. Через
    MCP её обходить нечем и без того — ни один инструмент не принимает текста
    для регистрации, а ``verify_citation`` требует ``evidence_id``, уже
    существующий в сессии. Тесты, собирающие собственную память, создают
    экземпляр без квитанции и ничего не теряют: они и проверяют хранилище, а не
    путь чтения.
    """

    def __init__(
        self,
        *,
        max_entries_per_session: int = DEFAULT_MAX_ENTRIES_PER_SESSION,
        max_chars_per_session: int = DEFAULT_MAX_CHARS_PER_SESSION,
        ttl_seconds: float = DEFAULT_SESSION_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        require_receipt: bool = False,
        max_entries_per_agent: int = DEFAULT_MAX_ENTRIES_PER_AGENT,
    ) -> None:
        self._entries: dict[tuple[str, str, str, str, str, str], CachedText] = {}
        self._by_evidence: dict[tuple[str, str], CachedText] = {}
        self._last_seen: dict[str, float] = {}
        #: (сессия, агент) — сколько фрагментов зарегистрировал именно он.
        #: Агент здесь — роль в задаче, а не человек: значение приходит от
        #: ведущего как метка («researcher-eu»), никуда не записывается и
        #: умирает вместе с сессией.
        self._agent_entries: dict[tuple[str, str], int] = {}
        self._max_entries = max_entries_per_session
        self._max_entries_per_agent = max_entries_per_agent
        self._max_chars = max_chars_per_session
        self._ttl = ttl_seconds
        self._clock = clock
        self._require_receipt = require_receipt
        self._lock = threading.Lock()

    # -- запись ------------------------------------------------------------

    def remember(
        self,
        session_id: str,
        document_id: str,
        text: str,
        *,
        path: str = "",
        source_url: str = "",
        fetched_at: str | None = None,
        legal_order: str = "",
        version_id: str = "",
        language: str = "",
        publication_kind: str = "unknown",
        content_kind: str = "unknown",
        locator: str = "",
        aliases: Iterable[str] = (),
        meta: CitationMeta | None = None,
        receipt: FetchReceipt | None = None,
        agent_id: str = "",
    ) -> CachedText:
        """Запомнить полученный текст. Тот же текст под тем же адресом — та же запись.

        Другой хеш под тем же адресом — другая запись; ничего не вытесняется.
        При превышении лимита — :class:`EvidenceCapacityExceeded`, текст не
        регистрируется, и вызывающий обязан не выдавать его.

        В памяти с ``require_receipt`` запись без квитанции чтения —
        :class:`EvidenceWriteForbidden`: доказательством является полученное от
        источника, а не принесённое вызывающим.

        ``agent_id`` — метка роли, читающей в этой сессии. Она расходует
        собственную квоту, поэтому исчерпание памяти одним исследователем
        называет его и не отнимает чтения у остальных; без метки чтение
        расходует только общий лимит сессии.
        """
        if self._require_receipt and not isinstance(receipt, FetchReceipt):
            raise EvidenceWriteForbidden(
                "пам'ять доказів сесії приймає лише текст, отриманий від джерела: "
                "реєстрація йде через routing.perform_read, який видає квитанцію читання"
            )
        entry = CachedText(
            session_id=session_id,
            document_id=document_id,
            path=normalize_subdivision(path),
            text=text,
            source_url=source_url,
            fetched_at=fetched_at or _now(),
            legal_order=normalize_legal_order_code(legal_order),
            version_id=str(version_id or ""),
            language=str(language or "").lower(),
            publication_kind=publication_kind,
            content_kind=content_kind,
            locator=locator or normalize_subdivision(path),
            aliases=tuple(str(alias) for alias in aliases if str(alias).strip()),
            meta=meta or CitationMeta(),
        )
        key = self._key(entry)
        agent = str(agent_id or "").strip()
        with self._lock:
            self._expire_locked()
            session = entry.session_id
            if key not in self._entries:
                count = sum(1 for k in self._entries if k[0] == session)
                chars = sum(len(e.text) for k, e in self._entries.items() if k[0] == session)
                if count + 1 > self._max_entries or chars + len(entry.text) > self._max_chars:
                    raise EvidenceCapacityExceeded(
                        f"сесія {session}: ліміт доказів ({self._max_entries} фрагментів / "
                        f"{self._max_chars} символів) досягнуто"
                    )
                if agent:
                    used = self._agent_entries.get((session, agent), 0)
                    if used + 1 > self._max_entries_per_agent:
                        raise EvidenceCapacityExceeded(
                            f"агент {agent} у сесії {session}: власна квота "
                            f"({self._max_entries_per_agent} фрагментів) вичерпана; "
                            "пам'ять сесії лишається доступною іншим агентам"
                        )
                    self._agent_entries[(session, agent)] = used + 1
            self._entries[key] = entry
            self._by_evidence[(session, entry.evidence_id)] = entry
            self._last_seen[session] = self._clock()
        return entry

    def remember_unit(
        self,
        session_id: str,
        unit: Unit,
        *,
        source_url: str = "",
        fetched_at: str | None = None,
        legal_order: str = "",
        language: str = "",
        receipt: FetchReceipt | None = None,
        agent_id: str = "",
    ) -> CachedText:
        """Запомнить единицу цитирования так, как её отдал источник."""
        version_id = ""
        if unit.revision is not None:
            version_id = unit.revision.consolidation_id or unit.revision.valid_from
        return self.remember(
            session_id,
            unit.document_id,
            unit.text,
            path=unit.path,
            source_url=source_url,
            fetched_at=fetched_at,
            legal_order=legal_order,
            version_id=version_id,
            language=language,
            content_kind="fragment",
            receipt=receipt,
            agent_id=agent_id,
        )

    # -- чтение ------------------------------------------------------------

    @staticmethod
    def _key(entry: CachedText) -> tuple[str, str, str, str, str, str]:
        return (
            entry.session_id,
            normalize_document_id(entry.document_id),
            normalize_subdivision(entry.path),
            entry.version_id,
            entry.language,
            entry.content_hash,
        )

    def _touch(self, session_id: str) -> None:
        with self._lock:
            self._expire_locked()
            if any(k[0] == session_id for k in self._entries):
                self._last_seen[session_id] = self._clock()

    def _session_entries(self, session_id: str) -> tuple[CachedText, ...]:
        session = str(session_id).strip()
        self._touch(session)
        with self._lock:
            return tuple(entry for key, entry in self._entries.items() if key[0] == session)

    def find(
        self,
        session_id: str,
        document_id: str,
        subdivision: str,
        *,
        version_id: str | None = None,
        language: str | None = None,
    ) -> CachedText | AmbiguousEvidence | None:
        """Точное совпадение по документу и подразделению — или ничего.

        Приблизительного совпадения здесь нет намеренно: подстановка соседнего
        пункта под видом запрошенного и есть выдуманная цитата. Несколько
        совпадений с разными редакциями/языками/хешами — :class:`AmbiguousEvidence`,
        а не первое попавшееся.
        """
        document = normalize_document_id(document_id)
        path = normalize_subdivision(subdivision)
        wanted_version = str(version_id or "").strip()
        wanted_language = str(language or "").strip().lower()
        matches = [
            entry
            for entry in self._session_entries(session_id)
            if document in entry.document_keys
            and normalize_subdivision(entry.path) == path
            and (not wanted_version or entry.version_id == wanted_version)
            and (not wanted_language or entry.language == wanted_language)
        ]
        if not matches:
            return None
        if len(matches) == 1:
            return matches[0]
        return AmbiguousEvidence(tuple(matches))

    def by_evidence(self, session_id: str, evidence_id: str) -> CachedText | None:
        """Фрагмент по идентификатору доказательства — только внутри своей сессии."""
        session = str(session_id).strip()
        self._touch(session)
        with self._lock:
            return self._by_evidence.get((session, str(evidence_id or "").strip()))

    def texts_for(self, session_id: str, document_id: str) -> tuple[CachedText, ...]:
        document = normalize_document_id(document_id)
        return tuple(
            entry for entry in self._session_entries(session_id) if document in entry.document_keys
        )

    def has_document(self, session_id: str, document_id: str) -> bool:
        """Запрашивался ли документ в этой сессии вообще."""
        return bool(self.texts_for(session_id, document_id))

    def count(self, session_id: str) -> int:
        """Сколько фрагментов сессии сейчас в памяти."""
        session = str(session_id or "").strip()
        with self._lock:
            return sum(1 for key in self._entries if key[0] == session)

    def agent_usage(self, session_id: str) -> dict[str, int]:
        """Сколько фрагментов зарегистрировал каждый агент этой сессии."""
        session = str(session_id or "").strip()
        with self._lock:
            return {
                agent: used
                for (sess, agent), used in self._agent_entries.items()
                if sess == session
            }

    @property
    def max_entries_per_agent(self) -> int:
        return self._max_entries_per_agent

    def sessions(self) -> tuple[str, ...]:
        with self._lock:
            self._expire_locked()
            return tuple(sorted({key[0] for key in self._entries}))

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    # -- забывание ---------------------------------------------------------

    def end_session(self, session_id: str) -> int:
        """Забыть всё, что получено в сессии. Возвращает число забытых записей."""
        session = str(session_id).strip()
        with self._lock:
            return self._forget_locked(session)

    def _forget_locked(self, session: str) -> int:
        doomed = [key for key in self._entries if key[0] == session]
        for key in doomed:
            del self._entries[key]
        for evidence_key in [k for k in self._by_evidence if k[0] == session]:
            del self._by_evidence[evidence_key]
        for agent_key in [k for k in self._agent_entries if k[0] == session]:
            del self._agent_entries[agent_key]
        self._last_seen.pop(session, None)
        return len(doomed)

    def _expire_locked(self) -> None:
        if self._ttl <= 0:
            return
        now = self._clock()
        for session, seen in list(self._last_seen.items()):
            if now - seen > self._ttl:
                self._forget_locked(session)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._by_evidence.clear()
            self._agent_entries.clear()
            self._last_seen.clear()


#: Память по умолчанию: одна на процесс сервера, разделённая по ``session_id``.
#: Запись — только с квитанцией чтения, то есть только из ``routing.perform_read``.
SESSION_TEXTS = SessionTextCache(require_receipt=True)


# ---------------------------------------------------------------------------
# Нормы без канонического текста (T039, R-09, T127)
# ---------------------------------------------------------------------------

#: Реестр обычных норм, проверенных юристом. Записи без reviewer/verified_at
#: остаются неактивными: по ним исход no_canonical_text не выдаётся.
CUSTOMARY_NORMS_PATH = Path(__file__).resolve().parents[1] / "data" / "customary_norms.json"


class CustomaryNorm(BaseModel):
    """Норма, которая существует, но текста для цитирования не имеет.

    Обычное международное право не имеет ни идентификатора, ни канонической
    формулировки, а во многих международных спорах оно является основным
    источником. Ответ строится как отсылка к отражающим документам и прямо
    помечается как таковой; подтверждать по доктринальному тексту как по норме
    запрещено — это подмена вспомогательного средства нормой.

    Исход по норме выдаётся только с положительным основанием: запись
    проверена юристом (``reviewer``, ``verified_at``) и указывает источники
    основания (``basis_source_urls``).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    subject: str = Field(min_length=1)
    legal_order: str = Field(min_length=2)
    reflecting_documents: tuple[str, ...] = Field(min_length=1)
    aliases: tuple[str, ...] = ()
    basis_id: str = ""
    basis_source_urls: tuple[str, ...] = ()
    reviewer: str | None = None
    verified_at: str | None = None
    notes: str = ""

    @field_validator("reflecting_documents")
    @classmethod
    def documents_are_real(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(item.strip() for item in value)
        if any(not item for item in cleaned):
            raise ValueError(
                "пустая строка не является отражающим документом; норма без отсылки "
                "не отличима от отсутствующей (R-09)"
            )
        return cleaned

    @property
    def is_verified(self) -> bool:
        """Есть ли положительное основание, проверенное юристом."""
        return bool(
            (self.reviewer or "").strip()
            and (self.verified_at or "").strip()
            and self.basis_id.strip()
            and self.basis_source_urls
        )

    @property
    def key(self) -> str:
        return _norm_key(self.subject)

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple({_norm_key(name) for name in (self.subject, *self.aliases)})

    def as_failure(self) -> NoCanonicalText:
        if not self.is_verified:
            raise ValueError(
                f"норма «{self.subject}» не имеет проверенного юристом основания: "
                "исход no_canonical_text по ней не строится"
            )
        return NoCanonicalText(
            subject=self.subject,
            reflecting_documents=self.reflecting_documents,
            basis_id=self.basis_id,
            basis_source_urls=self.basis_source_urls,
            reviewer=str(self.reviewer),
            verified_at=str(self.verified_at),
        )


def _norm_key(value: Any) -> str:
    text = re.sub(r"[^\w\s]", " ", str(value or "").lower())
    return re.sub(r"\s+", " ", text).strip()


_CUSTOMARY_NORMS: dict[str, CustomaryNorm] = {}


def register_customary_norm(norm: CustomaryNorm) -> CustomaryNorm:
    """Внести обычную норму в реестр. Повторная регистрация заменяет запись."""
    for key in norm.keys:
        _CUSTOMARY_NORMS[key] = norm
    return norm


def load_customary_norms(path: Path | None = None) -> tuple[CustomaryNorm, ...]:
    """Прочитать реестр обычных норм из файла и зарегистрировать его записи."""
    source = path or CUSTOMARY_NORMS_PATH
    if not source.exists():
        return ()
    with source.open("r", encoding="utf-8") as handle:
        rows = json.load(handle)
    loaded: list[CustomaryNorm] = []
    for row in rows if isinstance(rows, list) else []:
        norm = CustomaryNorm(
            subject=str(row.get("subject", "")),
            legal_order=str(row.get("legal_order", "")),
            reflecting_documents=tuple(row.get("reflecting_documents", ())),
            aliases=tuple(row.get("aliases", ())),
            basis_id=str(row.get("basis_id", "") or ""),
            basis_source_urls=tuple(row.get("basis_source_urls", ())),
            reviewer=row.get("reviewer"),
            verified_at=row.get("verified_at"),
            notes=str(row.get("notes", "") or ""),
        )
        loaded.append(register_customary_norm(norm))
    return tuple(loaded)


load_customary_norms()


# ---------------------------------------------------------------------------
# CitationCheck — исход сверки (T037–T041, T127)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Сверка
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# T189 — рендер аттестованного проекта: ссылку печатает сервер
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Реквизиты доказательства → ссылка (T272)
# ---------------------------------------------------------------------------
#
# До контракта 4.0.0 форма ссылки жила здесь семью функциями
# (``_eu_case_reference``, ``_echr_reference``, ``_nl_reference``,
# ``_lt_reference``, ``_lv_reference``, ``_eu_act_reference``,
# ``build_reference``). Ни короткой формы, ни сносок, ни дедупликации у них не
# было, и прогон 09.09.2026 показал цену: ноль ссылок с пинпоинтом до
# подпункта, одна адреса источника на весь пакет из семи документов, 97
# повторов оговорки о консолидации в одном файле. Форма переехала в
# :mod:`references` целиком — там она одна на все три рендерера.
#
# Здесь осталось одно: **перевод** реквизитов доказательства в аргументы
# сборщика. Ни одного реквизита этот код не выдумывает и ни одного не
# подставляет по умолчанию: недостающий даёт :class:`references.ReferenceError`
# с названным действием («візьміть OJ з картки EUR-Lex»), а не пустое место в
# напечатанном документе, куда потом что-нибудь впишет модель.


_ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


_MONTHS_UK = (
    "січня",
    "лютого",
    "березня",
    "квітня",
    "травня",
    "червня",
    "липня",
    "серпня",
    "вересня",
    "жовтня",
    "листопада",
    "грудня",
)


def _spoken_date(value: str) -> str:
    match = _ISO_DATE_RE.match(_date_only(str(value or "").strip()))
    if match is None:
        return str(value or "").strip()
    year, month, day = match.groups()
    index = int(month) - 1
    if not 0 <= index < 12:
        return value
    return f"{int(day)} {_MONTHS_UK[index]} {year} р."


def _joined(parts: Iterable[str]) -> str:
    return ", ".join(part for part in parts if part and part.strip())


# ---------------------------------------------------------------------------
# T295 — документы, чей текст ядро складывает из реестров
# ---------------------------------------------------------------------------
