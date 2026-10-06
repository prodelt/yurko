"""Конверт происхождения (T008, расширен T119).

Сопровождает каждый ответ, содержащий текст нормы, решения или договора.
Инструмент, вернувший текст без конверта, не соответствует контракту — это
проверяется валидацией в :mod:`contracts`, а не намерением.

Один инвариант этого модуля стоит выделить, потому что он и есть принцип IV
в исполняемом виде: **``stale = true`` обязано попадать в текст, который юрист
видит**, а не только в служебные поля. Поэтому :attr:`ProvenanceStamp.notice`
не является свободной строкой: конверт либо строит её сам, либо проверяет, что
переданная извне фраза не умалчивает о возрасте копии, о переводе и об
обязательной атрибуции. Незамеченная устаревшая норма в юридическом ответе —
это ошибка, а не техническая сноска.

Расширение T119 (ADR 0007, data-model.md): язык, его аутентичность, статус
публикации (официальная публикация / информационная консолидация / карточка /
резюме), полнота содержимого, идентификатор редакции, хеш и время получения —
независимые признаки. Неизвестное поле никогда не получает ложное ``true``:
``authenticity=unknown`` не может сопровождать ``is_authentic_version=True``.
"""

from __future__ import annotations

import datetime as dt
from datetime import timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = [
    "Authenticity",
    "ContentKind",
    "ENVELOPE_FIELDS",
    "ProvenanceStamp",
    "PublicationKind",
    "SourceChannel",
    "stamp_from_cache_entry",
]


class SourceChannel(str, Enum):
    """Откуда пришёл ответ.

    ``live`` — живой источник, ``index`` — горячий кеш, ``curated`` — выверенный
    нами набор. У последнего свежесть гарантируется нами, а не источником,
    поэтому он обязан нести дату сверки.
    """

    LIVE = "live"
    INDEX = "index"
    CURATED = "curated"


class PublicationKind(str, Enum):
    """Публикационная сила текста — независима от языка и от канала.

    ``official_journal`` — аутентичная официальная публикация (OJ,
    офіційне оприлюднення); ``official_journal_notice`` —
    повідомлення в Офіційному віснику (серія C) про відкриття провадження:
    офіційна публікація факту, а не текст рішення суду і не норма (T-cites,
    2026-09-09 — ``get_decision("EU","62020CN0340")`` раніше ніс
    ``court_publication``, хоча ні Суд, ні Загальний суд цей текст не
    видавали); ``consolidated`` — информационная консолидация (EUR-Lex
    consolidated text);
    ``official_translation`` — перевод, подготовленный официальным
    издателем; ``court_publication`` — официальная публикация судебного акта;
    ``administrative_guidance`` — разъяснение органа, не норма; ``card`` —
    карточка/метаданные; ``summary`` — резюме; ``curated`` — выверенная нами
    запись; ``unknown`` — не установлено.
    """

    OFFICIAL_JOURNAL = "official_journal"
    OFFICIAL_JOURNAL_NOTICE = "official_journal_notice"
    CONSOLIDATED = "consolidated"
    OFFICIAL_TRANSLATION = "official_translation"
    COURT_PUBLICATION = "court_publication"
    ADMINISTRATIVE_GUIDANCE = "administrative_guidance"
    CARD = "card"
    SUMMARY = "summary"
    CURATED = "curated"
    UNKNOWN = "unknown"


class ContentKind(str, Enum):
    """Что именно получено: полный текст, фрагмент, метаданные, резюме."""

    FULL_TEXT = "full_text"
    FRAGMENT = "fragment"
    METADATA = "metadata"
    SUMMARY = "summary"
    STUB = "stub"
    UNKNOWN = "unknown"


class Authenticity(str, Enum):
    """Аутентичность языковой версии: три состояния, не булево.

    ``unknown`` никогда не превращается в ``authentic`` по умолчанию.
    """

    AUTHENTIC = "authentic"
    NOT_AUTHENTIC = "not_authentic"
    UNKNOWN = "unknown"


_STALE_MARKER = "застаріл"
_TRANSLATION_MARKER = "переклад"

#: Виды содержимого, из которых можно подтверждать цитату.
_CONFIRMABLE_CONTENT = frozenset({ContentKind.FULL_TEXT, ContentKind.FRAGMENT})
#: Виды публикаций, которые не являются текстом нормы/решения.
_NON_TEXT_PUBLICATIONS = frozenset(
    {PublicationKind.CARD, PublicationKind.SUMMARY, PublicationKind.ADMINISTRATIVE_GUIDANCE}
)


class ProvenanceStamp(BaseModel):
    """Пометка происхождения: канал, возраст, язык, аутентичность, атрибуция."""

    model_config = ConfigDict(frozen=True)

    # -- обязательные ------------------------------------------------------
    source_channel: SourceChannel
    source_url: str = Field(min_length=1)
    language: str = Field(min_length=1)
    citation_format: str = Field(min_length=1)
    stale: bool
    is_authentic_version: bool
    is_translation: bool

    # -- необязательные ----------------------------------------------------
    cached_at: str | None = None
    age_days: float | None = None
    verified_at: str | None = None
    translation_outdated: bool | None = None
    attribution: str | None = None

    # -- идентичность текста (T119) ----------------------------------------
    #: Идентификатор редакции/выражения: датированный CELEX, ELI с датой,
    #: дата редакции национальной публикации, идентификатор документа суда. ``None`` —
    #: редакция источником не обозначена.
    version_id: str | None = None
    publication_kind: PublicationKind = PublicationKind.UNKNOWN
    content_kind: ContentKind = ContentKind.UNKNOWN
    #: Аутентичность как три состояния. Выводится из ``is_authentic_version``,
    #: если не задана явно; ``unknown`` несовместимо с ``is_authentic_version=True``.
    authenticity: Authenticity | None = None
    #: SHA-256 полученного текста — того, что реально отдано юристу.
    content_hash: str | None = None
    #: Момент получения от источника (ISO-8601, UTC).
    fetched_at: str | None = None

    #: Готовая фраза для юриста. Пустая означает «построй сам».
    notice: str = ""

    @field_validator("source_url", "language", "citation_format")
    @classmethod
    def not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("обязательное поле конверта не может быть пустым")
        return stripped

    @field_validator("cached_at", "verified_at", "attribution", "version_id", "content_hash")
    @classmethod
    def blank_is_absent(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None

    @model_validator(mode="after")
    def curated_must_say_when_we_checked(self) -> "ProvenanceStamp":
        if self.source_channel is SourceChannel.CURATED and not self.verified_at:
            raise ValueError(
                "канал curated обязан нести verified_at: в нём свежесть гарантируется "
                "нами, а не источником"
            )
        return self

    @model_validator(mode="after")
    def a_translation_is_not_the_authentic_version(self) -> "ProvenanceStamp":
        if self.is_translation and self.is_authentic_version:
            raise ValueError(
                "перевод не может быть аутентичной версией: он сопровождает норму, "
                "но не заменяет её (принцип V)"
            )
        return self

    @model_validator(mode="after")
    def unknown_authenticity_is_never_true(self) -> "ProvenanceStamp":
        """Три состояния аутентичности согласованы с булевым признаком."""
        if self.authenticity is None:
            derived = (
                Authenticity.AUTHENTIC if self.is_authentic_version else Authenticity.NOT_AUTHENTIC
            )
            object.__setattr__(self, "authenticity", derived)
            return self
        if self.authenticity is Authenticity.AUTHENTIC and not self.is_authentic_version:
            raise ValueError("authenticity=authentic противоречит is_authentic_version=False")
        if self.authenticity is not Authenticity.AUTHENTIC and self.is_authentic_version:
            raise ValueError(
                f"authenticity={self.authenticity.value} не может сопровождать "
                "is_authentic_version=True: неизвестное не становится подтверждённым (принцип V)"
            )
        return self

    @model_validator(mode="after")
    def the_notice_may_not_hide_what_the_lawyer_must_see(self) -> "ProvenanceStamp":
        """Построить фразу или проверить, что переданная ничего не умалчивает."""
        if not self.notice.strip():
            object.__setattr__(self, "notice", self._build_notice())
            return self

        lowered = self.notice.lower()
        if self.stale and _STALE_MARKER not in lowered and "недоступн" not in lowered:
            raise ValueError(
                "копия устарела, но текст для юриста об этом молчит: служебное поле "
                "пометкой не является (принцип IV)"
            )
        if self.is_translation and _TRANSLATION_MARKER not in lowered:
            raise ValueError(
                "текст является переводом, но фраза для юриста об этом молчит (принцип V)"
            )
        if self.attribution and self.attribution not in self.notice:
            raise ValueError(f"атрибуция {self.attribution!r} обязана быть воспроизведена в ответе")
        return self

    # -- производные -------------------------------------------------------

    @property
    def may_be_quoted_as_norm(self) -> bool:
        """Можно ли подавать этот текст как норму.

        Нельзя, если это перевод, не аутентичная версия, устаревшая копия,
        карточка/резюме/разъяснение вместо текста или содержимое, не
        являющееся текстом (метаданные, заглушка).
        """
        return (
            self.is_authentic_version
            and not self.is_translation
            and not self.stale
            and self.confirmable
        )

    @property
    def confirmable(self) -> bool:
        """Годится ли содержимое как доказательство цитаты (FR-204, T129).

        Карточка, резюме и разъяснение не подтверждают текст; неизвестный вид
        содержимого тоже не подтверждает — неизвестное не считается полным.
        """
        return (
            self.content_kind in _CONFIRMABLE_CONTENT
            and self.publication_kind not in _NON_TEXT_PUBLICATIONS
        )

    def as_envelope(self) -> dict[str, Any]:
        """Конверт в виде словаря для вливания в ответ инструмента."""
        return self.model_dump(mode="json")

    def _build_notice(self) -> str:
        parts: list[str] = []

        if self.stale:
            parts.append(
                "Джерело недоступне — відповідь із застарілої локальної копії, "
                "яку джерело могло вже змінити"
            )
        elif self.source_channel is SourceChannel.LIVE:
            parts.append("Відповідь отримано наживо з офіційного джерела")
        elif self.source_channel is SourceChannel.INDEX:
            parts.append("Відповідь із локального кешу")
        else:
            parts.append("Відповідь із вивіреного нами набору")

        if self.cached_at:
            age = f", вік {self.age_days} дн." if self.age_days is not None else ""
            parts.append(f" (знято {self.cached_at}{age})")
        if self.verified_at:
            parts.append(f"; звірено з першоджерелом {self.verified_at}")

        parts.append(f". Джерело: {self.source_url}")
        parts.append(f". Мова тексту: {self.language}")
        if self.version_id:
            parts.append(f". Редакція/версія: {self.version_id}")

        if self.publication_kind is PublicationKind.CONSOLIDATED:
            parts.append(
                ". Це інформаційна консолідація, а не автентична офіційна публікація; "
                "у разі розбіжності перевагу має офіційна публікація"
            )
        elif self.publication_kind is PublicationKind.OFFICIAL_JOURNAL_NOTICE:
            parts.append(
                ". Це повідомлення в Офіційному віснику про відкриття провадження, "
                "а не рішення суду"
            )
        elif self.publication_kind is PublicationKind.CARD:
            parts.append(". Це картка документа, а не його текст")
        elif self.publication_kind is PublicationKind.SUMMARY:
            parts.append(". Це резюме, а не текст документа")
        elif self.publication_kind is PublicationKind.ADMINISTRATIVE_GUIDANCE:
            parts.append(". Це роз'яснення органу, а не норма")

        if self.is_translation:
            parts.append(
                ". Це переклад, а не текст норми"
                + (
                    "; джерело позначило переклад як застарілий"
                    if self.translation_outdated
                    else ""
                )
            )
        elif not self.is_authentic_version:
            if self.authenticity is Authenticity.UNKNOWN:
                parts.append(". Автентичність наведеної мовної версії не встановлено")
            else:
                parts.append(". Наведена версія не є автентичною")

        if self.content_kind is ContentKind.METADATA:
            parts.append(". Отримано лише метадані, повного тексту немає")

        if self.stale:
            parts.append(". Перевірте чинність за першоджерелом перед використанням")
        if self.attribution:
            parts.append(f". {self.attribution}")

        return "".join(parts)


#: Поля конверта. Контракты ответа проверяют по этому перечню, что ответ
#: с текстом нормы несёт происхождение целиком, а не наполовину.
ENVELOPE_FIELDS: frozenset[str] = frozenset(ProvenanceStamp.model_fields)


def _parse_timestamp(value: Any) -> dt.datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def stamp_from_cache_entry(
    entry: dict[str, Any] | None,
    *,
    source_url: str,
    language: str = "uk",
    citation_format: str = "стаття N акта",
    is_authentic_version: bool = True,
    is_translation: bool = False,
    translation_outdated: bool | None = None,
    attribution: str | None = None,
    verified_at: str | None = None,
    publication_kind: PublicationKind = PublicationKind.CONSOLIDATED,
    content_kind: ContentKind = ContentKind.UNKNOWN,
    version_id: str | None = None,
    content_hash: str | None = None,
) -> ProvenanceStamp:
    """Собрать конверт из записи горячего кеша.

    Мост между существующим украинским каналом, который оперирует словарями
    записей кеша, и конвертом контракта. ``source_channel`` — ``live``, когда
    текст получен во время этого вызова, и ``index``, когда он пришёл из
    рабочего набора. Текст zakon.rada.gov.ua — систематизированная чинна
    редакція, поэтому по умолчанию ``publication_kind=consolidated``.
    """
    data = entry or {}
    from_cache = bool(data.get("from_cache"))
    stale = bool(data.get("stale"))
    cached_at = str(data.get("scraped_at") or data.get("retrieved_at") or "").strip()

    age_days: float | None = None
    parsed = _parse_timestamp(cached_at)
    if parsed is not None:
        age_days = round((dt.datetime.now(timezone.utc) - parsed).total_seconds() / 86400, 2)

    channel = SourceChannel.INDEX if from_cache else SourceChannel.LIVE
    if verified_at:
        channel = SourceChannel.CURATED

    return ProvenanceStamp(
        source_channel=channel,
        source_url=str(data.get("url") or source_url),
        language=language,
        citation_format=citation_format,
        stale=stale,
        is_authentic_version=is_authentic_version,
        is_translation=is_translation,
        cached_at=cached_at or None,
        age_days=age_days,
        verified_at=verified_at,
        translation_outdated=translation_outdated,
        attribution=attribution,
        publication_kind=publication_kind,
        content_kind=content_kind,
        version_id=version_id or (str(data.get("amendment_date") or "").strip() or None),
        content_hash=content_hash or (str(data.get("content_hash") or "").strip() or None),
        fetched_at=cached_at or None,
    )
