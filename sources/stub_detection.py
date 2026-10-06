"""Распознавание обрубков (T014).

**Обрубок** — сохранённая или полученная запись, текст которой не является
документом: оглавление, страница ошибки или страница проверки на автоматический
доступ. Он опаснее отсутствия записи, потому что выглядит здоровым — объём
заполнен, источник помечен доступным — и молча подменяет основание ответа
(CONTEXT.md, принцип III).

До этой задачи проверка жила в двух местах и в двух разных видах: пороги длины
в ``scraper.py`` (``len(text) < 500`` дважды) и список антибот-маркеров в
``court_registry.py``, привязанный к формулировкам ЄДРСР. Ни то ни другое не
переносилось на новые источники. Здесь оба вида сведены в одну проверку, а
результат — не булево, а :class:`StubVerdict` с признаком, по которому подмена
распознана: отказ ``upstream_stub_detected`` обязан этот признак назвать.

Порядок проверок значим. Сначала маркеры, потом объём: страница проверки на
автоматический доступ бывает длинной и порог длины её не поймает, а короткий,
но настоящий фрагмент не должен объявляться заглушкой только за то, что он
короткий, — поэтому объём проверяется последним и только при отсутствии
содержательных признаков.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

__all__ = [
    "DEFAULT_MIN_CHARS",
    "StubKind",
    "StubVerdict",
    "detect_stub",
    "is_stub",
]

#: Порог, ниже которого текст перестаёт быть отличим от страницы ошибки или
#: редиректа. Прежнее значение из ``scraper.py``, сохранено как есть.
DEFAULT_MIN_CHARS = 500


class StubKind(str, Enum):
    """Чем именно оказалась запись вместо документа."""

    ANTIBOT_CHALLENGE = "antibot_challenge"
    ERROR_PAGE = "error_page"
    TABLE_OF_CONTENTS = "table_of_contents"
    ACCESS_DENIED = "access_denied"
    TOO_SHORT = "too_short"
    EMPTY = "empty"


@dataclass(frozen=True)
class StubVerdict:
    """Признак, по которому распознана подмена.

    Отдаётся в отказ ``upstream_stub_detected`` целиком: юрист и разработчик
    должны видеть, почему ответ источника был отклонён, иначе отклонение
    неотличимо от произвола.
    """

    kind: StubKind
    marker: str
    length: int

    def as_detected_by(self) -> str:
        """Строка для поля ``detected_by`` типизированного отказа."""
        if self.marker:
            return f"{self.kind.value}: {self.marker}"
        return f"{self.kind.value} ({self.length} символів)"


#: Страницы проверки на автоматический доступ. Первые три — измеренные
#: формулировки ЄДРСР, перенесённые из ``court_registry.py`` без изменений;
#: остальные покрывают общие для государственных интерфейсов случаи.
_ANTIBOT_MARKERS: tuple[str, ...] = (
    "запроваджено інтерактивний елемент захисту",
    "введіть суму цифр",
    "інтерактивний елемент захисту системи",
    "підтвердьте, що ви не робот",
    "перевірка безпеки",
    "checking your browser",
    "just a moment",
    "enable javascript and cookies to continue",
    "please verify you are a human",
    "unusual traffic from your computer",
    "captcha",
    "cf-browser-verification",
    "recaptcha",
)

#: Страницы ошибок, отданные с кодом 200.
_ERROR_MARKERS: tuple[str, ...] = (
    "сторінку не знайдено",
    "сторінка не знайдена",
    "документ не знайдено",
    "запитуваний документ відсутній",
    "404 not found",
    "500 internal server error",
    "503 service unavailable",
    "service temporarily unavailable",
    "an error has occurred",
    "the requested url was not found",
)

#: Отказ в доступе: данные есть, но не нам.
_ACCESS_DENIED_MARKERS: tuple[str, ...] = (
    "доступ заборонено",
    "необхідна авторизація",
    "403 forbidden",
    "access denied",
    "subscription required",
    "please log in to continue",
)

#: Оглавление вместо документа. Само по себе слово «зміст» ничего не значит —
#: оно стоит в начале множества настоящих актов, — поэтому оглавление опознаётся
#: сочетанием признаков, а не словом (см. ``_looks_like_contents``).
_CONTENTS_MARKERS: tuple[str, ...] = (
    "перелік документів",
    "table of contents",
    "document index",
    "результати пошуку:",
)

#: Строка-пункт оглавления: номер, точка и короткий заголовок.
_CONTENTS_LINE = re.compile(r"^\s*(?:\d{1,3}[.)]|[-—•*])\s+\S")

#: Длина строки, начиная с которой это уже сплошной текст, а не пункт перечня.
#: Один порог на обе проверки: если бы форма и маркер считали прозу по-разному,
#: они противоречили бы друг другу на одном и том же документе.
_PROSE_LINE_CHARS = 200


def _find(haystack: str, markers: tuple[str, ...]) -> str | None:
    for marker in markers:
        if marker in haystack:
            return marker
    return None


def _prose_lines(text: str) -> int:
    """Сколько в тексте строк сплошной прозы.

    Оглавление состоит из ссылок целиком: длинных строк в нём нет по устройству.
    Документ, в котором они есть, оглавлением не является, как бы ни назывался
    его заголовок.
    """
    return sum(1 for line in text.splitlines() if len(line.strip()) > _PROSE_LINE_CHARS)


def _looks_like_contents(text: str) -> str | None:
    """Оглавление: много коротких пронумерованных строк и почти нет прозы.

    Порог намеренно высокий. Настоящий акт тоже содержит нумерацию, но между
    пунктами у него есть текст; оглавление же состоит из ссылок целиком.
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) < 8:
        return None

    listed = sum(1 for line in lines if _CONTENTS_LINE.match(line))
    prose = _prose_lines(text)
    if listed >= 8 and listed / len(lines) >= 0.7 and prose == 0:
        return f"{listed} з {len(lines)} рядків — пункти переліку, суцільного тексту немає"
    return None


def detect_stub(
    text: str,
    *,
    min_chars: int | None = DEFAULT_MIN_CHARS,
    extra_markers: tuple[str, ...] = (),
) -> StubVerdict | None:
    """Вернуть заключение, если пришёл не документ, и ``None``, если документ.

    ``min_chars=None`` отключает проверку объёма — для источников, у которых
    короткий ответ законен (одна статья, один статус договора).
    ``extra_markers`` добавляет формулировки конкретного источника к общим.
    """
    raw = str(text or "")
    stripped = raw.strip()
    if not stripped:
        return StubVerdict(kind=StubKind.EMPTY, marker="", length=0)

    lowered = stripped.lower()

    marker = _find(lowered, tuple(m.lower() for m in extra_markers))
    if marker is not None:
        return StubVerdict(kind=StubKind.ANTIBOT_CHALLENGE, marker=marker, length=len(stripped))

    marker = _find(lowered, _ANTIBOT_MARKERS)
    if marker is not None:
        return StubVerdict(kind=StubKind.ANTIBOT_CHALLENGE, marker=marker, length=len(stripped))

    marker = _find(lowered, _ACCESS_DENIED_MARKERS)
    if marker is not None:
        return StubVerdict(kind=StubKind.ACCESS_DENIED, marker=marker, length=len(stripped))

    marker = _find(lowered, _ERROR_MARKERS)
    if marker is not None:
        return StubVerdict(kind=StubKind.ERROR_PAGE, marker=marker, length=len(stripped))

    # Маркер оглавления сам по себе — ещё не оглавление. Длинное решение
    # Загального суду (232 КБ, 333 строки сплошной прозы) несёт подлинный
    # заголовок «Table of contents» и до этой правки отвергалось как заглушка:
    # настоящий документ объявлялся подменой.
    # Модуль и раньше знал верное правило — «оглавление опознаётся сочетанием
    # признаков, а не словом» (комментарий над _CONTENTS_MARKERS), — но код
    # ему не следовал. Теперь маркер решает только там, где сплошного текста
    # нет; где он есть, слово в заголовке ничего не доказывает.
    marker = _find(lowered, _CONTENTS_MARKERS)
    if marker is not None and _prose_lines(stripped) == 0:
        return StubVerdict(kind=StubKind.TABLE_OF_CONTENTS, marker=marker, length=len(stripped))

    shape = _looks_like_contents(stripped)
    if shape is not None:
        return StubVerdict(kind=StubKind.TABLE_OF_CONTENTS, marker=shape, length=len(stripped))

    if min_chars is not None and len(stripped) < min_chars:
        return StubVerdict(
            kind=StubKind.TOO_SHORT,
            marker=f"{len(stripped)} символів при порозі {min_chars}",
            length=len(stripped),
        )

    return None


def is_stub(
    text: str,
    *,
    min_chars: int | None = DEFAULT_MIN_CHARS,
    extra_markers: tuple[str, ...] = (),
) -> bool:
    """Булев вид :func:`detect_stub` для мест, которым признак не нужен."""
    return detect_stub(text, min_chars=min_chars, extra_markers=extra_markers) is not None
