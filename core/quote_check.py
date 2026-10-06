"""Звірка цитати з офіційним текстом одиниці (тікет 17).

Модуль відповідає на одне питання: чи є поданий рядок у прочитаному тексті —
і якщо не дослівно, то наскільки близько. Він **не** читає джерел, не веде
стану й не реєструє доказів; читання лишається за ``core.routing.perform_read``,
тобто тими самими читачами, якими ходять ``get_article`` і ``get_decision``.

Межа, яку цей модуль не переходить: збіг тексту — факт про рядки, а не про
право. Те, що цитата дослівна, не означає, що вона підтверджує тезу, заради
якої її наводять.

Нормалізація навмисно бідна і перелічена в ``NORMALIZATION_RULES``: вона
прибирає розбіжності набору (пробіли, типографські лапки й апострофи, тире,
м'які переноси, нуль-ширинні символи, нерозривні пробіли) і не чіпає літер.
Латиське ``ē`` не стає ``e``, українське ``і`` не стає ``и``: інакше
«нормалізований збіг» почав би приховувати змістову підміну.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Mapping

if TYPE_CHECKING:  # pragma: no cover - только для типов
    from core.legal_orders import DocumentClass

__all__ = [
    "MAX_FUZZY_CHARS",
    "MIN_QUOTE_CHARS",
    "NORMALIZATION_RULES",
    "NOT_A_LEGAL_CONCLUSION",
    "NormalizationRule",
    "QuoteMatch",
    "match_quote",
    "too_short_reason",
    "verify",
]

#: Застереження, без якого відповідь читалася б як правовий висновок.
NOT_A_LEGAL_CONCLUSION: Final[str] = (
    "Збіг тексту означає лише те, що цей рядок стоїть у джерелі за цією адресою. "
    "Він не доводить, що цитата підтверджує тезу, заради якої її наводять, "
    "і не замінює прочитання одиниці цілком."
)

#: Коротша цитата не перевіряється: на десятку символів «дослівний збіг»
#: трапляється випадково і нічого не доводить.
MIN_QUOTE_CHARS: Final[int] = 15

#: Скільки символів контексту віддавати з кожного боку за умовчанням.
DEFAULT_CONTEXT_CHARS: Final[int] = 80

#: Межа, за якою найближчий фрагмент не шукається. Нечіткий пошук коштує
#: ``O(n·m)``: на консолідованому акті з Додатком I (понад мільйон символів)
#: це секунди на кожен виклик. Дослівний і нормалізований збіг такої межі не
#: мають — вони лінійні, — тож обмеження стосується лише розбіжності, і тоді
#: відповідь прямо каже, що фрагмент не шукали, замість тихої паузи.
MAX_FUZZY_CHARS: Final[int] = 200_000

#: Скільки символів спільного тексту роблять «найближчий фрагмент» осмисленим.
#: Один випадковий пробіл спільним місцем не є, і видавати за нього початок
#: документа — гірше, ніж не видавати нічого.
_MIN_ANCHOR_CHARS: Final[int] = 4


@dataclass(frozen=True)
class NormalizationRule:
    """Одне правило нормалізації: ідентифікатор для відповіді і пояснення."""

    rule_id: str
    description: str


NORMALIZATION_RULES: Final[tuple[NormalizationRule, ...]] = (
    NormalizationRule(
        "whitespace",
        "пробіли, табуляції, переноси рядків і керівні символи зведено до одного "
        "пробілу, краї обрізано",
    ),
    NormalizationRule("quotes", "типографські лапки («», “”, „“, ″) зведено до прямої лапки"),
    NormalizationRule(
        "apostrophes", "типографські апострофи й штрихи (’, ‘, ′, ´, `) зведено до прямого"
    ),
    NormalizationRule("dashes", "дефіси й тире (‐, ‑, ‒, –, —, ―, −) зведено до дефіса"),
    NormalizationRule("soft_hyphen", "м'які переноси (U+00AD) прибрано"),
    NormalizationRule(
        "invisible",
        "нуль-ширинні символи (U+200B, U+200C, U+200D) і мітку порядку байтів (U+FEFF) прибрано",
    ),
    NormalizationRule(
        "nbsp", "нерозривні, вузькі та типографські пробіли зведено до звичайного пробілу"
    ),
)

#: Символ → (на що замінити, чиє це правило). ``None`` означає «прибрати».
_REPLACEMENTS: Final[dict[str, tuple[str | None, str]]] = {
    **{ch: (None, "soft_hyphen") for ch in "­"},
    **{ch: (None, "invisible") for ch in "​‌‍﻿"},
    **{ch: ('"', "quotes") for ch in "«»“”„‟″‶"},
    **{ch: ("'", "apostrophes") for ch in "‘’‚‛′‵ʼ´`"},
    **{ch: ("-", "dashes") for ch in "‐‑‒–—―−"},
    **{ch: (" ", "nbsp") for ch in "          "},
}

#: Слово або проміжок між словами: розмітка розбіжностей іде по словах.
_WORDS: Final[re.Pattern[str]] = re.compile(r"\S+|\s+")

#: Порядок правил у відповіді — той самий, що в переліку.
_RULE_ORDER: Final[tuple[str, ...]] = tuple(rule.rule_id for rule in NORMALIZATION_RULES)


def too_short_reason(quote: str) -> str:
    """Єдине формулювання правила мінімальної довжини цитати.

    Одне місце на всі входи: інструмент, модель контракту й чиста звірка
    повторюють ту саму фразу, а не три схожі.
    """
    return (
        f"цитата закоротка для звірки: потрібно щонайменше {MIN_QUOTE_CHARS} "
        f"символів, подано {len(quote.strip())}"
    )


@dataclass(frozen=True)
class QuoteMatch:
    """Результат звірки одного рядка з одним текстом.

    ``start``/``end`` — межі знайденого фрагмента в **оригінальному** тексті:
    відповідь показує те, що написано в джерелі, а не нормалізовану копію.
    ``differences`` рахуються на тому самому написанні, що й ``fragment``, щоб
    у відповіді не стояло поруч двох різних версій одного рядка.
    ``reason`` заповнюється тоді, коли фрагмента немає, і пояснює чому.
    """

    status: str
    start: int
    end: int
    fragment: str
    context_before: str
    context_after: str
    applied_rules: tuple[str, ...]
    differences: str
    reason: str = ""


def _normalize(text: str, disabled: frozenset[str] = frozenset()) -> tuple[str, list[int]]:
    """Нормалізований рядок і карта позицій у оригінал.

    ``disabled`` вимикає названі правила: цим перевіряється, чи правило взагалі
    вплинуло на збіг. Карта потрібна, щоб позиція збігу вказувала на оригінал:
    юрист цитує те, що надрукувало джерело, а не те, що зручно зіставляти.
    """
    collapse = "whitespace" not in disabled
    chars: list[str] = []
    index: list[int] = []
    previous_is_space = False
    lookup = _REPLACEMENTS.get

    for position, char in enumerate(text):
        rule = lookup(char)
        if rule is not None:
            replacement, rule_id = rule
            if rule_id in disabled:
                replacement = char
        elif collapse and (char.isspace() or char < " "):
            replacement = " "
        else:
            replacement = char

        if replacement is None:
            continue
        if replacement == " ":
            if previous_is_space and collapse:
                continue
            previous_is_space = True
        else:
            previous_is_space = False
        chars.append(replacement)
        index.append(position)

    if not collapse:
        return "".join(chars), index

    # Краї обрізаються разом із картою: інакше позиція з'їхала б на пробіл.
    first, last = 0, len(chars)
    while first < last and chars[first] == " ":
        first += 1
    while last > first and chars[last - 1] == " ":
        last -= 1
    return "".join(chars[first:last]), index[first:last]


def _rules_that_mattered(fragment: str, quote: str) -> tuple[str, ...]:
    """Правила, без яких ці два рядки не збіглися б.

    Правило називається лише тоді, коли його вимкнення ламає збіг. Тому тире,
    що стоїть у документі поза знайденим фрагментом, до переліку не потрапляє:
    перелік описує саме цей збіг, а не весь документ. Якщо не знадобилося
    жодного — рядки збіглися б і без нормалізації, і статус був би ``exact``.
    """
    if fragment == quote:
        return ()
    mattered: list[str] = []
    for rule_id in _RULE_ORDER:
        off = frozenset({rule_id})
        if _normalize(fragment, off)[0] != _normalize(quote, off)[0]:
            mattered.append(rule_id)
    return tuple(mattered)


def _mark_differences(found: str, quote: str) -> str:
    """Відмінності двох рядків у вигляді, придатному для читання людиною.

    ``[-…-]`` — те, що стоїть у джерелі; ``{+…+}`` — те, що стоїть у цитаті.

    Зіставляються слова, а не символи: посимвольна різниця дає ``[-sh-]{+m+}a``
    замість ``[-shall-]{+may+}`` і читається не юристом, а машиною.
    """
    source_words = _WORDS.findall(found)
    quote_words = _WORDS.findall(quote)
    marked: list[str] = []
    matcher = difflib.SequenceMatcher(None, source_words, quote_words, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        source_part = "".join(source_words[i1:i2])
        quote_part = "".join(quote_words[j1:j2])
        if tag == "equal":
            marked.append(source_part)
        elif tag == "delete":
            marked.append(f"[-{source_part}-]")
        elif tag == "insert":
            marked.append(f"{{+{quote_part}+}}")
        else:
            marked.append(f"[-{source_part}-]{{+{quote_part}+}}")
    return "".join(marked)


def _nearest_window(normalized_text: str, normalized_quote: str) -> tuple[int, int]:
    """Межі найближчого фрагмента тієї ж довжини — або порожній проміжок.

    Порожній проміжок означає «спільного місця немає». Видати замість нього
    перші N символів документа було б гірше за мовчання: юрист побачив би
    заголовок акта як «найближчий фрагмент» до своєї цитати.
    """
    length = len(normalized_quote)
    if not normalized_text or not length:
        return 0, 0
    matcher = difflib.SequenceMatcher(None, normalized_text, normalized_quote, autojunk=False)
    block = matcher.find_longest_match(0, len(normalized_text), 0, length)
    if block.size < min(_MIN_ANCHOR_CHARS, length):
        return 0, 0
    start = max(0, min(block.a - block.b, max(0, len(normalized_text) - length)))
    return start, min(start + length, len(normalized_text))


def _span_in_original(index: list[int], start: int, end: int) -> tuple[int, int]:
    """Перевести межі нормалізованого рядка в межі оригіналу."""
    if not index or start >= end:
        return 0, 0
    first = index[start]
    last = index[min(end, len(index)) - 1]
    return first, last + 1


def _built(
    text: str,
    start: int,
    end: int,
    status: str,
    applied_rules: tuple[str, ...],
    differences: str,
    context: int,
    reason: str = "",
) -> QuoteMatch:
    """Зібрати відповідь із межами в оригінальному тексті."""
    return QuoteMatch(
        status=status,
        start=start,
        end=end,
        fragment=text[start:end],
        context_before=text[max(0, start - context) : start],
        context_after=text[end : end + context],
        applied_rules=applied_rules,
        differences=differences,
        reason=reason,
    )


def match_quote(text: str, quote: str, *, context: int = DEFAULT_CONTEXT_CHARS) -> QuoteMatch:
    """Звірити цитату з текстом одиниці.

    Три відповіді, і всі три названі: ``exact`` — рядок стоїть у тексті
    дослівно; ``normalized`` — збігся після перелічених правил, і названі саме
    ті правила, без яких збігу не було б; ``mismatch`` — не збігся, і тоді
    повертається найближчий фрагмент такої ж довжини з позначеними
    відмінностями. Якщо спільного місця немає або текст завеликий для
    нечіткого пошуку, фрагмента немає, і ``reason`` каже, чому.
    """
    if len(quote.strip()) < MIN_QUOTE_CHARS:
        raise ValueError(too_short_reason(quote))

    # Швидкий шлях: пошук підрядка в C, без нормалізації мільйона символів.
    literal = text.find(quote)
    if literal >= 0:
        return _built(text, literal, literal + len(quote), "exact", (), "", context)

    normalized_text, index = _normalize(text)
    normalized_quote, _ = _normalize(quote)

    found = normalized_text.find(normalized_quote) if normalized_quote else -1
    if found >= 0:
        start, end = _span_in_original(index, found, found + len(normalized_quote))
        rules = _rules_that_mattered(text[start:end], quote)
        return _built(text, start, end, "normalized", rules, "", context)

    if len(normalized_text) > MAX_FUZZY_CHARS:
        return _built(
            text,
            0,
            0,
            "mismatch",
            (),
            "",
            context,
            reason=(
                f"Цитати в тексті немає. Найближчий фрагмент не шукали: текст завеликий "
                f"({len(normalized_text)} символів проти межі {MAX_FUZZY_CHARS}). "
                "Звузьте адресу (``path``) до потрібної одиниці."
            ),
        )

    window_start, window_end = _nearest_window(normalized_text, normalized_quote)
    if window_start >= window_end:
        return _built(
            text,
            0,
            0,
            "mismatch",
            (),
            "",
            context,
            reason=(
                "Цитати в тексті немає, і спільного місця з ним теж: найближчого "
                "фрагмента не існує. Перевірте, чи та це одиниця й та редакція."
            ),
        )

    start, end = _span_in_original(index, window_start, window_end)
    fragment = text[start:end]
    return _built(text, start, end, "mismatch", (), _mark_differences(fragment, quote), context)


# ---------------------------------------------------------------------------
# Читання: тими самими читачами, що ``get_article`` і ``get_decision``
# ---------------------------------------------------------------------------


class _ForgetfulEntry:
    """Квитанція «нічого не запам'ятали»: доказу тут немає за побудовою."""

    evidence_id = ""


class _ForgetfulStore:
    """Пам'ять доказів, яка нічого не пам'ятає.

    ``verify_quote`` читає, але доказу не створює: звірка цитати — питання про
    рядок, а не підстава цитувати. Підставивши цю пам'ять у ``perform_read``,
    ми лишаємо весь шлях читання спільним із ``get_article`` і не витрачаємо
    квоту доказів сеансу на перевірку.
    """

    def remember(self, *args: Any, **kwargs: Any) -> _ForgetfulEntry:
        return _ForgetfulEntry()


#: Відмови джерела, після яких має сенс спробувати інший вид документа.
_RETRYABLE: Final[frozenset[str]] = frozenset({"not_found", "invalid_input", "not_covered"})

#: Відмова джерела → статус звірки. ``not_found`` тут немає навмисно: відсутній
#: документ і відсутня адреса всередині документа — різні відповіді, і який із
#: них перед нами, вирішує ``_status_for_failure``.
_FAILURE_STATUS: Final[dict[str, str]] = {
    "not_covered": "not_covered",
    "blocked_by_license": "not_covered",
    "source_unavailable": "source_unavailable",
    "upstream_stub_detected": "source_unavailable",
}

_DECISION_ID: Final[re.Pattern[str]] = re.compile(
    # ECLI будь-якого форуму, CELEX судового документа (6…), itemid HUDOC.
    r"^(?:ecli:|6\d{4}[a-z]{2}\d|001-\d+)",
    re.IGNORECASE,
)


def _status_for_failure(failure: Mapping[str, Any], path: str) -> str:
    """Статус, яким називається відмова читання.

    ``not_found`` про документ і ``path_not_found`` про пункт усередині нього —
    різні факти: перший означає «такого документа в джерелі немає», другий —
    «документ є, потрібної одиниці в ньому немає». Злиття їх в один статус
    відправляло б юриста шукати не те.
    """
    code = str(failure.get("code") or "")
    named = _FAILURE_STATUS.get(code)
    if named is not None:
        return named
    if code in ("not_found", "invalid_input", "no_canonical_text"):
        details = failure.get("details")
        identifier = ""
        if isinstance(details, Mapping):
            identifier = str(details.get("identifier") or "")
        if path and identifier.strip().casefold() == path.strip().casefold():
            return "path_not_found"
        return "not_found"
    return "path_not_found"


def _looks_like_a_decision(document_id: str) -> bool:
    """Чи схожий ідентифікатор на судовий документ.

    Здогад, а не істина: якщо читання за здогадом дало «немає такого»,
    ``verify`` пробує другий вид документа. Здогад лише економить один запит.
    """
    return bool(_DECISION_ID.match(document_id.strip()))


def _classes_to_try(document_class: "DocumentClass", document_id: str) -> tuple[Any, ...]:
    """Види документа в порядку спроб, без повторів.

    Друга спроба йде з **тим самим** ``document_id`` — змінюється лише читач,
    тому підмінити документ вона не може; що саме спрацювало, відповідь називає
    полями ``document_class`` і ``document_class_guessed``.
    """
    from core.legal_orders import DocumentClass as _Class

    if _looks_like_a_decision(document_id):
        order: list[Any] = [_Class.DECISION, document_class]
    elif document_class is _Class.ACT:
        order = [_Class.ACT, _Class.DECISION]
    else:
        order = [document_class]
    unique: list[Any] = []
    for item in order:
        if item not in unique:
            unique.append(item)
    return tuple(unique)


def _read_unit(
    *,
    legal_order: str,
    document_id: str,
    path: str,
    language: str,
    as_of: str,
    session_id: str,
    document_class: "DocumentClass",
    adapters: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Одне читання через спільний шлях ``perform_read``.

    ``update_source_cache=False`` — межа «без стану»: кеш публічних текстів
    відповідає на питання «джерело те саме чи змінилося», і якщо звірка
    перезапише його своїм читанням, наступний ``get_article`` скаже ``same``
    там, де норма насправді змінилася.
    """
    from core.legal_orders import Operation
    from core.routing import ReadRequest, perform_read

    return perform_read(
        ReadRequest(
            legal_order=legal_order,
            operation=Operation.READ_FRAGMENT if path else Operation.READ_DOCUMENT,
            document_class=document_class,
            document_id=document_id,
            path=path,
            language=language,
            as_of=as_of,
            tool="verify_quote",
        ),
        session_id=session_id,
        adapters=adapters,
        evidence_store=_ForgetfulStore(),
        update_source_cache=False,
    )


def verify(
    *,
    legal_order: str,
    document_id: str,
    path: str,
    quote: str,
    language: str = "",
    as_of: str = "",
    session_id: str,
    document_class: "DocumentClass",
    adapters: Mapping[str, Any] | None = None,
    context: int = DEFAULT_CONTEXT_CHARS,
) -> dict[str, Any]:
    """Звірити цитату з текстом за адресою — і повернути стислу відповідь.

    Текст одиниці цілком не повертається: назад іде статус, знайдений фрагмент
    із малим контекстом, позиція, редакція, мова, посилання, час зняття й
    автентичність. Порожній ``path`` означає пошук по всьому тексту документа.
    """
    from core.contracts import ErrorCode, friendly_error, is_failure, unknown_legal_order
    from core.legal_orders import is_known_legal_order, normalize_legal_order_code

    code = normalize_legal_order_code(legal_order)
    if not is_known_legal_order(code):
        return unknown_legal_order(legal_order)

    if len(quote.strip()) < MIN_QUOTE_CHARS:
        return friendly_error(
            f"quote: {too_short_reason(quote)}",
            ErrorCode.INVALID_INPUT,
            {
                "field": "quote",
                "manual_path": (
                    "Подайте довший уривок: короткий рядок збігається випадково "
                    "і нічого не підтверджує."
                ),
            },
        )

    candidates = _classes_to_try(document_class, document_id)
    first_failure: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    for candidate in candidates:
        attempt = _read_unit(
            legal_order=code,
            document_id=document_id,
            path=path,
            language=language,
            as_of=as_of,
            session_id=session_id,
            document_class=candidate,
            adapters=adapters,
        )
        if not is_failure(attempt):
            attempt["document_class"] = candidate.value
            result = attempt
            break
        if first_failure is None:
            first_failure = attempt
        if str(attempt.get("code") or "") not in _RETRYABLE:
            first_failure = attempt
            break

    if result is None:
        failure = dict(first_failure or {})
        failure["status"] = _status_for_failure(failure, path)
        failure.setdefault("legal_order", code)
        return failure

    guessed = len(candidates) > 1
    text = str(result.get("text") or "")
    if not text.strip():
        return {
            "legal_order": code,
            "document_id": document_id,
            "resolved_document_id": result.get("resolved_document_id"),
            "document_class": result.get("document_class"),
            "document_class_guessed": guessed,
            "path": path,
            "status": "path_not_found",
            "reason": "Джерело відповіло за цією адресою порожнім текстом: звіряти немає з чим.",
            "source_url": str(result.get("source_url") or ""),
            "fetched_at": str(result.get("fetched_at") or ""),
            "notice": NOT_A_LEGAL_CONCLUSION,
        }

    match = match_quote(text, quote, context=context)
    return {
        "legal_order": code,
        "document_id": document_id,
        "resolved_document_id": result.get("resolved_document_id"),
        "source_id": result.get("source_id"),
        "document_class": result.get("document_class"),
        "document_class_guessed": guessed,
        "path": path,
        "locator": str(result.get("locator") or ""),
        "status": match.status,
        "quote_length": len(quote),
        "fragment": match.fragment,
        "context_before": match.context_before,
        "context_after": match.context_after,
        "position": match.start,
        "normalizations": list(match.applied_rules),
        "differences": match.differences,
        "reason": match.reason,
        "revision": str(result.get("version_id") or ""),
        "language": str(result.get("language") or ""),
        "source_url": str(result.get("source_url") or ""),
        "fetched_at": str(result.get("fetched_at") or ""),
        "authenticity": str(result.get("authenticity") or ""),
        "confirmable": bool(result.get("confirmable")),
        "notice": NOT_A_LEGAL_CONCLUSION,
    }
