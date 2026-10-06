"""Адаптер права ЄС через Cellar (T055, T056; XHTML-розбір і резолвер T132–T135).

Cellar — машиночитане обличчя EUR-Lex: SPARQL-ендпоінт без ключа для пошуку
(``eu.cellar_sparql``) і REST зі згодженням змісту для самого документа
(``eu.cellar_resource`` / ``eu.cellar_celex``).

Живі проби (research/13-runtime-probes-2026-09-07.md) показали, що
``Accept: application/akn+xml`` Cellar відхиляє (400 «Illegal accept
header») — content negotiation підтримує лише ``application/xhtml+xml``
(і ``application/xml;type=fmx4``). Тому читання документа йде запитом
``Accept: application/xhtml+xml, application/xml;q=0.8`` з
``Accept-Language`` мовою ISO 639-2 (``eng``, ``lav``…), а розбір — по
розмітці ELI (``div.eli-subdivision#art_N``), а не по Akoma Ntoso eId.
Розмітка базового акта OJ і інформаційної консолідації різна (research/13,
§A1–A3) — обидва варіанти розбираються тут.

Ідентифікатори:

* **CELEX** — мовонезалежний ідентифікатор акта (``32016R0679``), головний ключ
  для ``fetch``/``search``. Дужки й слеші в ньому обов'язково кодуються
  (``32012Q0929%2801%29``, ``12016E%2FPRO%2F03``) — без кодування Cellar
  віддає 404 (research/13, §A5).
* **ELI** — стабільний URI з підрозділами до статті/пункту
  (``http://data.europa.eu/eli/reg/2016/679/oj``); будується з CELEX там, де
  тип документа відомий (§4 нижче), і супроводжує відповідь як додаткове поле,
  а не замінює CELEX.

Ліміт видачі — 10 000 записів на пошуковий запит (з 01.01.2026). Перевищення
дає :class:`~contracts.PartialResult`, а не мовчазне обрізання (принцип III).

Редакція на дату (ADR 0007) доводиться через :meth:`EuLawAdapter.resolve_revision`:
перелік датованих консолідацій береться SPARQL-зв'язком
``act_consolidated_based_on_resource_legal`` від базового акта, а коли SPARQL
мовчить — з дерева нотатки Cellar REST (``Accept: application/xml;notice=tree``),
так само й склад консолідації (жива проба 22.09.2026). Дата пізніше останньої
відомої консолідації — ``revision_unknown``, а не підстановка чинної редакції.

Без ``as_of`` базовий CELEX (сектор 3) читається в чинній консолідації —
найпізнішій з датою не пізніше сьогодні (:meth:`EuLawAdapter.current_revision`),
з ``known_through`` у відповіді. Первинний текст OJ читається, лише коли
консолідацій у Cellar немає: інакше стаття, внесена поправкою, відповідала
``not_found`` (живий виклик 15.09.2026).

Атрибуція обов'язкова (Commission Decision 2011/833/EU) і йде в кожен
успішний конверт.
"""

from __future__ import annotations

import copy
import datetime as dt
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import requests
from lxml import etree

from core.contracts import (
    InvalidIdentifier,
    NotCovered,
    NotFound,
    PartialResult,
    Revision,
    RevisionUnknown,
    SourceHealth,
    SourcePolicy,
    SourceUnavailable,
    TypedFailure,
)
from core.legal_orders import LEGAL_ORDERS, CoverageLayer, Source, SourceAccess, SourceLimits
from core.provenance import ContentKind, ProvenanceStamp, PublicationKind, SourceChannel
from core.source_endpoints import endpoint
from sources import register_adapter
from sources.base import AdapterPayload, AdapterResult, RegistryAdapter, typed_failures
from sources.transport import CellarReader, SourceTransport

__all__ = ["EuLawAdapter", "ATTRIBUTION_EU", "eli_from_celex", "extract_akn_unit"]

#: Рішення 2011/833/EU: reuse вільний за умови зазначення джерела.
ATTRIBUTION_EU = (
    "© Європейський Союз, https://eur-lex.europa.eu, {year}. Повторне використання "
    "дозволене Рішенням Комісії 2011/833/EU за умови зазначення джерела."
)

#: З 01.01.2026 Cellar не віддає більше цієї кількості записів на один запит
#: (research/01-eu-sources.md, §6).
CELLAR_RECORD_LIMIT = 10_000

_AKN_NS = {"akn": "http://docs.oasis-open.org/legaldocml/ns/akn/3.0"}

_XSD_STRING = "^^<http://www.w3.org/2001/XMLSchema#string>"

#: Тип документа з третьої літери CELEX → сегмент ELI. Тільки ті, що трапляються
#: у регуляторному масиві, який реально запитують (R-07).
_CELEX_TO_ELI_KIND = {
    "R": "reg",
    "L": "dir",
    "D": "dec",
}

_CELEX_RE = re.compile(r"^\d(?P<year>\d{4})(?P<kind>[A-Z])(?P<number>\d+)$")


def eli_from_celex(celex: str) -> str | None:
    """Побудувати ELI-URI з CELEX, коли тип документа відомий.

    Повертає ``None``, коли CELEX не за очікуваною формою або тип документа не
    мапиться однозначно на сегмент ELI — краще відсутнє поле, ніж вигаданий URI.
    """
    match = _CELEX_RE.match(str(celex or "").strip())
    if not match:
        return None
    kind = _CELEX_TO_ELI_KIND.get(match.group("kind"))
    if kind is None:
        return None
    number = str(int(match.group("number")))
    return f"http://data.europa.eu/eli/{kind}/{match.group('year')}/{number}/oj"


#: CELEX у формах, які Cellar справді видає: акт (``32016R0679``), суфікс
#: публікації (``32020Q0214(01)``), виправлення (``32016R0679R(02)``),
#: консолідація (``02016R0679-20160504``), протокол (``12016E/PRO/03``), справа
#: (``62016CJ0064``). Інше до SPARQL не йде: рядок вставляється в запит.
_CELEX_ID_RE = re.compile(
    r"^\d{5}[A-Z]{1,2}(?:\d{1,5}|/[A-Z]{3}/\d{2}|/TXT)(?:R?\(\d{2}\))*(?:-\d{8})?$"
)

#: Базовий акт OJ сектора 3 (із суфіксом публікації, без виправлень і дат) —
#: лише такий має датовані консолідації ``0…-YYYYMMDD``.
_BASE_ACT_CELEX_RE = re.compile(r"^3\d{4}[A-Z]{1,2}\d{1,5}(?:\(\d{2}\))?$")

#: Префікс ELI у формах, якими його копіюють: URI data.europa.eu, адреса
#: EUR-Lex, ресурс Publications Office або голий хвіст ``eli/…``.
_ELI_PREFIX_RE = re.compile(
    r"^(?:https?://(?:data\.europa\.eu|eur-lex\.europa\.eu|publications\.europa\.eu/resource)/)?"
    r"eli/",
    re.IGNORECASE,
)
_ELI_SEGMENT_RE = re.compile(r"^[a-z0-9_\-]+$")
#: Хвости маніфестації, що до ідентифікатора роботи не належать.
_ELI_FORMATS = frozenset({"html", "pdf", "xml", "xhtml", "txt", "rdf", "fmx4", "mul"})

CELEX_FORMAT_HINT = (
    "CELEX, наприклад 32016R0679, 32020Q0214(01), 02016R0679-20160504, "
    "або ELI http://data.europa.eu/eli/reg/2016/679/oj"
)


def normalize_eli(document_id: str) -> str | None:
    """Канонічний ELI роботи; ``None`` — це не ELI, ``""`` — ELI зіпсованої форми.

    Регістр зводиться до нижнього (ELI ЄС нижньорегістровий, а
    ``ELI/REG/2016/679/OJ`` давав хибний ``not_found``); рядок запиту, мовний
    вираз (``/eng``) і формат (``/html``) відкидаються. Дата консолідації
    (``…/679/2016-05-04``) — частина ідентифікатора роботи й лишається.
    """
    raw = re.split(r"[?#]", str(document_id or "").strip(), maxsplit=1)[0]
    match = _ELI_PREFIX_RE.match(raw)
    if not match:
        return None
    segments = [segment for segment in raw[match.end() :].lower().split("/") if segment]
    languages = set(_LANG_2_TO_3.values())
    while len(segments) > 3 and (segments[-1] in _ELI_FORMATS or segments[-1] in languages):
        segments.pop()
    if len(segments) < 3 or not all(_ELI_SEGMENT_RE.match(segment) for segment in segments):
        return ""
    return "http://data.europa.eu/eli/" + "/".join(segments)


def _sparql_string(value: str) -> str:
    """Рядковий літерал SPARQL з повним екрануванням (форму вже перевірено, це — друга межа)."""
    escaped = (
        str(value)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
    )
    return f'"{escaped}"'


def _eli_to_celex_query(eli: str) -> str:
    """ELI → CELEX. Типізований літерал ``xsd:anyURI`` — так Cellar зберігає ELI.

    Живо 16.09.2026: 0,6 с проти 1,4 с для ``FILTER(STR(?eli)=…)``.
    """
    return (
        "PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>\n"
        "SELECT DISTINCT ?celex WHERE {\n"
        f"  ?w cdm:resource_legal_eli {_sparql_string(eli)}"
        "^^<http://www.w3.org/2001/XMLSchema#anyURI> .\n"
        "  ?w cdm:resource_legal_id_celex ?celex .\n"
        "}"
    )


def _celex_preference(celex: str) -> tuple[int, int, str]:
    """Порядок CELEX одного ELI: акт перед виправленням ``R(nn)``, коротший першим."""
    return (1 if re.search(r"R\(\d{2}\)$", celex) else 0, len(celex), celex)


def _card_query(celex: str) -> str:
    """Один SPARQL-запит на картку роботи (предикати виміряні живо 16.09.2026).

    Багатозначні властивості згортаються: інакше декартів добуток мов,
    дат і ідентифікаторів давав би сотні рядків на один акт.
    """
    lang = "<http://publications.europa.eu/resource/authority/language/ENG>"
    return (
        "PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>\n"
        "SELECT (SAMPLE(?c) AS ?celex) (SAMPLE(?eli0) AS ?eli) (SAMPLE(?type0) AS ?type)\n"
        "  (SAMPLE(?legal_type0) AS ?legal_type) (SAMPLE(?sector0) AS ?sector)\n"
        "  (SAMPLE(?date0) AS ?date_document)\n"
        '  (GROUP_CONCAT(DISTINCT ?eif0; separator="|") AS ?entry_into_force)\n'
        '  (GROUP_CONCAT(DISTINCT ?eov0; separator="|") AS ?end_of_validity)\n'
        "  (SAMPLE(?in_force0) AS ?in_force)\n"
        '  (GROUP_CONCAT(DISTINCT ?docid0; separator="|") AS ?document_ids)\n'
        "  (SAMPLE(?oj_date0) AS ?oj_date) (SAMPLE(?title0) AS ?title)\n"
        '  (GROUP_CONCAT(DISTINCT ?lang0; separator="|") AS ?languages)\n'
        "WHERE {\n"
        f"  ?w cdm:resource_legal_id_celex {_sparql_string(celex)}{_XSD_STRING} .\n"
        "  ?w cdm:resource_legal_id_celex ?c .\n"
        "  OPTIONAL { ?w cdm:resource_legal_eli ?eli0 }\n"
        "  OPTIONAL { ?w cdm:work_has_resource-type ?type0 }\n"
        "  OPTIONAL { ?w cdm:resource_legal_type ?legal_type0 }\n"
        "  OPTIONAL { ?w cdm:resource_legal_id_sector ?sector0 }\n"
        "  OPTIONAL { ?w cdm:work_date_document ?date0 }\n"
        "  OPTIONAL { ?w cdm:resource_legal_date_entry-into-force ?eif0 }\n"
        "  OPTIONAL { ?w cdm:resource_legal_date_end-of-validity ?eov0 }\n"
        "  OPTIONAL { ?w cdm:resource_legal_in-force ?in_force0 }\n"
        "  OPTIONAL { ?w cdm:work_id_document ?docid0 }\n"
        "  OPTIONAL { ?w cdm:official-journal-act_date_publication ?oj_date0 }\n"
        "  OPTIONAL { ?ex cdm:expression_belongs_to_work ?w ;\n"
        f"      cdm:expression_uses_language {lang} ; cdm:expression_title ?title0 }}\n"
        "  OPTIONAL { ?ex2 cdm:expression_belongs_to_work ?w ;\n"
        "      cdm:expression_uses_language ?lang0 }\n"
        "}"
    )


def _split_values(raw: str) -> list[str]:
    return sorted({item.strip() for item in str(raw or "").split("|") if item.strip()})


def _single_or_list(raw: str) -> str | list[str] | None:
    """Одне значення — рядком, кілька — списком, жодного — ``None``."""
    values = _split_values(raw)
    if not values:
        return None
    return values[0] if len(values) == 1 else values


#: ``cdm:resource_legal_in-force`` — ``xsd:boolean``; живо 16.09.2026 Cellar
#: серіалізує його як «1»/«0», але лексична форма «true»/«false» теж законна.
_BOOLEAN_LEXICAL = {"1": True, "true": True, "0": False, "false": False}


def _card_data(row: dict[str, Any], *, language: str = "en") -> dict[str, Any]:
    """Компактна картка з рядка :func:`_card_query`; невідоме лишається ``None``."""
    celex = _binding_value(row, "celex")
    code3_to_2 = {code3: code2 for code2, code3 in _LANG_2_TO_3.items()}
    languages = sorted(
        code3_to_2.get(uri.rsplit("/", 1)[-1].lower(), uri.rsplit("/", 1)[-1].lower())
        for uri in _split_values(_binding_value(row, "languages"))
    )
    oj_ids = [
        item.split(":", 1)[1]
        for item in _split_values(_binding_value(row, "document_ids"))
        if item.startswith("oj:")
    ]
    in_force_raw = _binding_value(row, "in_force")
    return {
        "resolved_document_id": celex,
        # Cellar кладе в назву нерозривні пробіли («(EU)\xa02016/679»).
        "title": re.sub(r"\s+", " ", _binding_value(row, "title")).strip() or celex,
        "url": _celex_url(celex),
        "eurlex_url": (
            f"{endpoint('eu.eurlex')}/legal-content/{language.upper()}/ALL/?uri=CELEX:{celex}"
        ),
        "celex": celex,
        "eli": _binding_value(row, "eli") or None,
        "resource_type": _binding_value(row, "type").rsplit("/", 1)[-1] or None,
        "celex_sector": _binding_value(row, "sector") or None,
        "celex_document_type": _binding_value(row, "legal_type") or None,
        "date_document": _binding_value(row, "date_document") or None,
        "entry_into_force": _single_or_list(_binding_value(row, "entry_into_force")),
        # 9999-12-31 — так Cellar позначає відсутність кінцевої дати; не переписується.
        "end_of_validity": _single_or_list(_binding_value(row, "end_of_validity")),
        "in_force": _BOOLEAN_LEXICAL.get(in_force_raw.strip().lower()),
        "official_journal": (
            {
                "id": oj_ids[0],
                "publication_date": _binding_value(row, "oj_date") or None,
            }
            if oj_ids
            else None
        ),
        "languages": languages,
    }


def _article_ids(article: str) -> tuple[str, ...]:
    """Можливі eId для запитаної статті чи пункту в Akoma Ntoso.

    Використовується лише :func:`extract_akn_unit` — спільним розбором Akoma
    Ntoso для джерел, що публікують LegalDocML. Право ЄС через
    Cellar читається окремим розбором XHTML (``extract_xhtml_unit`` нижче):
    Cellar на ``Accept: application/akn+xml`` відповідає 400 (research/13).
    """
    cleaned = str(article or "").strip()
    if not cleaned:
        return ()
    return (
        f"art_{cleaned}",
        f"art{cleaned}",
        f"para_{cleaned}",
        f"para{cleaned}",
        cleaned,
    )


def extract_akn_unit(xml_text: str, path: str) -> str | None:
    """Витягти текст статті/пункту з Akoma Ntoso по ``eId``.

    Повертає ``None``, коли документ розібрати не вдалося або запитаної
    підрозділи в ньому немає — обидва випадки викликач перетворює на
    типізований відказ, а не підставляє порожній текст.

    Збережено як спільна крапка розбору Akoma Ntoso для джерел, які
    публікують LegalDocML.
    Cellar (право ЄС) відповідає на такий ``Accept`` кодом 400 і читається
    через :func:`extract_xhtml_unit`.
    """
    raw = str(xml_text or "").strip()
    if not raw:
        return None
    try:
        root = etree.fromstring(raw.encode("utf-8"))
    except etree.XMLSyntaxError:
        return None

    candidates = _article_ids(path)
    if not candidates:
        return None

    for eid in candidates:
        nodes = root.xpath(f'.//*[@eId="{eid}"]')
        if nodes:
            text = "".join(nodes[0].itertext()).strip()
            text = re.sub(r"\s+", " ", text)
            return text or None
    return None


# ---------------------------------------------------------------------------
# XHTML (Cellar): мова запиту та розбір локатора (T132–T134)
# ---------------------------------------------------------------------------

#: 24 автентичні мови ЄС (``legal_orders.LEGAL_ORDERS["EU"].authentic_languages``)
#: у двобуквеному вигляді → трибуквений код ISO 639-2 для ``Accept-Language``
#: Cellar (перевірено живим запитом лише для ``eng``; решта — та сама
#: термінографічна форма, якою Publications Office підписує мови виразів
#: консолідації, research/13 §A2 «BUL…SWE»).
_LANG_2_TO_3: dict[str, str] = {
    "bg": "bul",
    "cs": "ces",
    "da": "dan",
    "de": "deu",
    "el": "ell",
    "en": "eng",
    "es": "spa",
    "et": "est",
    "fi": "fin",
    "fr": "fra",
    "ga": "gle",
    "hr": "hrv",
    "hu": "hun",
    "it": "ita",
    "lt": "lit",
    "lv": "lav",
    "mt": "mlt",
    "nl": "nld",
    "pl": "pol",
    "pt": "por",
    "ro": "ron",
    "sk": "slk",
    "sl": "slv",
    "sv": "swe",
}


def _resolve_language(language: str) -> tuple[str, str] | None:
    """(двобуквений, трибуквений) код мови, або ``None``, якщо мова не покрита.

    Cellar видає право ЄС лише 24 офіційними мовами (ст. 55 ДЄС) — усі вони
    рівно автентичні, інших мов (у т.ч. української) у Cellar немає взагалі,
    тож непокрита мова — не переклад із застереженням, а ``not_covered``.
    """
    code2 = str(language or "").strip().lower() or "en"
    authentic = set(LEGAL_ORDERS["EU"].authentic_languages)
    if code2 not in authentic:
        return None
    code3 = _LANG_2_TO_3.get(code2)
    if code3 is None:
        return None
    return code2, code3


#: Кирилична «а»/«А» невідрізнима на око від латинської — локатор «5а» мусить
#: означати те саме, що «5a».
#: Кириличні літери, які в локаторі означають латинські. Крім «а» статті 5a
#: сюди входять римські цифри: користувач, що пише «Додаток» кирилицею,
#: набирає кирилицею і номер, і без цього «Додаток ІІ» (U+0406) не
#: розв'язувався б, тоді як «Annex II» розв'язується.
_CONFUSABLE_LOCATOR_CHARS = str.maketrans(
    {
        "а": "a",
        "А": "A",
        "І": "I",
        "і": "i",
        "Х": "X",
        "х": "x",
        "С": "C",
        "с": "c",
        "В": "V",
        "Ѵ": "V",
    }
)

#: «5a» / «2» / «2(1)» / «5(1)(a)» — стаття (з можливим буквеним суфіксом),
#: за потреби частина в дужках і підпункт літерою в других дужках.
_LOCATOR_RE = re.compile(
    r"^(?P<article>\d+[A-Za-z]?)(?:\((?P<para>\d+)\))?(?:\((?P<sub>[A-Za-z])\))?$"
)


#: Додаток акта ЄС: слово «Annex» (або його відповідник) і номер римською чи
#: арабською. Проба 2026-09-09 показала, що жодна з шести природних форм
#: (`Annex II`, `ANNEX II`, `II`, `Annex_II`, `annex-2`, `Додаток II`) не
#: розв'язувалася, хоча в XHTML Cellar додатки лежать під id `anx_I`, `anx_II`.
#: Це не дрібниця: акти часто відсилають до додатка зі статті, і без
#: локатора юрист його не прочитає.
#: Слова, якими додаток називають у локаторі. Кожне проганяється через ту саму
#: таблицю сплутуваних літер, що й сам локатор: без цього кирилична «Додаток»
#: після приведення «а» до латинської перестала б збігатися сама з собою.
_ANNEX_WORDS: tuple[str, ...] = (
    "annex",
    "додаток",
    "приложение",
    "pielikums",
    "priedas",
    "bijlage",
    "anexo",
)
_ANNEX_LOCATOR_RE = re.compile(
    "^(?:"
    + "|".join(
        sorted(
            {re.escape(word.translate(_CONFUSABLE_LOCATOR_CHARS)) for word in _ANNEX_WORDS}
            | {re.escape(word) for word in _ANNEX_WORDS}
        )
    )
    + r")?\s*[_\-.]?\s*(?P<number>[IVXLC]+|\d{1,2})$",
    re.IGNORECASE,
)

#: Рівно стільки, скільки буває додатків у акті; ширший перетворювач тут був би
#: кодом без застосування.
_ARABIC_TO_ROMAN: tuple[tuple[int, str], ...] = (
    (100, "C"),
    (90, "XC"),
    (50, "L"),
    (40, "XL"),
    (10, "X"),
    (9, "IX"),
    (5, "V"),
    (4, "IV"),
    (1, "I"),
)


def _roman(number: int) -> str:
    """Арабське число римським; 0 і від'ємні не бувають номером додатка."""
    if number <= 0:
        return ""
    text = ""
    left = number
    for value, glyph in _ARABIC_TO_ROMAN:
        while left >= value:
            text += glyph
            left -= value
    return text


def _split_annex_locator(path: str) -> str | None:
    """Номер додатка римськими з локатора, або ``None``, якщо це не додаток.

    Голе число («2») додатком НЕ вважається: воно вже означає статтю 2, і
    тлумачити його двома способами означало б віддати юристові не той текст,
    про який він спитав. Голе римське число («II») додатком вважається:
    статті римськими в актах ЄС не нумеруються.
    """
    cleaned = str(path or "").strip().translate(_CONFUSABLE_LOCATOR_CHARS)
    if not cleaned:
        return None
    match = _ANNEX_LOCATOR_RE.match(cleaned)
    if not match:
        return None
    number = match.group("number")
    named = bool(re.match(r"^[A-Za-zА-Яа-яЁёІіЇїЄєҐґ]", cleaned))
    if number.isdigit():
        # «annex 2» — так, «2» — ні: без слова це стаття.
        if not named:
            return None
        return _roman(int(number)) or None
    return number.upper()


def _locator_label(locator: str, unit_id: str = "") -> str:
    """Як локатор називається в надрукованому посиланні.

    Додаток називається додатком, а не статтею: «ст. Annex II» — не описка, а
    неправильна назва в рядку, який юрист копіює в подання. Так само пункт
    практичних вказівок (``unit_id`` ``point_N``) — «п.», а не «ст.»: статей у
    такому документі немає. Локатор наводиться канонічним, інакше та сама
    одиниця, набрана кирилицею, дала б інше посилання, ніж вона ж, набрана
    латиницею.
    """
    text = str(locator or "").strip()
    if not text:
        return ""
    canonical = _canonical_locator(text)
    if _split_annex_locator(text) is not None:
        return canonical
    if str(unit_id or "").startswith("point_"):
        return f"п. {canonical}"
    if _split_locator(text) is None:
        # Нерозпізнана форма («recital 5») — без вигаданого «ст.».
        return canonical
    return f"ст. {canonical}"


def _find_annex(root: Any, number: str) -> tuple[str, str] | None:
    """Текст додатка за id ``anx_<римське>``.

    Клас елемента не перевіряється навмисне: виміряно 2026-09-09, що в базовому
    акті це ``div.eli-container``, а в його консолідації — ``div`` без класу
    зовсім. Прив'язка до класу
    зробила б додаток читаним у базовому акті й не читаним у редакції на дату,
    тобто саме там, де він потрібен.
    """
    annex_id = f"anx_{number}"
    matches = root.xpath(f'.//*[@id="{annex_id}"]')
    if not matches:
        # Історична консолідація (розмітка ``clg``) id не має: додаток —
        # ``p.title-annex-*`` і блоки до наступного заголовка.
        return _find_legacy_annex(root, number)
    return _clean_text(matches[0]), annex_id


def _split_locator(path: str) -> tuple[str, str | None, str | None] | None:
    """Розібрати локатор на (токен статті, частина, підпункт).

    ``None`` — локатор не за очікуваною формою; викликач перетворює це на
    ``not_found``, а не намагається вгадати. «5a» і «5(1)(a)» дають різні
    токени статті/частини — перше піде в ``art_5a``, друге — в ``art_5`` з
    часткою «1» і підпунктом «a» (RFC карти 002, T134).
    """
    cleaned = str(path or "").strip().translate(_CONFUSABLE_LOCATOR_CHARS)
    if not cleaned:
        return None
    match = _LOCATOR_RE.match(cleaned)
    if not match:
        return None
    article_raw = match.group("article")
    piece = re.match(r"^(\d+)([A-Za-z]?)$", article_raw)
    if not piece:
        return None
    number, suffix = piece.groups()
    article_token = f"{number}{suffix.lower()}"
    return article_token, match.group("para"), match.group("sub")


def _canonical_locator(path: str) -> str:
    """Канонічний вигляд локатора: ``5А`` і кирилична ``5а`` → ``5a``.

    Локатор доказу мусить бути однаковий незалежно від того, якою розкладкою
    його набрали: інакше та сама стаття, прочитана двічі, лягла б у пам'ять
    доказів під двома різними адресами, і звірка цитати з кириличною «а» не
    знайшла б власного ж прочитаного тексту.
    """
    annex = _split_annex_locator(path)
    if annex is not None:
        # Одна адреса на додаток: «Annex II», «ANNEX II» і «додаток 2» мають
        # лягти в пам'ять доказів однією записом, інакше звірка цитати не
        # знайшла б власного ж прочитаного тексту.
        return f"Annex {annex}"
    parts = _split_locator(path)
    if parts is None:
        return str(path or "").strip()
    article_token, para, sub = parts
    canonical = article_token
    if para is not None:
        canonical += f"({para})"
    if sub is not None:
        canonical += f"({sub.lower()})"
    return canonical


def _parse_xhtml(html_text: str) -> Any:
    """Розібрати XHTML Cellar у DOM; ``None``, якщо документ не парситься."""
    raw = str(html_text or "").strip()
    if not raw:
        return None
    # Байти кодуються тут же в UTF-8; без явного кодування парсер HTML читав
    # документ без XML-декларації як Latin-1 і псував «▼» і кирилицю.
    parser = etree.HTMLParser(recover=True, encoding="utf-8")
    try:
        return etree.fromstring(raw.encode("utf-8"), parser=parser)
    except (etree.XMLSyntaxError, ValueError):
        return None


def _clean_text(element: Any) -> str:
    return re.sub(r"\s+", " ", "".join(element.itertext())).strip()


#: Классы заголовка акта в разметке Cellar — по одному на семейство подачи,
#: перебраны живой пробой 2026-09-09:
#: ``oj-doc-ti`` — базовый акт OJ (``32016R0679``: «REGULATION (EU) 2016/679 OF
#: THE EUROPEAN PARLIAMENT AND OF THE COUNCIL» + «of 27 April 2016»);
#: ``title-doc-first`` — консолидация (``02016R0679-20160504``); ``doc-ti`` —
#: протокол/договор (``12016E/PRO/03``:
#: «PROTOCOL (No 3)» + «ON THE STATUTE OF THE COURT OF JUSTICE…»);
#: ``oj-ti-tbl`` — акт, начинающийся оглавлением (``32012Q0929(01)``: «RULES OF
#: PROCEDURE OF THE COURT OF JUSTICE», а следующей строкой уже «Table of
#: Contents», то есть заголовок оглавления, а не часть названия).
#: Значение — сколько подряд идущих абзацев этого класса образуют название.
_ACT_TITLE_CLASSES: dict[str, int] = {
    "oj-doc-ti": 2,
    "title-doc-first": 2,
    "doc-ti": 2,
    "oj-ti-tbl": 1,
}

#: Предел длины названия акта: оно попадает в юридическую ссылку, а не в карточку.
_ACT_TITLE_MAX_CHARS = 300


def _act_title(root: Any) -> str:
    """Название акта из самого документа — без второго запроса к источнику.

    Берётся первая группа заголовочных абзацев одного класса; сколько их входит
    в название, объявлено в :data:`_ACT_TITLE_CLASSES` по измеренной разметке.
    Ничего не додумывается: не нашли заголовка — вернули пусто, и ссылка
    обойдётся без названия акта, а не с выдуманным.
    """
    if root is None:
        return ""
    collected: list[str] = []
    wanted = ""
    # Перебираются только сами заголовочные абзацы, а не всё поддерево: в
    # разметке Cellar первая строка названия часто несёт inline-потомка
    # (``<span class="oj-bold">``), и обход ``root.iter()`` обрывался на нём —
    # название теряло вторую строку с датой, ради которой в
    # ``_ACT_TITLE_CLASSES`` и стоит двойка (аудит рецензента 2026-09-09).
    for element in root.iter():
        classes = str(element.get("class") or "").split()
        matched = next((name for name in classes if name in _ACT_TITLE_CLASSES), "")
        if not matched:
            continue
        if wanted and matched != wanted:
            break
        text = _clean_text(element)
        if not text:
            continue
        wanted = matched
        collected.append(text)
        if len(collected) >= _ACT_TITLE_CLASSES[matched]:
            break
    title = " ".join(collected).strip()
    return title[:_ACT_TITLE_MAX_CHARS].strip()


def _find_by_article_id(root: Any, article_id: str) -> Any | None:
    matches = root.xpath(f'.//div[contains(@class,"eli-subdivision") and @id="{article_id}"]')
    return matches[0] if matches else None


def _find_paragraph(article_el: Any, article_token: str, para: str) -> tuple[Any | None, str]:
    """Частина статті — два різні варіанти розмітки (research/13 §A1, §A3).

    Базовий акт OJ: ``div#NNN.MMM`` (номер статті й частини, по 3 цифри).
    Консолідація: ``span.no-parag`` («1.  ») усередині ``div.norm``, без id.
    """
    digits_match = re.match(r"^(\d+)", article_token)
    if digits_match:
        numbered_id = f"{int(digits_match.group(1)):03d}.{int(para):03d}"
        matches = article_el.xpath(f'.//div[@id="{numbered_id}"]')
        if matches:
            return matches[0], numbered_id

    for norm_div in article_el.xpath(
        './/div[contains(concat(" ", normalize-space(@class), " "), " norm ") '
        'and not(contains(@class,"inline-element"))]'
    ):
        markers = norm_div.xpath('./span[contains(@class,"no-parag")]')
        if not markers:
            continue
        marker_text = _clean_text(markers[0]).rstrip(".").strip()
        if marker_text == str(int(para)):
            article_id = article_el.get("id") or article_token
            return norm_div, f"{article_id}.{para}"
    return None, f"{article_el.get('id') or article_token}.{para}"


def _find_subpoint(paragraph_el: Any, sub: str) -> str | None:
    """Підпункт «(a)» — таблиця (OJ) або grid-list (консолідація)."""
    sub_norm = sub.lower()

    for row in paragraph_el.xpath(".//table//tr"):
        cells = row.xpath("./td")
        if len(cells) < 2:
            continue
        marker = _clean_text(cells[0]).strip("()").strip().lower()
        if marker == sub_norm:
            return _clean_text(cells[1])

    for marker_div in paragraph_el.xpath('.//div[contains(@class,"grid-list-column-1")]'):
        marker = _clean_text(marker_div).strip("()").strip().lower()
        if marker != sub_norm:
            continue
        container = marker_div.getparent()
        if container is None:
            continue
        content = container.xpath('./div[contains(@class,"grid-list-column-2")]')
        if content:
            return _clean_text(content[0])
    return None


def _paragraph_content_text(paragraph_el: Any) -> str:
    """Текст частини без маркера номера (``span.no-parag``), якщо він є."""
    inline = paragraph_el.xpath('./div[contains(@class,"inline-element")]')
    if inline:
        return _clean_text(inline[0])
    return _clean_text(paragraph_el)


def _extract_from_id_markup(
    root: Any, article_token: str, para: str | None, sub: str | None
) -> tuple[str, str] | None:
    article_id = f"art_{article_token}"
    article_el = _find_by_article_id(root, article_id)
    if article_el is None:
        return None
    if para is None:
        return _clean_text(article_el), article_id

    paragraph_el, paragraph_id = _find_paragraph(article_el, article_token, para)
    if paragraph_el is None:
        return None
    if sub is None:
        return _paragraph_content_text(paragraph_el), paragraph_id

    sub_text = _find_subpoint(paragraph_el, sub)
    if sub_text is None:
        return None
    return sub_text, f"{paragraph_id}({sub})"


def _extract_from_statute_markup(root: Any, article_token: str) -> tuple[str, str] | None:
    """Статут (Протокол 3): статті без ``art_`` id — ``p.ti-art`` + ``p.normal``.

    Заголовок статті — точний збіг «Article N» (або «Article N» з подальшим
    пробілом перед приміткою), щоб не зачепити «Article 23a» при пошуку
    «Article 23» (research/13, §A5).
    """
    target = f"Article {article_token}"
    headings = root.xpath('.//p[contains(concat(" ", normalize-space(@class), " "), " ti-art ")]')
    for heading in headings:
        text = _clean_text(heading)
        if text != target and not text.startswith(target + " "):
            continue
        parts: list[str] = []
        node = heading.getnext()
        while node is not None:
            node_class = node.get("class") or ""
            if "ti-art" in node_class:
                break
            piece = _clean_text(node)
            if piece:
                parts.append(piece)
            node = node.getnext()
        return " ".join(parts), (heading.get("id") or target)
    return None


def _find_practice_point(root: Any, number: str) -> tuple[str, str] | None:
    """Пункт практичних вказівок: клітинка «N.» і сусідня клітинка тексту.

    Пункти не мають id (research/13, §A5) — локатор шукається за точним
    текстом маркера в таблиці, а не за оглавленням чи вільним пошуком тексту.
    """
    target = f"{number}."
    for cell_text in root.xpath('.//td//p | .//p[contains(@class,"oj-normal")]'):
        if _clean_text(cell_text) != target:
            continue
        row = cell_text
        while row is not None and row.tag != "tr":
            row = row.getparent()
        if row is None:
            continue
        cells = row.findall("td")
        if len(cells) < 2:
            continue
        content = _clean_text(cells[-1])
        if content:
            return content, f"point_{number}"
    return None


#: Заголовок статті в історичній консолідації (розмітка ``clg``, 2014–2022).
_LEGACY_ARTICLE_CLASS = "title-article-norm"

#: Класи блоків, що закривають статтю чи додаток: наступна стаття, додаток,
#: розділ. ``stitle-article-norm`` (назва статті) сюди не входить — це текст статті.
_LEGACY_BOUNDARY_PREFIXES: tuple[str, ...] = ("title-article", "title-annex", "title-division")

_LEGACY_ARTICLE_NUMBER_RE = re.compile(r"\d+[A-Za-z]?(?![A-Za-z\d])")
_LEGACY_ANNEX_NUMBER_RE = re.compile(r"([IVXLC]+|\d{1,2})\s*$")
_LEGACY_PARAGRAPH_MARKER_RE = re.compile(r"^(\d+)\.\s+")
_LEGACY_SUBPOINT_MARKER_RE = re.compile(r"^\(([A-Za-z])\)\s*")

#: Заголовок статті звичайним текстом — на випадок розмітки, якої розбір ще не знає.
_ARTICLE_HEADING_TEXT_RE = re.compile(r"^Article\s+\d+[A-Za-z]?$", re.IGNORECASE)


def _class_tokens(element: Any) -> list[str]:
    return str(element.get("class") or "").split()


def _has_legacy_article_headings(root: Any) -> bool:
    return bool(
        root.xpath(
            f'.//p[contains(concat(" ", normalize-space(@class), " "), " {_LEGACY_ARTICLE_CLASS} ")]'
        )
    )


def _looks_divided_into_articles(root: Any) -> bool:
    """Чи документ розбито на статті — за класом заголовка або за його текстом.

    Практичні вказівки статей не мають: їхні пункти — клітинки «N.» таблиці.
    Документ зі статтями, який жодна знайома розмітка не розібрала, не можна
    читати як практичні вказівки: клітинка «5.» першої-ліпшої таблиці (Додаток I
    з датою внесення до переліку) стала б «пунктом 5» і «статтею 5».
    """
    for heading in root.iter("p"):
        classes = _class_tokens(heading)
        if any(name in ("oj-ti-art", "ti-art", _LEGACY_ARTICLE_CLASS) for name in classes):
            return True
        if _ARTICLE_HEADING_TEXT_RE.match(_clean_text(heading)):
            return True
    return False


def _legacy_heading_number(heading: Any) -> str | None:
    """Номер статті із заголовка — мовою документа («Article 5», «5. pants», «Стаття 5»)."""
    match = _LEGACY_ARTICLE_NUMBER_RE.search(_clean_text(heading))
    return match.group(0).lower() if match else None


def _is_legacy_boundary(element: Any) -> bool:
    if element.tag == "hr":
        return any(name.startswith("separator") for name in _class_tokens(element))
    return element.tag == "p" and any(
        name.startswith(_LEGACY_BOUNDARY_PREFIXES) for name in _class_tokens(element)
    )


def _block_text(block: Any) -> str:
    """Текст блоку; рядок таблиці — клітинки через пробіл, а не злиті докупи."""
    if block.tag == "tr":
        cells = (_clean_text(cell) for cell in block.xpath("./td|./th"))
        return " ".join(cell for cell in cells if cell)
    return _clean_text(block)


def _without_modification_markers(element: Any) -> Any:
    """Копія блоку без маркерів змін ``p.modref`` («▼M3», «▼B») будь-де всередині.

    Маркер — позначка консолідованого тексту, а не текст статті; у пізніших
    консолідаціях він стоїть і між статтями, і всередині частини (перед
    підпунктом, доданим поправкою). Хвіст маркера (текст після нього) зберігається.
    """
    clone = copy.deepcopy(element)
    for marker in clone.xpath(
        './/p[contains(concat(" ", normalize-space(@class), " "), " modref ")]'
    ):
        parent = marker.getparent()
        if parent is None:
            continue
        if marker.tail:
            previous = marker.getprevious()
            if previous is not None:
                previous.tail = (previous.tail or "") + marker.tail
            else:
                parent.text = (parent.text or "") + marker.tail
        parent.remove(marker)
    return clone


def _legacy_blocks_after(heading: Any) -> list[tuple[Any, str]]:
    """Блоки між заголовком і наступним заголовком (стаття чи додаток), без порожніх.

    Блок — копія без маркерів змін (:func:`_without_modification_markers`).
    Таблиця розкладається на рядки, бо перелік підпунктів у консолідаціях
    буває таблицею.
    """
    blocks: list[tuple[Any, str]] = []
    for sibling in heading.itersiblings():
        if not isinstance(sibling.tag, str):
            continue
        if _is_legacy_boundary(sibling):
            break
        if sibling.tag == "p" and "modref" in _class_tokens(sibling):
            continue
        clean = _without_modification_markers(sibling)
        candidates = clean.xpath(".//tr") if clean.tag == "table" else [clean]
        for candidate in candidates:
            text = _block_text(candidate)
            if text:
                blocks.append((candidate, text))
    return blocks


def _split_legacy_paragraphs(blocks: list[tuple[Any, str]]) -> dict[int, list[tuple[Any, str]]]:
    """Нумеровані частини статті: блок із «1. », «2. » підряд; решта — до попередньої частини.

    Блок частини — ``p.norm`` («1.  текст») у консолідаціях 2014–2020 і
    ``div.norm`` із ``span.no-parag`` у консолідаціях 2021–2022: маркер обидва
    рази стоїть на початку тексту блоку. Номер має бути наступним по порядку:
    абзац, що починається з «2014. …», частиною 2014 не стає.
    """
    paragraphs: dict[int, list[tuple[Any, str]]] = {}
    current = 0
    for element, text in blocks:
        marker = _LEGACY_PARAGRAPH_MARKER_RE.match(text)
        if marker and int(marker.group(1)) == current + 1:
            current += 1
            paragraphs[current] = []
        if current:
            paragraphs[current].append((element, text))
    return paragraphs


def _legacy_subpoint(blocks: list[tuple[Any, str]], sub: str) -> str | None:
    """Підпункт «(a)».

    Спершу — блок верхнього рівня, що починається з цього маркера (консолідації
    2014–2020: ``div > p.norm``); далі — всередині блоків, як у решти розміток
    (``grid-list`` консолідацій 2020–2022, таблиця).
    """
    wanted = sub.lower()
    for _block, text in blocks:
        marker = _LEGACY_SUBPOINT_MARKER_RE.match(text)
        if marker and marker.group(1).lower() == wanted:
            return text[marker.end() :].strip()
    for element, _text in blocks:
        found = _find_subpoint(element, sub)
        if found:
            return found
    return None


def _extract_from_legacy_consolidation_markup(
    root: Any, article_token: str, para: str | None, sub: str | None
) -> tuple[str, str] | None:
    """Історична консолідація без ``art_``-id: стаття — ``p.title-article-norm`` і сусіди.

    Так розмічено консолідації 2014–2022 років (конвертер ``clg``; проба
    29.09.2026 на трьох регламентах): жодного ``id`` статті немає, тож
    одиниця збирається з блоків між двома заголовками. Відсутня стаття чи частина —
    ``None`` (чесне ``not_found``), а не інша стратегія: див. :func:`extract_xhtml_unit`.
    """
    heading = next(
        (
            element
            for element in root.xpath(
                f'.//p[contains(concat(" ", normalize-space(@class), " "), '
                f'" {_LEGACY_ARTICLE_CLASS} ")]'
            )
            if _legacy_heading_number(element) == article_token
        ),
        None,
    )
    if heading is None:
        return None
    article_id = f"art_{article_token}"
    blocks = _legacy_blocks_after(heading)

    if para is None:
        if sub is None:
            return " ".join([_clean_text(heading), *(text for _e, text in blocks)]), article_id
        # «1(a)»: підпункт статті без нумерованих частин.
        text = _legacy_subpoint(blocks, sub)
        return (text, f"{article_id}({sub.lower()})") if text else None

    paragraph = _split_legacy_paragraphs(blocks).get(int(para))
    if not paragraph:
        return None
    paragraph_id = f"{article_id}.{int(para)}"
    if sub is None:
        joined = " ".join(text for _e, text in paragraph)
        return _LEGACY_PARAGRAPH_MARKER_RE.sub("", joined, count=1), paragraph_id
    text = _legacy_subpoint(paragraph, sub)
    return (text, f"{paragraph_id}({sub.lower()})") if text else None


def _find_legacy_annex(root: Any, number: str) -> tuple[str, str] | None:
    """Додаток історичної консолідації: ``p.title-annex-*`` і блоки до наступного додатка."""
    for heading in root.iter("p"):
        if not any(name.startswith("title-annex") for name in _class_tokens(heading)):
            continue
        match = _LEGACY_ANNEX_NUMBER_RE.search(_clean_text(heading))
        if not match or match.group(1).upper() != number:
            continue
        blocks = _legacy_blocks_after(heading)
        return " ".join([_clean_text(heading), *(text for _e, text in blocks)]), f"anx_{number}"
    return None


def extract_xhtml_unit(root: Any, path: str) -> tuple[str, str] | None:
    """Текст і ідентифікатор одиниці, знайденої в XHTML Cellar за локатором.

    Диспетчер за формою розмітки документа (research/13):

    * документ має ``id`` виду ``art_*`` — базовий акт OJ, консолідація,
      Регламент Суду чи рішення e-Curia: шукаємо саме за цим id, і
      відсутність запитаної статті НЕ веде до інших стратегій (інакше
      правдоподібний, але хибний збіг підмінив би чесний ``not_found``);
    * інакше є ``p.ti-art`` — Статут: пошук за текстом заголовка;
    * інакше є ``p.title-article-norm`` — історична консолідація 2014–2022
      років (розмітка ``clg``): стаття збирається з блоків між заголовками;
    * інакше, коли документ узагалі розбито на статті, — ``None``: незнайома
      розмітка статтей не має права перетворюватися на «пункт» із клітинки
      таблиці Додатка (аудит 22.1, № 7);
    * інакше — практичні вказівки: пошук за клітинкою «N.» у таблиці.
    """
    annex = _split_annex_locator(path)
    if annex is not None:
        # Додаток перевіряється першим: «II» розбирається обома розбірниками,
        # а стаття з римським номером в актах ЄС не зустрічається.
        return _find_annex(root, annex)

    parsed = _split_locator(path)
    if parsed is None:
        return None
    article_token, para, sub = parsed

    if root.xpath('//*[starts-with(@id,"art_")]'):
        return _extract_from_id_markup(root, article_token, para, sub)
    if root.xpath('.//p[contains(concat(" ", normalize-space(@class), " "), " ti-art ")]'):
        return _extract_from_statute_markup(root, article_token)
    if _has_legacy_article_headings(root):
        return _extract_from_legacy_consolidation_markup(root, article_token, para, sub)
    if _looks_divided_into_articles(root):
        return None
    return _find_practice_point(root, article_token)


#: Датована консолідація: сектор 0, базовий акт і дата редакції в одному рядку.
_CONSOLIDATED_CELEX_RE = re.compile(r"^0(?P<base>\d{4}[A-Z]\d+)-(?P<date>\d{8})$")


def _document_aliases(requested: str, resolved: str) -> tuple[str, ...]:
    """Форми, якими той самий документ цитують, окрім ``resolved``.

    Для консолідації це базовий акт (``02016R0679-20160504`` → ``32016R0679``):
    розпізнавач цитат віддає саме його плюс окремий ``version_id``, тому без
    псевдоніма доказ не знайшовся б за власною ж цитатою.
    """
    aliases: list[str] = []
    for candidate in (requested, resolved):
        match = _CONSOLIDATED_CELEX_RE.match(str(candidate or "").strip())
        if match:
            aliases.append(f"3{match.group('base')}")
    if requested and requested != resolved:
        aliases.append(requested)
    return tuple(dict.fromkeys(alias for alias in aliases if alias and alias != resolved))


def _publication_kind_for_celex(celex: str) -> PublicationKind:
    """OJ (сектор 3 і 1 — договори/протоколи) чи консолідація (сектор 0)."""
    sector = str(celex or "").strip()[:1]
    if sector == "0":
        return PublicationKind.CONSOLIDATED
    if sector in ("1", "3"):
        return PublicationKind.OFFICIAL_JOURNAL
    return PublicationKind.UNKNOWN


#: Акти без консолідації в Cellar (Статут — Протокол 3, Регламент Суду):
#: доведено живими пробами (research/13, §A5) — 0 рядків за
#: ``act_consolidated_consolidates_resource_legal``/``based_on`` і за
#: префіксом CELEX. Читання без ``as_of`` віддає текст саме цього CELEX, і
#: конверт зобов'язаний назвати, чим його відтоді змінено, а не мовчати про
#: те, що чинний стан статті цим не доведено.
_NO_CONSOLIDATION_AMENDING_ACTS: dict[str, tuple[str, ...]] = {
    "12016E/PRO/03": ("32016R1192", "32019R0629", "32024R2019"),
    "32012Q0929(01)": (
        "32016Q0812(01)",
        "32019Q0425(01)",
        "32019Q1206(01)",
        "32024Q02094",
        "32026Q01335",
    ),
}


def _revision_notice_for(celex: str) -> str | None:
    amending = _NO_CONSOLIDATION_AMENDING_ACTS.get(celex)
    if not amending:
        return None
    return (
        f"Консолідованої версії {celex} у Cellar немає: "
        f"це редакція {celex} як є. Відомі змінюючі акти: {', '.join(amending)}. "
        "Чинний стан статті цим не доведено — редакцію на дату не запитано і "
        "не підставлено."
    )


def _sparql_search_query(query: str, *, limit: int) -> str:
    escaped = str(query or "").replace('"', '\\"')
    return (
        "PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>\n"
        "SELECT ?work ?celex ?title WHERE {\n"
        "  ?work cdm:resource_legal_id_celex ?celex .\n"
        "  ?work cdm:work_title ?title .\n"
        f'  FILTER(CONTAINS(LCASE(STR(?title)), LCASE("{escaped}")))\n'
        "}\n"
        f"LIMIT {int(limit)}"
    )


def _binding_value(binding: dict[str, Any], key: str) -> str:
    return str((binding.get(key) or {}).get("value") or "")


def _consolidations_query(base_celex: str) -> str:
    """Датовані консолідації базового акта — від самого акта, а не префіксом CELEX.

    Фільтр ``STRSTARTS`` за префіксом «0…-» переглядав усі CELEX Cellar: жива
    проба 22.09.2026 — 6–16 с на акт (п'ять регламентів, серед них 2016/679 і
    1907/2006) при тайм-ауті 30 с, тож повільний день Cellar означав «чинну
    консолідацію не визначено». Зв'язок ``act_consolidated_based_on_resource_legal``
    дав на тих самих актах ті самі переліки за 0,1 с. Зв'язок
    ``act_consolidated_consolidates_resource_legal`` не годиться: у пробі
    чотири консолідації одного регламенту за 2018–2019 роки вели ним не на
    базовий акт.
    """
    return (
        "PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>\n"
        "SELECT DISTINCT ?celex WHERE {\n"
        f"  ?basic cdm:resource_legal_id_celex {_sparql_string(base_celex)}{_XSD_STRING} .\n"
        "  ?w cdm:act_consolidated_based_on_resource_legal ?basic .\n"
        "  ?w cdm:resource_legal_id_celex ?celex .\n"
        "}"
    )


#: Дерево нотатки роботи з Cellar REST — метадані без SPARQL. REST і SPARQL у
#: Cellar — різні служби: 22.09.2026 SPARQL не відповідав, а текст за CELEX
#: читався. На п'яти актах живої проби того дня перелік консолідацій і склад
#: консолідації з нотатки збіглися зі SPARQL до рядка.
_NOTICE_TREE_ACCEPT = "application/xml;notice=tree"
#: Зв'язки в дереві нотатки: консолідації базового акта й склад консолідації.
_NOTICE_CONSOLIDATIONS = "RESOURCE_LEGAL_BASIS_FOR_ACT_CONSOLIDATED"
_NOTICE_COMPOSITION = "ACT_CONSOLIDATED_CONSOLIDATES_RESOURCE_LEGAL"


def _notice_celex_links(root: Any, relation: str) -> list[str] | None:
    """CELEX робіт, на які веде зв'язок ``relation`` у дереві нотатки; ``None`` — не нотатка.

    Лише прямі ланки ``NOTICE/WORK/<relation>``: вкладені нотатки інших робіт
    несуть власні зв'язки, і глибокий обхід зібрав би чужі CELEX. CELEX стоїть
    у ``SAMEAS/URI`` з ``TYPE=celex``; у живій нотатці одного регламенту 105
    ланок із 183 мали лише URI типу ``consolidation`` без CELEX, і вони
    пропускаються.
    Документ іншої форми — не порожній перелік, а ``None``: інакше сторінка
    помилки означала б «консолідацій немає», і стаття знову була б «відсутня».
    """
    if root.tag != "NOTICE" or root.find("WORK") is None:
        return None
    celexes: list[str] = []
    for link in root.iterfind(f"WORK/{relation}"):
        for uri in link.iterfind("SAMEAS/URI"):
            identifier = (uri.findtext("IDENTIFIER") or "").strip()
            if uri.findtext("TYPE") == "celex" and identifier:
                celexes.append(identifier)
    return celexes


def _current_unavailable_note(unavailable: SourceUnavailable) -> str:
    """Примітка до тексту, прочитаного без чинної консолідації."""
    return (
        f"метадані Cellar недоступні ({unavailable.reason}): чинну консолідацію "
        "не визначено, прочитано первинний текст OJ без пізніших змін"
    )


def _consolidated_date_from_celex(dated_celex: str) -> str | None:
    """ISO-дата з хвоста датованого CELEX (``…-20231219`` → ``2023-12-19``)."""
    match = re.search(r"-(\d{8})$", str(dated_celex or ""))
    if not match:
        return None
    raw = match.group(1)
    return f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]}"


def _day_before(date_str: str) -> str:
    parsed = dt.date.fromisoformat(date_str)
    return (parsed - dt.timedelta(days=1)).isoformat()


@dataclass(frozen=True)
class ResolvedId:
    """CELEX для читання, ELI із запиту (якщо був) і інші CELEX того самого ELI."""

    celex: str
    eli: str | None = None
    alternatives: tuple[str, ...] = ()


def _celex_url(celex: str) -> str:
    return f"{endpoint('eu.cellar_celex')}/{quote(celex, safe='')}"


class EuLawAdapter(CellarReader, RegistryAdapter):
    """Право ЄС: пошук через Cellar SPARQL, читання через Cellar XHTML."""

    source_id = "eu_law_eurlex_cellar"
    id_format_hint = (
        "CELEX, наприклад 32016R0679; консолідована редакція — 02016R0679-20160504; "
        "або ELI http://data.europa.eu/eli/reg/2016/679/oj"
    )
    legal_order = "EU"
    source_policy = SourcePolicy.API

    def __init__(
        self,
        session: requests.Session | None = None,
        *,
        today: Callable[[], dt.date] = dt.date.today,
    ) -> None:
        self._session = session or requests.Session()
        #: Джерело «сьогодні» для чинної консолідації. Окремим параметром, бо
        #: тест на конкретну дату інакше залежав би від дати прогону: фікстура
        #: з майбутньою консолідацією одного дня стає минулою і тихо змінює
        #: відповідь.
        self._today = today
        #: Усі звернення до Cellar ідуть через транспорт: пулове з'єднання, що
        #: пережило простій, інакше давало хибний ``source_unavailable``
        #: (живий виклик 15.09.2026, :mod:`sources.transport`).
        self._transport = SourceTransport(self._session)
        #: Перелік датованих консолідацій за базовим CELEX — на час процесу.
        self._consolidation_cache: dict[str, list[tuple[str, str]]] = {}
        #: Склад датованої консолідації: така консолідація не змінюється, а
        #: без кешу під час збою SPARQL кожне читання того самого акта знову
        #: чекало б його відмови.
        self._composition_cache: dict[str, tuple[str, ...]] = {}
        #: ELI → CELEX на час процесу: відповідність роботи не змінюється.
        self._eli_cache: dict[str, ResolvedId] = {}

    # -- транспорт -------------------------------------------------------

    def resolve_document_id(self, document_id: str) -> ResolvedId | TypedFailure:
        """Єдиний вхід ідентифікатора EU: перевірка форми і ELI → CELEX.

        Cellar REST ELI за адресою ``/resource/celex/`` не приймає і відповідає
        500 — користувач бачив «джерело недоступне» там, де була лише інша
        форма ідентифікатора (research тікета 19, §2.2). Тому ELI спершу
        розв'язується одним SPARQL-запитом ``cdm:resource_legal_eli``; ELI, якого
        Cellar не знає, — ``not_found``. Рядок, що не є ні CELEX, ні ELI, —
        ``invalid_input`` і до джерела не йде: він вставлявся б у SPARQL.
        """
        raw = str(document_id or "").strip()
        if not raw:
            return NotFound(identifier="(порожній ідентифікатор)", id_format_hint=CELEX_FORMAT_HINT)
        eli = normalize_eli(raw)
        if eli is None:
            if not _CELEX_ID_RE.match(raw):
                return InvalidIdentifier(identifier=raw, id_format_hint=CELEX_FORMAT_HINT)
            return ResolvedId(celex=raw)
        if not eli:
            return InvalidIdentifier(identifier=raw, id_format_hint=CELEX_FORMAT_HINT)
        cached = self._eli_cache.get(eli)
        if cached is not None:
            return cached
        bindings = self._sparql_bindings(_eli_to_celex_query(eli))
        if isinstance(bindings, SourceUnavailable):
            return bindings
        celexes = sorted(
            {_binding_value(row, "celex") for row in bindings} - {""}, key=_celex_preference
        )
        if not celexes:
            consolidation = re.search(r"/\d{4}-\d{2}-\d{2}$", eli) is not None
            return NotFound(
                identifier=raw,
                id_format_hint=(
                    f"ELI {eli} у Cellar не знайдено; "
                    + (
                        "консолідації на цю дату немає — перелік дат консолідацій дає "
                        "ELI акта з «/oj» або CELEX базового акта"
                        if consolidation
                        else "перевірте тип, рік і номер або вкажіть CELEX, наприклад 32024Q02173"
                    )
                ),
            )
        resolved = ResolvedId(celex=celexes[0], eli=eli, alternatives=tuple(celexes[1:]))
        self._eli_cache[eli] = resolved
        return resolved

    # -- контракт RegistryAdapter --------------------------------------

    def policy(self) -> Source:
        return Source(
            id=self.source_id,
            legal_order=self.legal_order,
            layer=CoverageLayer.CONNECTED,
            access=SourceAccess.OFFICIAL_API,
            license="Commission Decision 2011/833/EU",
            license_forbids_commercial=False,
            attribution_required=True,
            attribution=ATTRIBUTION_EU.format(year="—"),
            limits=SourceLimits(
                max_records_per_request=CELLAR_RECORD_LIMIT,
                notes=(
                    "з 01.01.2026 — ліміт 10000 записів на один пошуковий запит; "
                    "обхід постранично, інакше partial_result"
                ),
            ),
            source_url=endpoint("eu.eurlex"),
            description=(
                "Право ЄС через Cellar: SPARQL для пошуку, REST зі згодженням "
                "змісту (XHTML) для тексту до статті/частини/підпункту; CELEX і ELI."
            ),
        )

    # -- SPARQL і нотатка REST: перелік консолідацій і їхній склад -------

    def _notice_links(self, celex: str, relation: str) -> list[str] | TypedFailure:
        """CELEX за зв'язком ``relation`` роботи ``celex`` з дерева нотатки Cellar REST."""
        response = self._get(_celex_url(celex), headers={"Accept": _NOTICE_TREE_ACCEPT}, timeout=30)
        if isinstance(response, SourceUnavailable):
            return response
        if response.status_code == 404:
            return NotFound(identifier=celex, id_format_hint=CELEX_FORMAT_HINT)
        if response.status_code >= 400:
            return self._unavailable(f"http_{response.status_code}")
        parser = etree.XMLParser(resolve_entities=False, no_network=True)
        try:
            root = etree.fromstring(response.content, parser=parser)
        except (etree.XMLSyntaxError, ValueError):
            return self._unavailable("unparsable_notice")
        links = _notice_celex_links(root, relation)
        if links is None:
            return self._unavailable("unexpected_notice")
        return links

    def _celexes_via_sparql_or_notice(
        self, sparql: str, celex: str, relation: str
    ) -> list[str] | TypedFailure:
        """CELEX зі SPARQL, а коли SPARQL мовчить — з нотатки REST.

        Відмова лише тоді, коли не відповіли обидва; причина називає обидва,
        щоб за нею було видно, котра служба лежала.
        """
        bindings = self._sparql_bindings(sparql)
        if not isinstance(bindings, SourceUnavailable):
            return [_binding_value(binding, "celex") for binding in bindings]
        links = self._notice_links(celex, relation)
        if isinstance(links, SourceUnavailable):
            return self._unavailable(f"sparql:{bindings.reason}; rest_notice:{links.reason}")
        return links

    def _list_consolidations(self, base_celex: str) -> list[tuple[str, str]] | TypedFailure:
        """[(ISO-дата, датований CELEX), …] за зростанням дати, з кешем."""
        cached = self._consolidation_cache.get(base_celex)
        if cached is not None:
            return cached

        listed = self._celexes_via_sparql_or_notice(
            _consolidations_query(base_celex), base_celex, _NOTICE_CONSOLIDATIONS
        )
        if isinstance(listed, TypedFailure):
            return listed

        prefix = f"0{base_celex[1:]}-"
        parsed: list[tuple[str, str]] = []
        for dated_celex in sorted(set(listed)):
            iso_date = _consolidated_date_from_celex(dated_celex)
            if iso_date is None or not dated_celex.startswith(prefix):
                continue
            parsed.append((iso_date, dated_celex))
        parsed.sort(key=lambda item: item[0])
        self._consolidation_cache[base_celex] = parsed
        return parsed

    def _composition(self, dated_celex: str) -> tuple[str, ...] | TypedFailure:
        """CELEX робіт, з яких складена консолідація ``dated_celex``, з кешем."""
        cached = self._composition_cache.get(dated_celex)
        if cached is not None:
            return cached
        sparql = (
            "PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>\n"
            "SELECT ?celex WHERE {\n"
            f"  ?w cdm:resource_legal_id_celex {_sparql_string(dated_celex)}{_XSD_STRING} .\n"
            "  ?w cdm:act_consolidated_consolidates_resource_legal ?a .\n"
            "  ?a cdm:resource_legal_id_celex ?celex .\n"
            "}"
        )
        listed = self._celexes_via_sparql_or_notice(sparql, dated_celex, _NOTICE_COMPOSITION)
        if isinstance(listed, TypedFailure):
            return listed
        composition = tuple(listed)
        if composition:
            # Порожній склад не кешується: нову консолідацію Cellar міг ще не
            # проіндексувати, і «складу немає» не має пережити процес.
            self._composition_cache[dated_celex] = composition
        return composition

    # -- редакція на дату (T133, T135; ADR 0007) -------------------------

    def resolve_revision(self, document_id: str, as_of: str) -> Revision | TypedFailure:
        """Довести інтервал дії редакції ``document_id`` на дату ``as_of``.

        Чинну редакцію замість непідтвердженої не підставляємо: дата поза
        доведеними межами (раніше першої консолідації чи пізніше
        ``known_through``) — завжди ``revision_unknown`` із чесними межами.
        """
        celex = str(document_id or "").strip()
        as_of_date = str(as_of or "").strip()
        if not celex or not as_of_date:
            return RevisionUnknown(
                document_id=celex or "(порожній ідентифікатор)",
                requested_date=as_of_date or "(дата не вказана)",
                known_bounds=("ідентифікатор документа або дата не вказані",),
            )
        resolved = self.resolve_document_id(celex)
        if isinstance(resolved, TypedFailure):
            return resolved
        celex = resolved.celex

        consolidations = self._list_consolidations(celex)
        if isinstance(consolidations, TypedFailure):
            return consolidations
        if not consolidations:
            return RevisionUnknown(
                document_id=celex,
                requested_date=as_of_date,
                known_bounds=(f"консолідації для {celex} у Cellar не знайдено",),
            )

        first_date, first_celex = consolidations[0]
        last_date, last_celex = consolidations[-1]
        if as_of_date < first_date:
            return RevisionUnknown(
                document_id=celex,
                requested_date=as_of_date,
                known_bounds=(f"найраніша відома консолідація — {first_date} ({first_celex})",),
            )
        if as_of_date > last_date:
            return RevisionUnknown(
                document_id=celex,
                requested_date=as_of_date,
                known_bounds=(
                    f"найпізніша відома консолідація — {last_date} ({last_celex}); "
                    f"known_through={last_date}",
                    # Дата пізніше known_through не доведена: поправку могли
                    # опублікувати, а консолідувати ще ні. Текст останньої
                    # консолідації дає читання без as_of — з тією самою межею.
                    f"без as_of читається найпізніша відома консолідація {last_celex} "
                    "з позначкою known_through",
                ),
            )

        covering_index = 0
        for index, (date_str, _celex) in enumerate(consolidations):
            if date_str <= as_of_date:
                covering_index = index
            else:
                break
        return self._revision_at(celex, consolidations, covering_index)

    def current_revision(self, document_id: str) -> Revision | None | TypedFailure:
        """Чинна консолідація базового акта: найпізніша з датою не пізніше сьогодні.

        ``None`` — у Cellar консолідацій цього акта немає (або всі датовані
        майбутнім), і читати слід сам CELEX. На відміну від
        :meth:`resolve_revision` дата пізніше останньої консолідації тут не
        ``revision_unknown``: юрист без дати питає чинну норму, і найпізніша
        відома консолідація — найточніша відповідь джерела, а межу її
        відомості (``known_through``) відповідь несе явно. Живий виклик
        15.09.2026: без цього ``get_article`` на статтю, внесену поправкою,
        читав первинний текст OJ і відповідав ``not_found``.
        """
        resolved = self.resolve_document_id(document_id)
        if isinstance(resolved, TypedFailure):
            return resolved
        celex = resolved.celex
        match = _CELEX_RE.match(celex)
        if match is None or not celex.startswith("3") or celex in _NO_CONSOLIDATION_AMENDING_ACTS:
            return None
        consolidations = self._list_consolidations(celex)
        if isinstance(consolidations, TypedFailure):
            return consolidations
        today = self._today().isoformat()
        in_force = [
            index for index, (date_str, _) in enumerate(consolidations) if date_str <= today
        ]
        if not in_force:
            return None
        return self._revision_at(celex, consolidations, in_force[-1])

    def _revision_at(
        self, celex: str, consolidations: list[tuple[str, str]], covering_index: int
    ) -> Revision | TypedFailure:
        """Інтервал дії консолідації ``consolidations[covering_index]`` і її склад."""
        last_date = consolidations[-1][0]
        covering_date, covering_celex = consolidations[covering_index]
        has_next = covering_index + 1 < len(consolidations)
        valid_to = _day_before(consolidations[covering_index + 1][0]) if has_next else None
        known_through = None if has_next else last_date

        composition = self._composition(covering_celex)
        if isinstance(composition, TypedFailure):
            return composition
        consolidated_prefix = f"0{celex[1:]}-"
        amending_acts = tuple(
            item
            for item in composition
            if item and item != celex and not item.startswith(consolidated_prefix)
        )

        interval_basis_urls = [_celex_url(covering_celex)]
        if has_next:
            interval_basis_urls.append(_celex_url(consolidations[covering_index + 1][1]))

        return Revision(
            document_id=celex,
            valid_from=covering_date,
            valid_to=valid_to,
            consolidation_id=covering_celex,
            known_through=known_through,
            interval_basis_urls=tuple(interval_basis_urls),
            amending_acts=amending_acts,
            publication_kind="consolidated",
        )

    # -- читання документа (T132, T134, T150) ----------------------------

    def _select_revision(
        self, celex: str, as_of_value: str
    ) -> tuple[Revision | None, str, SourceUnavailable | None] | TypedFailure:
        """(редакція, CELEX для читання, відмова, через яку чинну редакцію не визначено).

        З ``as_of`` — редакція на дату; без неї — чинна консолідація, а для
        акта без консолідацій — сам CELEX.
        """
        if as_of_value:
            revision_result = self.resolve_revision(celex, as_of_value)
            if isinstance(revision_result, TypedFailure):
                return revision_result
            return revision_result, revision_result.consolidation_id or celex, None
        current = self.current_revision(celex)
        if isinstance(current, SourceUnavailable):
            # Без дати консолідація — уточнення, а не передумова (рев'ю
            # 15.09.2026): збій метаданих Cellar не валить читання, текст
            # CELEX читається, а відповідь прямо каже, що чинну редакцію не
            # визначено.
            return None, celex, current
        if isinstance(current, TypedFailure):
            return current
        if current is not None:
            return current, current.consolidation_id or celex, None
        return None, celex, None

    def _absent_while_consolidation_unknown(
        self, target_celex: str, locator: str, unavailable: SourceUnavailable
    ) -> SourceUnavailable:
        """Одиниці немає в первинному тексті, а чинну консолідацію не визначено.

        ``not_found`` тут був би хибним твердженням: статтю чи додаток могли
        внести пізнішим актом, і перевіряльник, побачивши ``not_found`` на
        внесеній статті, вирішив би, що її немає (перевірка 22.09.2026).
        Джерело відповідає «не можу сказати», а не «ні».
        """
        return SourceUnavailable(
            source=self.source_id,
            retry_after=self.retry_after,
            reason=f"current_consolidation_unknown:{unavailable.reason}",
            manual_path=(
                f"«{locator}» немає в первинному тексті {target_celex}, але чинну "
                "консолідацію не визначено — одиницю могли внести пізнішим актом, "
                "тож це не доказ, що її немає. Повторіть пізніше; або прочитайте "
                f"датований CELEX консолідації (0{target_celex[1:]}-РРРРММДД) чи "
                "акт, що вніс зміну; вручну — перелік консолідацій на "
                f"{endpoint('eu.eurlex')}/legal-content/EN/ALL/?uri=CELEX:{target_celex}"
            ),
        )

    def _get_xhtml(self, target_celex: str, lang3: str) -> tuple[Any, str, str] | TypedFailure:
        """(корінь DOM, сире тіло, адреса) документа Cellar або типізована відмова."""
        url = _celex_url(target_celex)
        response = self._get(
            url,
            headers={
                "Accept": "application/xhtml+xml, application/xml;q=0.8",
                "Accept-Language": lang3,
            },
            timeout=30,
        )
        if isinstance(response, SourceUnavailable):
            return response

        if response.status_code == 404:
            return NotFound(identifier=target_celex, id_format_hint="CELEX, наприклад 32016R0679")
        if response.status_code >= 400:
            return self._unavailable(f"http_{response.status_code}")

        body = response.text
        root = _parse_xhtml(body)
        if root is None:
            stub = self.reject_stub(body, document_id=target_celex)
            if stub is not None:
                return stub
            return self._unavailable("unparsable_document")
        return root, body, url

    @typed_failures(source_id="eu_law_eurlex_cellar")
    def fetch(
        self,
        document_id: str,
        *,
        path: str = "",
        language: str = "",
        as_of: str = "",
        **options: Any,
    ) -> AdapterResult:
        resolved = self.resolve_document_id(document_id)
        if isinstance(resolved, TypedFailure):
            return resolved
        celex, requested_eli = resolved.celex, resolved.eli

        locator = str(path or options.get("path") or options.get("article") or "").strip()
        requested_language = str(language or options.get("language") or "").strip()
        lang_pair = _resolve_language(requested_language)
        if lang_pair is None:
            return NotCovered(
                legal_order=self.legal_order,
                manual_path=f"{endpoint('eu.eurlex')}/legal-content/EN/ALL/?uri=CELEX:{celex}",
                subject=f"{celex} мовою «{requested_language or 'uk'}»",
                operation="read_document",
                source_id=self.source_id,
            )
        lang2, lang3 = lang_pair

        as_of_value = str(as_of or options.get("as_of") or "").strip()
        selected = self._select_revision(celex, as_of_value)
        if isinstance(selected, TypedFailure):
            return selected
        revision, target_celex, current_unavailable = selected

        document = self._get_xhtml(target_celex, lang3)
        if isinstance(document, TypedFailure):
            return document
        root, body, url = document

        if locator:
            found = extract_xhtml_unit(root, locator)
            if found is None:
                # Знайдена структурна одиниця довела б, що це справжній
                # документ, а не підміна; коли її немає — перевіряємо, чи не
                # підмінили ВЕСЬ документ, перш ніж списувати це на локатор.
                # Друкована редакція деяких актів (наприклад Регламенту Суду)
                # має справжній розділ «Table of Contents» на початку —
                # маркер-евристика на ньому спрацьовує хибно, тож перевірка
                # заглушки на сирому тілі відповіді йде лише тут, а не завжди
                # (research/13, §A5).
                stub = self.reject_stub(body, document_id=target_celex)
                if stub is not None:
                    return stub
                if current_unavailable is not None:
                    return self._absent_while_consolidation_unknown(
                        target_celex, locator, current_unavailable
                    )
                hint = (
                    f"локатор «{locator}» відсутній у редакції {target_celex} "
                    + (
                        f"(чинній на {as_of_value})"
                        if as_of_value
                        else "(найпізніша відома консолідація; первинний текст OJ не читався)"
                    )
                    if revision is not None
                    else (
                        "номер статті/частини/підпункту в межах цього акта "
                        "(«5», «5a», «5(1)(a)») або додаток («Annex II»)"
                    )
                )
                return NotFound(identifier=f"{target_celex}#{locator}", id_format_hint=hint)
            unit_text, unit_id = found
            content_kind = ContentKind.FRAGMENT
        else:
            stub = self.reject_stub(body, document_id=target_celex)
            if stub is not None:
                return stub
            unit_text = _clean_text(root)
            unit_id = ""
            content_kind = ContentKind.FULL_TEXT

        publication_kind = _publication_kind_for_celex(target_celex)

        data: dict[str, Any] = {
            "celex": celex,
            "resolved_document_id": target_celex,
            # Название акта — реквизит ссылки, а не украшение: «CELEX 32016R0679,
            # ст. 5» юрист в EUR-Lex найдёт, но в процессуальный документ подают
            # акт по имени. Берётся из уже полученного тела, без второго запроса.
            "act_title": _act_title(root),
            # ELI самого прочитаного документа. Коли базовий акт прочитано в
            # консолідації, ELI акта (``…/oj``) консолідацію не ідентифікує —
            # він іде окремо як ``basic_act_eli``, а не підміняє ``eli``.
            "eli": (
                (requested_eli or eli_from_celex(celex))
                if target_celex == celex
                else eli_from_celex(target_celex)
            ),
            # Локатор доказательства — тот адрес, которым норму цитируют
            # («2», «5a», «5(1)(a)»), а не внутренний идентификатор элемента
            # разметки Cellar («art_2», «d1e267-210-1»). Последний сохраняется
            # рядом как unit_id: он нужен для отладки разбора, но по нему никто
            # не ссылается, и сверка цитаты по нему не нашла бы доказательства.
            "locator": _canonical_locator(locator) or unit_id,
            "unit_id": unit_id,
            "path": locator,
            "text": unit_text,
            "url": url,
            # Консолідацію цитують і датованим CELEX, і базовим актом із
            # зазначенням редакції: `02016R0679-20160504` і `32016R0679` — той
            # самий акт, і доказ мусить знаходитися за обома формами. Без
            # цього псевдоніма звірка власної ж прочитаної норми не знайшла б
            # її, бо розпізнавач сектора 0 віддає базовий CELEX плюс версію.
            "aliases": _document_aliases(celex, target_celex)
            + ((requested_eli,) if requested_eli else ()),
        }
        if target_celex != celex:
            data["basic_act_eli"] = requested_eli or eli_from_celex(celex)
        if resolved.alternatives:
            data["eli_alternatives"] = list(resolved.alternatives)
        if revision is not None:
            data["revision"] = {
                "consolidation_id": revision.consolidation_id,
                "valid_from": revision.valid_from,
                "valid_to": revision.valid_to,
                "known_through": revision.known_through,
                "amending_acts": list(revision.amending_acts),
                # Чим обрано редакцію: датою з запиту чи «чинна» без дати. Друге
                # — найпізніша консолідація, відома Cellar; зміни, опубліковані
                # після known_through, у ній не відображені.
                "selected_by": f"as_of={as_of_value}" if as_of_value else "current",
            }
        else:
            if current_unavailable is not None:
                data["current_revision_unavailable"] = _current_unavailable_note(
                    current_unavailable
                )
            notice = _revision_notice_for(target_celex)
            if notice is not None:
                data["amending_acts"] = list(_NO_CONSOLIDATION_AMENDING_ACTS[target_celex])
                data["revision_notice"] = notice

        provenance = ProvenanceStamp(
            source_channel=SourceChannel.LIVE,
            source_url=url,
            language=lang2,
            citation_format=", ".join(
                part
                for part in (
                    data["act_title"],
                    f"CELEX {target_celex}",
                    _locator_label(locator, unit_id),
                )
                if part
            ),
            stale=False,
            is_authentic_version=True,
            is_translation=False,
            attribution=ATTRIBUTION_EU.format(year="—"),
            version_id=target_celex,
            publication_kind=publication_kind,
            content_kind=content_kind,
        )
        return AdapterPayload(
            data=data,
            provenance=provenance,
            attribution=ATTRIBUTION_EU.format(year="—"),
        )

    def _revision_status(self, celex: str, as_of: str) -> dict[str, Any]:
        """Редакція на дату для картки: доведена, невідома чи недоступна — завжди поле."""
        revision = self.resolve_revision(celex, as_of)
        if isinstance(revision, Revision):
            return {
                "as_of": as_of,
                "status": "resolved",
                "consolidation_id": revision.consolidation_id,
                "valid_from": revision.valid_from,
                "valid_to": revision.valid_to,
                "known_through": revision.known_through,
            }
        status: dict[str, Any] = {"as_of": as_of, "status": revision.failure_code.value}
        if isinstance(revision, RevisionUnknown):
            status["known_bounds"] = list(revision.known_bounds)
        elif isinstance(revision, SourceUnavailable):
            status["reason"] = revision.reason
        else:
            status["message"] = revision.message
        return status

    @typed_failures(source_id="eu_law_eurlex_cellar")
    def card(self, document_id: str, **options: Any) -> AdapterResult:
        """Картка документа з Cellar SPARQL: ідентичність, дати, OJ, мови.

        Один запит на картку плюс перелік консолідацій (кешований) для базового
        акта сектора 3. Без ``as_of`` — найпізніша консолідація з датою не
        пізніше сьогодні; з ``as_of`` — редакція на дату через
        :meth:`resolve_revision`, недоведена — ``revision_unknown``. Збій
        переліку консолідацій картку не валить: поле лишається порожнім, а
        причина названа.
        """
        resolved = self.resolve_document_id(document_id)
        if isinstance(resolved, TypedFailure):
            return resolved
        celex = resolved.celex

        bindings = self._sparql_bindings(_card_query(celex))
        if isinstance(bindings, SourceUnavailable):
            return bindings
        row = next((item for item in bindings if _binding_value(item, "celex")), None)
        if row is None:
            return NotFound(
                identifier=celex,
                id_format_hint=(
                    "CELEX у тій формі, якою його знає Cellar (32024Q02173, "
                    "32020Q0214(01), 02016R0679-20160504) або ELI"
                ),
            )
        language_pair = _resolve_language(str(options.get("language") or ""))
        data = _card_data(row, language=language_pair[0] if language_pair else "en")
        if resolved.eli and not data["eli"]:
            data["eli"] = resolved.eli
        if resolved.alternatives:
            data["eli_alternatives"] = list(resolved.alternatives)

        # Метадані вже отримано: ні збій SPARQL, ні недоведена редакція їх не
        # скасовують — статус редакції стає полем картки, а не відмовою.
        data["latest_consolidation"] = None
        if _BASE_ACT_CELEX_RE.match(celex):
            consolidations = self._list_consolidations(celex)
            if isinstance(consolidations, SourceUnavailable):
                data["consolidations_unavailable"] = consolidations.reason
            elif isinstance(consolidations, list):
                today = self._today().isoformat()
                published = [item for item in consolidations if item[0] <= today]
                if published:
                    date_str, dated_celex = published[-1]
                    latest: dict[str, Any] = {"consolidation_id": dated_celex, "date": date_str}
                    if data["in_force"] is False:
                        # Консолідація акта, що втратив чинність, — історична
                        # редакція, а не чинна норма.
                        latest["act_in_force"] = False
                        latest["act_end_of_validity"] = data["end_of_validity"]
                    data["latest_consolidation"] = latest
        as_of_value = str(options.get("as_of") or "").strip()
        if as_of_value:
            data["revision_as_of"] = self._revision_status(celex, as_of_value)

        provenance = ProvenanceStamp(
            source_channel=SourceChannel.LIVE,
            source_url=data["url"],
            language="en",
            citation_format=f"CELEX {celex}",
            stale=False,
            is_authentic_version=True,
            is_translation=False,
            attribution=ATTRIBUTION_EU.format(year="—"),
            version_id=celex,
            content_kind=ContentKind.METADATA,
            publication_kind=PublicationKind.CARD,
        )
        return AdapterPayload(
            data=data, provenance=provenance, attribution=ATTRIBUTION_EU.format(year="—")
        )

    @typed_failures(source_id="eu_law_eurlex_cellar")
    def search(self, query: str, **options: Any) -> AdapterResult:
        max_results = int(options.get("max_results") or 20)
        sparql = _sparql_search_query(query, limit=CELLAR_RECORD_LIMIT + 1)
        bindings = self._sparql_bindings(sparql)
        if isinstance(bindings, SourceUnavailable):
            return bindings

        if len(bindings) > CELLAR_RECORD_LIMIT:
            return PartialResult(
                received=len(bindings),
                limited_by="ліміт Cellar — 10000 записів на один пошуковий запит",
            )

        results = [
            {
                "celex": _binding_value(binding, "celex"),
                "title": _binding_value(binding, "title"),
                "work": _binding_value(binding, "work"),
            }
            for binding in bindings[:max_results]
        ]
        provenance = ProvenanceStamp(
            source_channel=SourceChannel.LIVE,
            source_url=endpoint("eu.cellar_sparql"),
            language="en",
            # Здесь стоял литерал «CELEX {celex}» — незаполненный шаблон, который
            # так и уезжал в ответ. Выдача поиска — не один документ, и CELEX у
            # конверта нет: он лежит в каждой найденной карточке.
            citation_format="CELEX <номер акта>",
            stale=False,
            is_authentic_version=True,
            is_translation=False,
            attribution=ATTRIBUTION_EU.format(year="—"),
        )
        return AdapterPayload(
            data={"query": query, "results": results, "found": len(results)},
            provenance=provenance,
            attribution=ATTRIBUTION_EU.format(year="—"),
        )

    @typed_failures(source_id="eu_law_eurlex_cellar")
    def revisions(self, document_id: str) -> AdapterResult:
        """Перелік датованих консолідацій базового акта (T056).

        Метаданими, не текстом — ``content_kind=METADATA``: сама редакція на
        дату вибирається :meth:`resolve_revision`, тут лише перелік і межа
        відомості (``known_through``).
        """
        resolved = self.resolve_document_id(document_id)
        if isinstance(resolved, TypedFailure):
            return resolved
        celex = resolved.celex

        consolidations = self._list_consolidations(celex)
        if isinstance(consolidations, TypedFailure):
            return consolidations
        if not consolidations:
            return NotFound(
                identifier=celex,
                id_format_hint="CELEX базового акта з відомими консолідаціями в Cellar",
            )

        versions = [
            {"celex": dated_celex, "date": date_str, "url": _celex_url(dated_celex)}
            for date_str, dated_celex in consolidations
        ]
        provenance = ProvenanceStamp(
            source_channel=SourceChannel.LIVE,
            source_url=endpoint("eu.cellar_sparql"),
            language="en",
            # Тот же незаполненный шаблон. Здесь документ известен — это базовый
            # акт, перечень консолидаций которого и запрошен.
            citation_format=f"CELEX {celex}",
            stale=False,
            is_authentic_version=True,
            is_translation=False,
            attribution=ATTRIBUTION_EU.format(year="—"),
            content_kind=ContentKind.METADATA,
            publication_kind=PublicationKind.CARD,
        )
        return AdapterPayload(
            data={
                "base_celex": celex,
                "versions": versions,
                "found": len(versions),
                "known_through": consolidations[-1][0],
            },
            provenance=provenance,
            attribution=ATTRIBUTION_EU.format(year="—"),
        )

    def health(self) -> SourceHealth:
        try:
            response = self._transport.get(
                endpoint("eu.cellar_sparql"),
                params={"query": "ASK { ?s ?p ?o }"},
                headers={"Accept": "application/sparql-results+json"},
                timeout=10,
            )
            ok = response.status_code == 200
            details: dict[str, Any] = {"status_code": response.status_code}
        except requests.RequestException as error:
            ok = False
            details = {"error": type(error).__name__}
        return SourceHealth(
            adapter=self.source_id, ok=ok, source_policy=self.source_policy, details=details
        )


register_adapter(EuLawAdapter())
