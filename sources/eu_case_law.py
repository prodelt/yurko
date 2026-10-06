"""Адаптер практики Суду ЄС (T059; публічна картка непубікованого провадження T151).

У CURIA (InfoCuria) офіційного API/дампу немає (research/01-eu-sources.md, §2):
фактичний машиночитаний шлях до рішень Суду ЄС — той самий Cellar, яким читає
право ЄС :mod:`sources.eu_law`. ECLI призначено всім рішенням з 1954 року і є
тут ідентифікатором документа — так само, як CELEX для законодавства.

Пошук іде через SPARQL за типом ресурсу (``JUDG``/``ORDER``/``OPIN_AG``, за
цифрами 07.2026: 34 261/8 362/14 480), читання — через REST зі згодженням
змісту.

:meth:`EuCaseLawAdapter.search` (гілка ``cites``, T-cites, 2026-09-09) —
структурна операція «документи, що цитують акт»: вхід — CELEX акта
(перевірений формою на вході інструмента, вільний текст сюди не доходить), а
не номер справи чи ECLI шуканого документа. Побудова запиту —
:func:`build_cited_by_sparql`; ``?work cdm:work_cites_work ?cited`` — та сама
властивість Cellar, якою SPARQL зв'язує будь-який акт із тим, що на нього
посилається. Живий прогін 2026-09-09 на CELEX одного регламенту: кілька
сотень документів Суду ЄС і Загального суду за COUNT. Видача навігаційна (як і решта
пошуків): CELEX, ECLI, дата, компактний код виду (``TJ``/``CO``/``CN``…) і
назва справи, коли вона дістається тим самим запитом — тексту нема.

Розмітка рішення **не та сама**, що в законодавства, і не Akoma Ntoso: Cellar
відхиляє ``Accept: application/akn+xml`` кодом 400 (research/13). Саме тому тут
свій розбір, а не спільний з :mod:`sources.eu_law`: спільним у них лишається
транспорт (``_celex_url``, узгодження мови), а не структура документа.

Родин розмітки три, і подача в Cellar у них різна — це доведено перебором
заголовків 2026-09-08:

* сучасна (рішення, новіші за середину 2012 р.): лише ``Accept: application/xhtml+xml``; класи
  ``coj-*``, пункт лежить у таблиці двома клітинками — номер у ``p.coj-count``
  з ``id="pointN"``, текст у сусідній клітинці (живий розбір 62019CJ0487);
* середня (рішення 2000-х — початку 2010-х років):
  лише ``Accept: text/html``; пункт — абзац ``P.C01PointnumeroteAltN``, номер
  стоїть провідними цифрами всередині самого абзацу;
* стара (рішення 1990-х): теж лише ``text/html``; ``div#TexteOnly`` з якорями
  ``SM``/``I1``/``MO``/``CO``/``DI``, пункти — прості ``<p>N …</p>`` після
  якоря Grounds.

Звідси два правила. Формат перебирається, а не задається один: сучасне рішення
на ``text/html`` віддає 404, старе на ``xhtml`` — теж 404, і питати лише один
формат означало б оголосити відсутнім документ, який у джерелі є. Відсутність
формату — не відсутність документа (принцип III), тому вичерпаний перебір дає
``not_covered`` з ручним шляхом, а не ``not_found`` «такого ідентифікатора
немає». ``retrieved_format`` і ``fetch_attempts`` у відповіді показують, чим
саме прочитано й скількома спробами.

Ідентифікатор читання — ECLI або CELEX судової практики: CELEX стоїть у видачі
пошуку й у публічній картці провадження, і не приймати його на вхід означало б
віддавати адресу, якою не можна прочитати.

:meth:`EuCaseLawAdapter.public_card` (ADR 0006, T151) — публічна картка
провадження за номером справи (``C-605/26``), а не за ECLI: номер справи
з'являється до того, як з'явиться ECLI рішення, і саме так юрист посилається
на відкрите провадження. InfoCuria віддає лише Angular-оболонку без
серверного вмісту (research/13, §A6) — картка її НЕ парсить. Перший канал —
Cellar SPARQL; коли публікацій там немає, другий — легасі-картка CURIA
``juris.curia.europa.eu`` (:mod:`sources.curia_legacy`, 15.09.2026): дата
подання, суд, що звернувся, предмет, мова справи, документи. Поле ``channel``
називає, яким каналом отримано картку. Відсутність документів — чесна картка з
``published=False``, а недоступність CURIA — ``source_unavailable`` з ручним
шляхом, а не «не опубліковано». Жодних спроб e-Curia — ані облікових даних,
ані підписок, ані звернень до закритих ендпоінтів.
"""

from __future__ import annotations

import datetime as dt
import re
from datetime import timezone
from typing import Any

import requests

from core.contracts import NotCovered, NotFound, SourceHealth, SourcePolicy, SourceUnavailable
from core.legal_orders import CoverageLayer, Source, SourceAccess, SourceLimits
from core.provenance import ContentKind, ProvenanceStamp, PublicationKind, SourceChannel
from core.source_endpoints import endpoint
from sources import register_adapter
from sources.base import AdapterPayload, AdapterResult, RegistryAdapter, typed_failures
from sources.curia_legacy import CuriaLegacyCard
from sources.eu_law import (
    ATTRIBUTION_EU,
    CELLAR_RECORD_LIMIT,
    _celex_url,
    _clean_text,
    _parse_xhtml,
    _resolve_language,
    _XSD_STRING,
)
from sources.transport import CellarReader, SourceTransport

__all__ = [
    "EuCaseLawAdapter",
    "extract_judgment_paragraph",
    "is_case_law_celex",
    "is_valid_ecli",
    "build_cited_by_sparql",
    "build_case_number_sparql",
    "case_number_celex_candidates",
]

_ECLI_RE = re.compile(r"^ECLI:[A-Z]{2}:[A-Z0-9]+:\d{4}:[A-Za-z0-9.]+$")

#: CELEX судової практики: ``6`` + рік + дві літери типу + чотири цифри номера,
#: за потреби з суфіксом супровідного запису (``…_SUM`` — резюме, ``…_INF``).
#: Суфікс тут приймається на вхід свідомо: якщо юрист просить саме резюме, читати
#: замість нього рішення означало б відповісти не на те питання.
_CASE_LAW_CELEX_RE = re.compile(r"^6\d{4}[A-Z]{2}\d{4}(?:_[A-Z0-9]{1,8})?$")

#: Суфікс номера справи: ``P`` (апеляція), ``R`` (тимчасові заходи), ``DEP``
#: (витрати), ``PPU``, ``REV``, ``P-DEP``, ``P(R)``… Перелік відкритий, тому
#: описується формою (великі латинські літери, за потреби через дефіс або з
#: уточненням у дужках), а не вгадуванням: нерозібрана форма лишається
#: нерозібраною.
_CASE_SUFFIX_TOKEN = r"[A-Z]{1,4}(?:\((?:R|I)\))?(?:-[A-Z]{1,4})?"

#: Номер справи Суду ЄС: форум (палата), номер, дворозрядний рік і, за потреби,
#: суфікс виду провадження. Суфікс на CELEX не впливає (``62000C[A-Z]0*0050``
#: знаходить ту саму справу з ``P`` і без нього), але без нього в регулярці весь
#: номер «C-50/00 P» відкидався як нерозпізнаний.
_CASE_NUMBER_RE = re.compile(
    r"^(?P<forum>[CTF])-(?P<num>\d{1,4})/(?P<yy>\d{2})"
    rf"(?:\s+(?P<suffix>{_CASE_SUFFIX_TOKEN}(?:\s+{_CASE_SUFFIX_TOKEN})*))?$"
)

#: Форма, у якій ідентифікатор цього форуму випускається назовні (T237, FR-306).
#: Загальний шаблон :data:`egress._IDENTIFIER_RE` пробіл не пропускає, тому
#: ``assert_identifier('C-50/00 P')`` давав ``EgressDenied``: суфікс виду
#: провадження відокремлений пробілом, і без оголошеної форми номер апеляції не
#: доїжджав до джерела взагалі. Загальний шаблон ослабляти не можна — він
#: тримає вільний текст; форму оголошує форум, як уже зроблено в
#: ``sources/echr.py``. Об'єднання, а не лише номер справи: поле ``case_number``
#: цього форуму законно приймає ще CELEX судової практики й ECLI (``id_format_hint``
#: обіцяє саме їх), і звуження до одного номера відкинуло б те, що працює.
_CASE_NUMBER_EGRESS_RE = re.compile(
    "|".join(
        (
            rf"^[CTF]-\d{{1,4}}/\d{{2}}(?:\s+{_CASE_SUFFIX_TOKEN}(?:\s+{_CASE_SUFFIX_TOKEN})*)?$",
            r"^6\d{4}[A-Z]{2}\d{4}(?:_[A-Z0-9]{1,8})?$",
            r"^ECLI:[A-Z]{2}:[A-Z0-9]+:\d{4}:[A-Za-z0-9.]+$",
        )
    )
)

#: Порядок форматів читання. Не косметика: сучасні рішення віддаються лише як
#: XHTML, середні й старі — лише як ``text/html``, і кожне з них відповідає 404
#: на «чужий» заголовок. XHTML стоїть першим, бо в ньому пункт розібраний
#: розміткою (``id="pointN"``), а не провідними цифрами тексту.
_DOCUMENT_FORMATS: tuple[tuple[str, str], ...] = (
    ("xhtml", "application/xhtml+xml, application/xml;q=0.8"),
    ("html", "text/html"),
)

#: Коди, після яких має сенс спитати документ іншим форматом. 404 — «такої
#: подачі немає», 400 — «такий заголовок не приймається»; обидва стосуються
#: формату, а не документа. Решта 4xx/5xx — стан джерела, і перебирати їх
#: означало б ховати недоступність за виглядом «не опубліковано».
_FORMAT_RETRY_CODES = frozenset({400, 404})

#: Рік, з якого дворозрядний рік номера справи означає XX сторіччя. Нумерація
#: Суду ЄС з дворозрядним роком іде з 1953-го й не переривалася, тож «98» — це
#: 1993 (C-415/93, Bosman), а «05» — 2005 (C-438/05, Viking Line).
_CASE_NUMBER_CENTURY_PIVOT = 53

#: Форум за першою літерою номера справи — вона ж перша літера CELEX-типу.
_FORUM_NAMES: dict[str, str] = {
    "C": "Суд Європейського Союзу (Court of Justice)",
    "T": "Загальний суд (General Court)",
    "F": "Трибунал з питань публічної служби (розформований 2016)",
}

#: Друга літера CELEX-типу справи → який це документ. Лише впевнено відомі
#: коди (research/01-eu-sources.md §2: JUDG/ORDER/OPIN_AG); невідомий код не
#: вгадується, а показується як є (``resource_legal_type`` без перекладу).
_KIND_NAMES: dict[str, str] = {
    "J": "рішення (judgment)",
    "O": "ухвала (order)",
    "C": "висновок генерального адвоката (opinion of Advocate General)",
    "N": "повідомлення про відкриття провадження (notice, OJ C)",
}


def _case_number_year(two_digit_year: str) -> str:
    """Дворозрядний рік номера справи → чотирирозрядний, вікном сторіч.

    Раніше тут стояла константа «20XX», і кожна справа XX сторіччя діставала
    неіснуючий CELEX: ``C-415/93`` шукався як ``62093C…`` (2093 рік!) і давав
    ``not_published`` — відповідь хибну, бо Bosman опубліковано
    (``61993CJ0415``, ECLI:EU:C:1995:463). Вікно виведене з нумерації, а не
    вгадане: дворозрядний рік іде з 1953-го, тож ``>= 53`` — це 19XX.
    """
    digits = str(two_digit_year or "").strip()
    if not digits.isdigit():
        return f"20{digits}"
    return f"19{digits}" if int(digits) >= _CASE_NUMBER_CENTURY_PIVOT else f"20{digits}"


def is_valid_ecli(value: str) -> bool:
    return bool(_ECLI_RE.match(str(value or "").strip()))


def is_case_law_celex(value: str) -> bool:
    """CELEX судової практики — другий припустимий ідентифікатор читання."""
    return bool(_CASE_LAW_CELEX_RE.match(str(value or "").strip()))


def _binding_value(binding: dict[str, Any], key: str) -> str:
    return str((binding.get(key) or {}).get("value") or "")


#: Номер пункту рішення: лише цифри. «45a» чи «45.1» у нумерації Суду ЄС не
#: трапляються, і приймати їх означало б обіцяти локатор, якого немає.
_PARAGRAPH_RE = re.compile(r"^\d{1,4}$")


def _normalize_paragraph(path: str) -> str:
    """``§ 45`` / ``п. 45`` / ``para 45`` → ``45``; невпізнане — порожньо."""
    cleaned = str(path or "").strip()
    # Довші ярлики стоять перед коротшими: інакше «para» з'їдає початок слова
    # «paragraph» і лишає «graph 45», який уже не розбирається.
    cleaned = re.sub(r"(?i)^(§+|пункт|п\.?|paragraph|para\.?|point)\s*", "", cleaned).strip()
    cleaned = cleaned.rstrip(".")
    return cleaned if _PARAGRAPH_RE.match(cleaned) else ""


#: Провідний номер пункту всередині абзаца: цифри, за якими йде пробіл, і далі
#: текст. Крапка після цифр відсікається навмисно — «4. as regards a civil
#: claim» у старій розмітці це рядок цитованого переліку всередині пункту 5, а
#: не пункт 4, і взяти його за пункт означало б видати чужий текст за потрібний.
_LEADING_NUMBER_RE = re.compile(r"^(\d{1,4})(?!\.)\s+(.+)$")


def _paragraph_by_point_id(root: Any, number: str) -> str | None:
    """Сучасна розмітка ``coj-*``: номер і текст — дві клітинки одного рядка.

    ``p.coj-count`` з ``id="pointN"`` несе сам номер, текст лежить у сусідній
    клітинці (живий розбір 62019CJ0487). Тому за id береться не текст елемента —
    там був би самий номер, — а вміст останньої клітинки рядка.
    """
    marker = root.xpath(f'.//*[@id="point{number}"]')
    if not marker:
        return None
    row = marker[0]
    while row is not None and row.tag != "tr":
        row = row.getparent()
    if row is None:
        return None
    cells = row.findall("td")
    if len(cells) < 2:
        return None
    text = _clean_text(cells[-1])
    return text or None


def _paragraph_by_numbered_class(root: Any, target: int) -> str | None:
    """Середня розмітка: номер пункту — провідні цифри самого абзаца.

    ``<P class="C01PointnumeroteAltN">45&nbsp;&nbsp;…текст…</P>`` (середня
    родина розмітки). Клас відбирає лише пронумеровані
    пункти, тож цитовані переліки з такими самими цифрами (``C09Marge0…``) сюди
    не потрапляють. Нерозривні пробіли зникають разом із рештою у ``_clean_text``.
    """
    for node in root.xpath('.//p[contains(@class,"C01Pointnumerote")]'):
        match = _LEADING_NUMBER_RE.match(_clean_text(node))
        if match is not None and int(match.group(1)) == target:
            return match.group(2).strip() or None
    return None


def _paragraph_in_text_only(root: Any, target: int) -> str | None:
    """Стара розмітка ``div#TexteOnly``: пункти — прості ``<p>N …</p>``.

    Класу в цих абзацах немає взагалі, а поряд із пунктами стоять заголовки
    розділів і цитовані переліки, тож єдина надійна ознака — послідовність:
    пункти йдуть підряд від першого після якоря Grounds (``#MO``) і далі, через
    ``#CO`` (пункт про витрати нумерацію продовжує). Тому номер приймається,
    лише коли він дорівнює очікуваному наступному: інакше випадкове «1968
    Convention provides» на початку абзаца стало б «пунктом 1968» і збило б увесь
    відлік.

    Обхід лінійний, а не осями XPath: ``<a name="MO"/>`` — самозакритий тег у
    HTML, і що з ним зробить відновлювальний парсер, залежить від документа;
    порядок елементів від цього не залежить.
    """
    if not root.xpath('.//div[@id="TexteOnly"]'):
        return None
    expected = 1
    after_grounds = False
    for element in root.iter():
        if not isinstance(element.tag, str):
            continue
        if (element.get("name") or element.get("id")) == "MO":
            after_grounds = True
            continue
        if not after_grounds or element.tag != "p":
            continue
        match = _LEADING_NUMBER_RE.match(_clean_text(element))
        if match is None or int(match.group(1)) != expected:
            continue
        if expected == target:
            return match.group(2).strip() or None
        expected += 1
    return None


def extract_judgment_paragraph(root: Any, number: str) -> str | None:
    """Текст пронумерованого пункту рішення Суду ЄС, або ``None``.

    Розборів три — по одному на родину розмітки Cellar (див. docstring модуля).
    Порядок від найнадійнішого: у сучасній розмітці номер стоїть у розмітці, у
    двох інших його доводиться читати з тексту абзаца.

    Відсутність пункту повертає ``None``, і викликач перетворює це на
    ``not_found``: сусідній пункт замість запитаного був би справжнім текстом
    під чужою адресою, тобто рівно тією вигаданою цитатою, яку забороняє
    принцип II.
    """
    by_id = _paragraph_by_point_id(root, number)
    if by_id:
        return by_id
    if not _PARAGRAPH_RE.match(str(number or "")):
        return None
    target = int(number)
    return _paragraph_by_numbered_class(root, target) or _paragraph_in_text_only(root, target)


def _judgment_text(root: Any) -> str:
    """Повний текст рішення: тіло документа без розмітки.

    Раніше сюди потрапляв сам XHTML, зведений по пробілах, — тобто розмітка,
    видана за текст норми. Тепер береться вміст ``#judgment`` (а якщо його
    немає — ``body``), і текст лишається текстом.
    """
    for xpath in ('.//*[@id="judgment"]', ".//body"):
        found = root.xpath(xpath)
        if found:
            text = _clean_text(found[0])
            if text:
                return text
    return _clean_text(root)


#: CELEX судових справ: варіанти з суфіксом (``…_RES``, ``…_INF``) — супровідні
#: записи, а не сам документ. SPARQL повертає їх упереміш, і брати перший-ліпший
#: означало б читати не те, що просили.
_PLAIN_CASE_CELEX_RE = re.compile(r"^6\d{4}[A-Z]{2}\d{4}$")


def _pick_document_celex(candidates: list[str]) -> str:
    """Основний CELEX документа з видачі SPARQL, інакше перший непорожній."""
    for celex in candidates:
        if _PLAIN_CASE_CELEX_RE.match(celex):
            return celex
    return next((celex for celex in candidates if celex), "")


def _resolve_ecli_sparql(ecli: str) -> str:
    escaped = str(ecli or "").replace('"', '\\"')
    return (
        "PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>\n"
        "SELECT ?work ?celex ?parties ?case ?date WHERE {\n"
        "  ?work cdm:case-law_ecli ?ecli .\n"
        f'  FILTER(STR(?ecli) = "{escaped}")\n'
        "  ?work cdm:resource_legal_id_celex ?celex .\n" + _CASE_METADATA_CLAUSES + "}\n"
        # Не LIMIT 1: у видачі трапляються супровідні записи (``…_RES``) поряд
        # із самим рішенням, і перший рядок не обов'язково той документ.
        "LIMIT 20"
    )


def _resolve_celex_sparql(celex: str) -> str:
    """Зворотний бік того самого предиката: CELEX → ECLI.

    Маршрут у джерелі був завжди (CELEX → ECLI, живий запит 2026-09-08),
    просто на вхід ``fetch`` його не приймали.

    ECLI тут ``OPTIONAL`` навмисно: інакше «немає ECLI» і «немає такого CELEX»
    дали б однакові нуль рядків, і неіснуючий ідентифікатор довелося б називати
    неопублікованим документом. З ``OPTIONAL`` нуль рядків означає рівно одне —
    такого CELEX у Cellar немає (перевірено на ``69999CJ9999``, 2026-09-08).
    """
    escaped = str(celex or "").replace('"', '\\"')
    return (
        "PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>\n"
        "SELECT ?ecli ?parties ?case ?date WHERE {\n"
        "  ?work cdm:resource_legal_id_celex ?celex .\n"
        f'  FILTER(STR(?celex) = "{escaped}")\n'
        "  OPTIONAL { ?work cdm:case-law_ecli ?ecli }\n" + _CASE_METADATA_CLAUSES + "}\n"
        "LIMIT 20"
    )


#: Реквізити рішення, які Cellar тримає **на рівні виразу**, а не твору
#: (живий запит 2026-09-09 на чотирьох рішеннях різних років):
#: ``expression_case-law_parties`` — назва справи («Associação Sindical
#: dos Juízes Portugueses v Tribunal de Contas»),
#: ``expression_case-law_identifier_case`` — її номер («Case C-64/16», «Case
#: C-50/00 P»). Дата документа лежить на рівні твору
#: (``work_date_document``).
#:
#: Клаузи дописуються до резолвера ідентифікатора, а не йдуть окремим запитом:
#: читання рішення й без того коштує двох звернень (SPARQL + документ), і третє
#: заради назви справи платив би кожен виклик.
#:
#: Вираз береться англійський навмисно, а не мовою читання: назва справи — це
#: ідентифікатор провадження, за яким його шукають у CURIA і цитують, і вона не
#: змінюється від того, якою мовою прочитаний текст.
#:
#: Усе ``OPTIONAL``: реквізити не є умовою читання. Документ, у якого сторін у
#: Cellar немає (супровідні записи), читається так само — посилання на нього
#: буде коротшим, а не вигаданим.
_CASE_METADATA_CLAUSES = (
    "  OPTIONAL { ?work cdm:work_date_document ?date }\n"
    "  OPTIONAL { ?expr cdm:expression_belongs_to_work ?work .\n"
    "            ?expr cdm:expression_uses_language "
    "<http://publications.europa.eu/resource/authority/language/ENG> .\n"
    "            OPTIONAL { ?expr cdm:expression_case-law_parties ?parties }\n"
    "            OPTIONAL { ?expr cdm:expression_case-law_identifier_case ?case } }\n"
)

#: Як реквізит зветься у видачі SPARQL і як — у відповіді адаптера.
_CASE_METADATA_FIELDS = (
    ("parties", "case_name"),
    ("case", "case_number"),
    ("date", "judgment_date"),
)


def _case_metadata(bindings: list[dict[str, Any]]) -> dict[str, str]:
    """Назва справи, її номер і дата з видачі резолвера — або порожньо.

    Порожньо означає рівно «джерело цього не віддало». Жодне поле не
    добудовується з іншого: вигадана назва справи гірша за її відсутність.
    """
    found: dict[str, str] = {}
    for binding in bindings:
        for key, field in _CASE_METADATA_FIELDS:
            value = _binding_value(binding, key).strip().rstrip(".")
            if value and field not in found:
                found[field] = value
    return found


def _case_kind(celex: str) -> str:
    """Вид документа за другою літерою CELEX судової практики.

    Потрібен саме тут: ``62020CN0340`` — це повідомлення в OJ C про відкриття
    провадження, а не рішення, і друкувати його в посиланні як «рішення Суду ЄС»
    означало б видати анонс за судовий акт.
    """
    cleaned = str(celex or "").strip()
    if not _CASE_LAW_CELEX_RE.match(cleaned):
        return ""
    return _KIND_NAMES.get(cleaned[6:7], "")


def _case_court(celex: str) -> str:
    """Форум за першою літерою типу CELEX — та сама мапа, що в публічній картці."""
    letters = str(celex or "").strip()[5:6]
    return _FORUM_NAMES.get(letters, "")


def _is_oj_notice(celex: str) -> bool:
    """``62020CN0340`` — повідомлення в Офіційному віснику (серія C), не рішення.

    Друга літера типу CELEX — ``N``. До цього ``fetch`` віддавав такий документ
    з ``publication_kind=court_publication`` завжди: юрист бачив «офіційна
    публікація судового акта» там, де ні Суд, ні Загальний суд нічого не
    видавали — саму лише мітку про відкрите провадження (T-cites, 2026-09-09).
    """
    return str(celex or "").strip()[6:7] == "N"


def _search_sparql(query: str, *, limit: int) -> str:
    escaped = str(query or "").replace('"', '\\"')
    return (
        "PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>\n"
        "SELECT ?work ?celex ?ecli ?title WHERE {\n"
        "  ?work cdm:resource_legal_id_celex ?celex .\n"
        "  ?work cdm:case-law_ecli ?ecli .\n"
        "  ?work cdm:work_title ?title .\n"
        f'  FILTER(CONTAINS(LCASE(STR(?title)), LCASE("{escaped}")))\n'
        "}\n"
        f"LIMIT {int(limit)}"
    )


# ---------------------------------------------------------------------------
# T-cites (2026-09-09) — «документи, що цитують акт»
# ---------------------------------------------------------------------------
#
# Живий запит 2026-09-09 на CELEX одного регламенту: 60 рядків за LIMIT 60
# (COUNT — кілька сотень усього), упорядковані за датою документа. ECLI на
# рівні твору (``cdm:case-law_ecli``, без OPTIONAL-виразу) і назва/номер справи
# (``expression_case-law_parties``/``…_identifier_case``, англійський вираз —
# та сама пара, що вже читає :data:`_CASE_METADATA_CLAUSES`) дістаються тим
# самим запитом безкоштовно — окремого звернення на реквізити не треба.

#: Форум за префіксом CELEX судового акта: шоста позиція CELEX (1-індексація
#: SPARQL) — той самий розряд, що читає :func:`_case_court`. Необов'язковий
#: фільтр видачі: без нього повертається практика і Суду, і Загального суду.
_CITED_BY_COURT_LETTERS = frozenset({"C", "T"})

#: Види судових документів, які входять у добір «хто цитує акт» за
#: замовчуванням: рішення (``J``) і ухвали (``O``). Висновок генерального
#: адвоката (``C``) і повідомлення Офіційного вісника (``N``) виключені —
#: вони не є рішенням форуму, і в референс-наборі оцінки шансів (карта 006,
#: FR-641) їх наявність зсувала б результат тим, чого суд не вирішував.
CITED_BY_DEFAULT_KINDS: tuple[str, ...] = ("J", "O")


def build_cited_by_sparql(
    celex: str,
    *,
    date_from: str = "",
    date_to: str = "",
    court: str = "",
    limit: int = 10,
    kinds: tuple[str, ...] = CITED_BY_DEFAULT_KINDS,
) -> str:
    """SPARQL «документи, що цитують акт ``celex``» (Cellar, ``work_cites_work``).

    ``celex`` іде в запит типізованим рядком (``^^xsd:string``) — точно так,
    як його віддає власний SPARQL-ендпоінт Cellar і як його приймає жива
    проба 2026-09-09: без типу рядок і типізований літерал не збігаються, і
    видача була б порожньою для акта, який насправді цитують.
    ``FILTER(STRSTARTS(STR(?celex), "6"))`` лишає лише судову практику: акт
    цитують і інші акти (наприклад, регламент, що вносить зміни), а тут
    потрібна практика Суду ЄС і Загального суду.
    """
    escaped = str(celex or "").replace('"', '\\"')
    filters = ['FILTER(STRSTARTS(STR(?celex), "6"))']
    # T434 (FR-662): референс-набір будується з **рішень**, а не з усього, що
    # цитує акт. Висновок генерального адвоката (``C``) і повідомлення в
    # Офіційному віснику (``N``) — не рішення форуму, і рахувати їх нарівні з
    # рішеннями означало б рахувати те, чого суд не вирішував. Перелік видів —
    # параметр, а не константа: юрист, якому потрібні висновки, називає їх явно.
    wanted_kinds = tuple(letter.upper() for letter in kinds if str(letter).strip())
    if wanted_kinds:
        allowed = ", ".join(f'"{letter}"' for letter in sorted(set(wanted_kinds)))
        filters.append(f"FILTER(SUBSTR(STR(?celex), 7, 1) IN ({allowed}))")
    letter = str(court or "").strip().upper()
    if letter in _CITED_BY_COURT_LETTERS:
        filters.append(f'FILTER(SUBSTR(STR(?celex), 6, 1) = "{letter}")')
    if date_from:
        filters.append(
            f'FILTER(BOUND(?date) && ?date >= "{date_from}"'
            "^^<http://www.w3.org/2001/XMLSchema#date>)"
        )
    if date_to:
        filters.append(
            f'FILTER(BOUND(?date) && ?date <= "{date_to}"'
            "^^<http://www.w3.org/2001/XMLSchema#date>)"
        )
    filter_clauses = "".join(f"  {clause}\n" for clause in filters)
    return (
        "PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>\n"
        "SELECT DISTINCT ?celex ?date ?ecli ?parties ?case WHERE {\n"
        f'  ?cited cdm:resource_legal_id_celex "{escaped}"{_XSD_STRING} .\n'
        "  ?work cdm:work_cites_work ?cited .\n"
        "  ?work cdm:resource_legal_id_celex ?celex .\n"
        "  OPTIONAL { ?work cdm:case-law_ecli ?ecli }\n"
        + _CASE_METADATA_CLAUSES
        + filter_clauses
        + "}\n"
        "ORDER BY ?date\n"
        f"LIMIT {int(limit)}"
    )


#: Реквізити, за якими обирається «багатший» з двох рядків тієї самої справи.
_CITED_BY_METADATA_KEYS = ("ecli", "parties", "case", "date")


def _dedupe_cited_by(bindings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Один work — один рядок (жива проба 2026-09-09 на 62022TO0193).

    ``OPTIONAL``-блок реквізитів (``_CASE_METADATA_CLAUSES``) інколи знаходить
    для однієї справи два вирази англійською з різним набором заповнених
    полів — ``SELECT DISTINCT`` тоді бачить два різні рядки, бо різняться не
    ``?celex``, а супровідні поля. Без дедуплікації один документ показувався
    б у видачі двічі й даром їв бюджет ``max_results``. Із двох лишається
    той, у якого заповнених реквізитів більше — жоден рядок не вигадується.
    """
    best: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for binding in bindings:
        celex = _binding_value(binding, "celex")
        if not celex:
            continue
        if celex not in best:
            best[celex] = binding
            order.append(celex)
            continue
        richer = sum(bool(_binding_value(binding, key)) for key in _CITED_BY_METADATA_KEYS)
        existing = sum(bool(_binding_value(best[celex], key)) for key in _CITED_BY_METADATA_KEYS)
        if richer > existing:
            best[celex] = binding
    return [best[celex] for celex in order]


#: Види судових документів, які номер справи розкриває в CELEX: рішення (``J``),
#: ухвала (``O``) і висновок генерального адвоката (``C``, лише Суд). Загальний суд
#: і Трибунал з питань публічної служби висновків не мають. Повідомлення в OJ C
#: (``N``), резюме й інші супровідні записи сюди не входять і не шукаються.
_CASE_NUMBER_CELEX_KINDS: dict[str, tuple[str, ...]] = {
    "C": ("J", "O", "C"),
    "T": ("J", "O"),
    "F": ("J", "O"),
}

#: Що кожна відповідь пошуку за номером каже про межі цього шляху. Стоїть завжди,
#: а не лише при порожній видачі: порожньо буває з двох причин («такого рішення
#: немає» і «CELEX цього номера не існує»), і без цього тексту вони однакові.
CASE_NUMBER_SEARCH_NOTICE = (
    "Номер справи перетворюється на CELEX (6 + рік + вид + номер до чотирьох цифр) "
    "для рішення (J), ухвали (O) і висновку генерального адвоката (C, лише Суд); "
    "повідомлення в OJ C, резюме й інші супровідні записи не шукалися. "
    "В об'єднаних справах CELEX має лише перша справа: за номером другої "
    "рішення може не знайтися, хоча воно є — шукайте за номером першої справи "
    "або в InfoCuria."
)


def case_number_celex_candidates(case_number: str) -> tuple[str, ...] | None:
    """CELEX-кандидати за номером справи (``C-311/18`` → ``62018CJ0311``, ``…CO…``, ``…CC…``).

    ``None`` — вхід не схожий ні на номер справи цього форуму, ні на CELEX судової
    практики. CELEX, поданий як номер, лишається єдиним кандидатом: його не
    «виправляють» на інший вид документа.
    """
    text = str(case_number or "").strip()
    if _CASE_LAW_CELEX_RE.match(text):
        return (text,)
    match = _CASE_NUMBER_RE.match(text)
    if not match:
        return None
    forum = match.group("forum")
    year = _case_number_year(match.group("yy"))
    number = match.group("num").zfill(4)
    return tuple(f"6{year}{forum}{kind}{number}" for kind in _CASE_NUMBER_CELEX_KINDS[forum])


def build_case_number_sparql(celexes: tuple[str, ...]) -> str:
    """SPARQL «які з цих CELEX є в Cellar, і з яким ECLI та датою».

    Кандидати йдуть типізованим рядком (``^^xsd:string``), як і в запиті цитувань:
    без типу літерал не збігається з тим, що зберігає Cellar. ``VALUES`` замість
    ``REGEX`` за всіма CELEX: точний пошук, ~1 с (жива проба 29.09.2026), а не
    скан усього сховища. ECLI ``OPTIONAL``: рішення без ECLI лишається у видачі.
    """
    values = " ".join(f'"{str(celex).replace(chr(34), "")}"{_XSD_STRING}' for celex in celexes)
    return (
        "PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>\n"
        "SELECT ?celex ?date ?ecli ?parties ?case WHERE {\n"
        "  ?work cdm:resource_legal_id_celex ?celex .\n"
        f"  VALUES ?celex {{ {values} }}\n"
        "  OPTIONAL { ?work cdm:case-law_ecli ?ecli }\n" + _CASE_METADATA_CLAUSES + "}\n"
        "ORDER BY ?date ?celex"
    )


def _cited_by_result_item(binding: dict[str, Any]) -> dict[str, Any]:
    """Один рядок видачі «цитувань» → навігаційний запис (без тексту, T-cites).

    Тексту тут немає навмисно: перелік цитувань — навігація, не доказ
    (принцип II); хто цитує — читає рішення окремим ``fetch`` за CELEX/ECLI.
    """
    celex = _binding_value(binding, "celex")
    ecli = _binding_value(binding, "ecli")
    case_name = _binding_value(binding, "parties").rstrip(".")
    case_number = _binding_value(binding, "case").rstrip(".")
    date = _binding_value(binding, "date")
    return {
        "id": celex or ecli,
        "celex": celex,
        "ecli": ecli or None,
        # Компактний код (CJ/CO/CC/TJ/TO/TN…) — друга й шоста позиції CELEX
        # судової практики; показаний завжди, навіть коли _KIND_NAMES не знає
        # опису (невідомий код не вгадується, а показується як є).
        "document_type": celex[5:7] if len(celex) >= 7 else "",
        "kind": _case_kind(celex),
        "court": _case_court(celex),
        "case_name": case_name or None,
        "case_number": case_number or None,
        "date": date or None,
        "url": f"{endpoint('eu.cellar_celex')}/{celex}" if celex else "",
    }


class EuCaseLawAdapter(CellarReader, RegistryAdapter):
    """Практика Суду ЄС: ECLI, читання й пошук через Cellar."""

    source_id = "eu_case_law_cellar"
    id_format_hint = (
        "ECLI, наприклад ECLI:EU:C:2022:100, або CELEX судової практики, " "наприклад 62016CJ0064"
    )
    legal_order = "EU"
    source_policy = SourcePolicy.API
    #: Номер справи цього форуму містить пробіл перед суфіксом виду
    #: провадження, тож загальний шаблон його не пропускає (T237).
    case_number_pattern = _CASE_NUMBER_EGRESS_RE
    #: `date_from`/`date_to` доведено живим SPARQL-фільтром 2026-09-09 для
    #: операції `cites` (T-cites) — за `work_date_document`, включно.
    supports_date_filter = True

    def __init__(self, session: requests.Session | None = None) -> None:
        self._session = session or requests.Session()
        #: Звернення до Cellar і CURIA — через транспорт, що не перевикористовує
        #: з'єднання, скинуте мережею під час простою (:mod:`sources.transport`).
        self._transport = SourceTransport(self._session)
        self._curia = CuriaLegacyCard(self._transport, source_id=self.source_id)

    def policy(self) -> Source:
        return Source(
            id=self.source_id,
            legal_order=self.legal_order,
            layer=CoverageLayer.CONNECTED,
            access=SourceAccess.OFFICIAL_API,
            license="Commission Decision 2011/833/EU",
            attribution_required=True,
            attribution=ATTRIBUTION_EU.format(year="—"),
            limits=SourceLimits(
                max_records_per_request=CELLAR_RECORD_LIMIT,
                notes=(
                    "офіційного API/дампу у CURIA немає; практика читається через "
                    "Cellar — ту саму інфраструктуру, що й законодавство ЄС"
                ),
            ),
            source_url=endpoint("eu.eurlex"),
            description="Практика Суду ЄС через Cellar; ECLI — ідентифікатор рішення.",
        )

    def _read_document(
        self, url: str, accept_language: str
    ) -> tuple[str, str, int] | SourceUnavailable:
        """Тіло документа, формат, яким його віддали, і скільки було спроб.

        Порожній формат у відповіді означає: жоден із перебраних форматів
        документа не віддав. Це не помилка транспорту й не відсутній
        ідентифікатор — це відсутня машиночитана подача, і викликач мусить
        сказати саме це.
        """
        attempts = 0
        for name, accept in _DOCUMENT_FORMATS:
            attempts += 1
            response = self._get(
                url,
                headers={"Accept": accept, "Accept-Language": accept_language},
                timeout=30,
            )
            if isinstance(response, SourceUnavailable):
                return response
            if response.status_code in _FORMAT_RETRY_CODES:
                continue
            if response.status_code >= 400:
                return self._unavailable(f"http_{response.status_code}")
            body = response.text or ""
            if not body.strip():
                # Порожня відповідь — теж не подача документа: питаємо наступним
                # форматом, а не видаємо порожнечу за прочитане рішення.
                continue
            return body, name, attempts
        return "", "", attempts

    @typed_failures(source_id="eu_case_law_cellar")
    def fetch(self, document_id: str, **options: Any) -> AdapterResult:
        identifier = str(document_id or "").strip()
        as_ecli = is_valid_ecli(identifier)
        as_celex = is_case_law_celex(identifier)
        if not identifier or not (as_ecli or as_celex):
            return NotFound(
                identifier=identifier or "(порожній ідентифікатор)",
                id_format_hint=self.id_format_hint,
            )

        if as_ecli:
            ecli = identifier
            bindings = self._sparql_bindings(_resolve_ecli_sparql(ecli))
            if isinstance(bindings, SourceUnavailable):
                return bindings
            if not bindings:
                return NotFound(identifier=ecli, id_format_hint=self.id_format_hint)
            celex = _pick_document_celex([_binding_value(b, "celex") for b in bindings])
            if not celex:
                return NotFound(identifier=ecli, id_format_hint=self.id_format_hint)
        else:
            # CELEX просили дослівно — ``_pick_document_celex`` тут не місце: він
            # обирає рішення замість резюме, а якщо резюме й просили, це була б
            # відповідь не на те питання.
            celex = identifier
            bindings = self._sparql_bindings(_resolve_celex_sparql(celex))
            if isinstance(bindings, SourceUnavailable):
                return bindings
            if not bindings:
                return NotFound(identifier=celex, id_format_hint=self.id_format_hint)
            # ECLI у документа може не бути (супровідні записи, найдавніші акти) —
            # це не привід відмовляти в читанні, лише привід не вигадувати ECLI.
            ecli = next(
                (_binding_value(b, "ecli") for b in bindings if _binding_value(b, "ecli")), ""
            )

        # Мова рішення: Cellar видає лише 24 офіційні мови, всі рівно автентичні.
        # Непокрита мова — не переклад із застереженням, а честный not_covered.
        resolved = _resolve_language(str(options.get("language") or ""))
        if resolved is None:
            return NotCovered(
                legal_order=self.legal_order,
                manual_path=(
                    f"Мова {options.get('language')!r} не є офіційною мовою ЄС — у Cellar "
                    f"такої версії немає. Читайте однією з 24 офіційних: {_celex_url(celex)}"
                ),
                subject=identifier,
                operation="read_document",
                source_id=self.source_id,
            )
        language, accept_language = resolved

        url = _celex_url(celex)
        read = self._read_document(url, accept_language)
        if isinstance(read, SourceUnavailable):
            return read
        body, retrieved_format, attempts = read
        if not retrieved_format:
            # Ідентифікатор резолвиться, документ у Cellar є — немає лише
            # машиночитаної подачі. Сказати тут «такого ідентифікатора немає»
            # означало б видати відсутність формату за відсутність документа.
            return NotCovered(
                legal_order=self.legal_order,
                manual_path=(
                    f"Документ {identifier} у джерелі не опублікований у машиночитаному "
                    f"вигляді: жоден із {attempts} форматів "
                    f"({', '.join(name for name, _ in _DOCUMENT_FORMATS)}) не віддав тексту. "
                    f"Читайте вручну: {url}"
                ),
                subject=identifier,
                operation="read_document",
                source_id=self.source_id,
            )

        stub = self.reject_stub(body, document_id=identifier)
        if stub is not None:
            return stub

        root = _parse_xhtml(body)
        if root is None:
            return SourceUnavailable(
                source=self.source_id,
                retry_after="через 15 хвилин",
                reason="unparsable_document",
            )

        citation_base = ecli or celex
        requested = str(options.get("path") or options.get("paragraph") or "").strip()
        if requested:
            paragraph = _normalize_paragraph(requested)
            if not paragraph:
                return NotFound(
                    identifier=f"{citation_base}#{requested}",
                    id_format_hint="номер пункту рішення, наприклад 45 або «§ 45»",
                )
            unit_text = extract_judgment_paragraph(root, paragraph)
            if unit_text is None:
                return NotFound(
                    identifier=f"{citation_base}#{paragraph}",
                    id_format_hint="номер пункту в межах цього рішення",
                )
            content_kind = ContentKind.FRAGMENT
        else:
            paragraph = ""
            unit_text = _judgment_text(root)
            if not unit_text:
                return NotFound(
                    identifier=citation_base,
                    id_format_hint="ECLI або CELEX опублікованого рішення",
                )
            content_kind = ContentKind.FULL_TEXT

        # Реквізити рішення: назва справи, її номер і дата. Без них посилання
        # виглядало як «62016CJ0064, 57» — ні назви справи, ні суду, ні дати,
        # тобто не посилання, а внутрішній ідентифікатор. Відказ SPARQL тут не
        # скасовує читання: реквізити лишаються порожніми, і рендер покаже це,
        # а не підставить вигадане.
        case_meta = _case_metadata(bindings)
        court = _case_court(celex)
        kind = _case_kind(celex)
        citation_parts = [
            part
            for part in (
                kind,
                court,
                case_meta.get("judgment_date", ""),
                case_meta.get("case_name", ""),
                case_meta.get("case_number", ""),
                ecli,
                f"CELEX {celex}" if celex != ecli else "",
                f"п. {paragraph}" if paragraph else "",
            )
            if part
        ]
        provenance = ProvenanceStamp(
            source_channel=SourceChannel.LIVE,
            source_url=url,
            language=language,
            citation_format=", ".join(citation_parts) or citation_base,
            stale=False,
            is_authentic_version=True,
            is_translation=False,
            attribution=ATTRIBUTION_EU.format(year="—"),
            publication_kind=(
                PublicationKind.OFFICIAL_JOURNAL_NOTICE
                if _is_oj_notice(celex)
                else PublicationKind.COURT_PUBLICATION
            ),
            content_kind=content_kind,
            version_id=celex,
            fetched_at=dt.datetime.now(timezone.utc).isoformat(),
        )
        return AdapterPayload(
            data={
                "ecli": ecli,
                "celex": celex,
                "court": court,
                "document_type": kind,
                **case_meta,
                "resolved_document_id": identifier,
                # Скільки спроб і чим саме прочитано — частина відповіді, а не
                # службова дрібниця: за нею видно, що документ узятий не з того
                # формату, якого просили першим.
                "retrieved_format": retrieved_format,
                "fetch_attempts": attempts,
                "paragraph": paragraph,
                # Локатор доказу — адреса, якою пункт цитують, і саме за нею
                # звірка шукає прочитаний текст.
                "locator": paragraph,
                "text": unit_text,
                "url": url,
                "aliases": tuple(alias for alias in (celex, ecli) if alias and alias != identifier),
            },
            provenance=provenance,
            attribution=ATTRIBUTION_EU.format(year="—"),
        )

    @typed_failures(source_id="eu_case_law_cellar")
    def search(self, query: str, **options: Any) -> AdapterResult:
        cites = str(options.get("cites") or "").strip()
        if cites:
            return self._search_cited_by(cites, options)
        case_number = str(options.get("case_number") or "").strip()
        if case_number:
            return self._search_by_case_number(case_number, options)
        max_results = int(options.get("max_results") or 20)
        bindings = self._sparql_bindings(_search_sparql(query, limit=max_results))
        if isinstance(bindings, SourceUnavailable):
            return bindings

        results = [
            {
                "ecli": _binding_value(binding, "ecli"),
                "celex": _binding_value(binding, "celex"),
                "title": _binding_value(binding, "title"),
            }
            for binding in bindings
        ]
        provenance = ProvenanceStamp(
            source_channel=SourceChannel.LIVE,
            source_url=endpoint("eu.cellar_sparql"),
            language="en",
            citation_format="ECLI:...",
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

    def _search_by_case_number(self, case_number: str, options: dict[str, Any]) -> AdapterResult:
        """Рішення за номером справи: ``C-311/18`` → ECLI, дата, назва справи (тікет 47).

        Юрист посилається на справу номером, а ``get_decision`` приймає лише ECLI чи
        CELEX. Номер розкривається в CELEX за правилом самого Cellar
        (:func:`case_number_celex_candidates`), і джерело питається про ці точні
        CELEX — вільний текст йому не йде. Видача навігаційна: читання рішення — окремий
        ``fetch`` за CELEX чи ECLI зі знайденого.
        """
        candidates = case_number_celex_candidates(case_number)
        if candidates is None:
            return NotCovered(
                legal_order=self.legal_order,
                manual_path=(
                    "Номер справи має вигляд C-NNN/YY, T-NNN/YY або F-NNN/YY (за потреби "
                    "з суфіксом виду провадження) або CELEX судової практики; рішення за "
                    "ECLI читає get_decision. Вручну: https://curia.europa.eu/juris/liste.jsf"
                ),
                subject=f"case_number «{case_number}»",
                operation="search_by_identifier",
                source_id=self.source_id,
            )
        bindings = self._sparql_bindings(build_case_number_sparql(candidates))
        if isinstance(bindings, SourceUnavailable):
            return bindings

        max_results = max(1, min(int(options.get("max_results") or 10), 20))
        date_from = str(options.get("date_from") or "").strip()
        date_to = str(options.get("date_to") or "").strip()
        results = [
            item
            for item in (_cited_by_result_item(binding) for binding in _dedupe_cited_by(bindings))
            # Дати рішення — включно, як і в запиті цитувань; рядок без дати
            # фільтр за датою не проходить (там теж BOUND(?date)).
            if (not date_from or (item["date"] and item["date"] >= date_from))
            and (not date_to or (item["date"] and item["date"] <= date_to))
        ]
        provenance = ProvenanceStamp(
            source_channel=SourceChannel.LIVE,
            source_url=endpoint("eu.cellar_sparql"),
            language="en",
            citation_format=case_number,
            stale=False,
            is_authentic_version=True,
            is_translation=False,
            attribution=ATTRIBUTION_EU.format(year="—"),
            content_kind=ContentKind.METADATA,
            publication_kind=PublicationKind.CARD,
        )
        return AdapterPayload(
            data={
                "case_number": case_number,
                "results": results[:max_results],
                "found": len(results[:max_results]),
                "source": self.source_id,
                "retrieved_at": dt.datetime.now(timezone.utc).isoformat(),
                # Кандидатів скінченна кількість, видача не урізається LIMIT: відповідь
                # повна щодо того, про що питали (межі шляху — у notice).
                "complete": len(results) <= max_results,
                "applied_filters": {
                    "case_number": case_number,
                    "celex_candidates": list(candidates),
                    "date_from": date_from or None,
                    "date_to": date_to or None,
                },
                "notice": CASE_NUMBER_SEARCH_NOTICE,
            },
            provenance=provenance,
            attribution=ATTRIBUTION_EU.format(year="—"),
        )

    def _search_cited_by(self, celex: str, options: dict[str, Any]) -> AdapterResult:
        """«Документи, що цитують акт» (T-cites) — структурний пошук за CELEX.

        Форма ``celex`` перевірена контрактом інструмента (``contracts.py``,
        ``SearchDecisionsInput.cites``) ще до виклику адаптера — вільний текст
        сюди не доходить і джерелу не надсилається (ADR 0009). Тут лишається
        побудова запиту й чесний ``complete``: ``LIMIT max_results + 1`` — якщо
        рядків прийшло більше, ніж просили, видача урізана, і про це сказано
        прямо, а не приховано зайвим рядком.
        """
        max_results = max(1, min(int(options.get("max_results") or 10), 20))
        date_from = str(options.get("date_from") or "").strip()
        date_to = str(options.get("date_to") or "").strip()
        court = str(options.get("court") or "").strip()
        kinds = options.get("document_kinds") or CITED_BY_DEFAULT_KINDS
        query = build_cited_by_sparql(
            celex,
            date_from=date_from,
            date_to=date_to,
            court=court,
            limit=max_results + 1,
            kinds=tuple(kinds),
        )
        bindings = self._sparql_bindings(query)
        if isinstance(bindings, SourceUnavailable):
            return bindings

        # Повнота — з сирої видачі (до дедуплікації): рядок понад max_results
        # доводить, що результатів більше, навіть якщо частина сирих рядків
        # виявиться дублікатом того самого документа після дедуплікації.
        # Урізати можна тільки в бік «менше обіцяно», не навпаки (принцип III).
        complete = len(bindings) <= max_results
        deduped = _dedupe_cited_by(bindings)
        results = [_cited_by_result_item(binding) for binding in deduped[:max_results]]
        provenance = ProvenanceStamp(
            source_channel=SourceChannel.LIVE,
            source_url=endpoint("eu.cellar_sparql"),
            language="en",
            citation_format=f"cites {celex}",
            stale=False,
            is_authentic_version=True,
            is_translation=False,
            attribution=ATTRIBUTION_EU.format(year="—"),
            content_kind=ContentKind.METADATA,
            publication_kind=PublicationKind.CARD,
        )
        return AdapterPayload(
            data={
                "cites": celex,
                "results": results,
                "found": len(results),
                "source": self.source_id,
                "retrieved_at": dt.datetime.now(timezone.utc).isoformat(),
                "complete": complete,
                "applied_filters": {
                    "cites": celex,
                    "date_from": date_from or None,
                    "date_to": date_to or None,
                    "court": court or None,
                    "document_kinds": list(kinds),
                },
            },
            provenance=provenance,
            attribution=ATTRIBUTION_EU.format(year="—"),
        )

    @typed_failures(source_id="eu_case_law_cellar")
    def public_card(self, case_number: str) -> AdapterResult:
        """Публічна картка провадження за номером справи (ADR 0006, T151).

        Перший канал — Cellar SPARQL за формою CELEX судової практики
        (``6<рік><форум><тип>0*<номер>``). InfoCuria віддає лише
        Angular-оболонку й не використовується (research/13, §A6). Коли Cellar
        публікацій не має, картку читає легасі-інтерфейс CURIA
        (:class:`sources.curia_legacy.CuriaLegacyCard`); відсутність документів
        і там — чесна картка ``published=False`` з датою перевірки й ручним
        шляхом, а не відказ.

        ``content_kind=METADATA`` і ``publication_kind=CARD`` завжди — картка
        не є текстом рішення і не підтверджує цитату (:attr:`ProvenanceStamp.confirmable`)
        незалежно від того, знайдено публікацію чи ні.
        """
        case = str(case_number or "").strip()
        checked_at = dt.datetime.now(timezone.utc).isoformat()
        manual_path = (
            f"Картка провадження вручну: https://curia.europa.eu/juris/liste.jsf"
            f"?num={case}&language=en; офіційний вісник — https://eur-lex.europa.eu/oj/"
            "direct-access.html (повідомлення про справу публікується в серії C)"
        )

        match = _CASE_NUMBER_RE.match(case)
        if not match:
            provenance = ProvenanceStamp(
                source_channel=SourceChannel.LIVE,
                source_url=endpoint("eu.cellar_sparql"),
                language="en",
                citation_format="C-NNN/YY",
                stale=False,
                is_authentic_version=True,
                is_translation=False,
                attribution=ATTRIBUTION_EU.format(year="—"),
                content_kind=ContentKind.METADATA,
                publication_kind=PublicationKind.CARD,
            )
            return AdapterPayload(
                data={
                    "case_number": case or "(порожній номер справи)",
                    "court": "невідомо",
                    "published_status": "unknown_case_number_format",
                    "published": False,
                    "public_documents": [],
                    "checked_at": checked_at,
                    "checked_sources": (
                        "формат номера справи не розпізнано; очікується "
                        "C-NNN/YY, T-NNN/YY або F-NNN/YY, за потреби з суфіксом "
                        "виду провадження (P, R, DEP)",
                    ),
                    "manual_path": manual_path,
                },
                provenance=provenance,
                attribution=ATTRIBUTION_EU.format(year="—"),
            )

        forum = match.group("forum")
        year = _case_number_year(match.group("yy"))
        num_padded = match.group("num").zfill(4)
        pattern = f"^6{year}{forum}[A-Z]0*{num_padded}$"
        sparql = (
            "PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>\n"
            "SELECT ?celex ?date ?kind WHERE {\n"
            "  ?w cdm:resource_legal_id_celex ?celex .\n"
            f'  FILTER(REGEX(STR(?celex), "{pattern}"))\n'
            "  OPTIONAL { ?w cdm:work_date_document ?date }\n"
            "  OPTIONAL { ?w cdm:resource_legal_type ?kind }\n"
            "}\n"
            "ORDER BY ?celex"
        )
        checked_sources: tuple[str, ...] = (
            f"Cellar SPARQL, resource_legal_id_celex REGEX {pattern}",
        )
        bindings = self._sparql_bindings(sparql)
        if isinstance(bindings, SourceUnavailable):
            # Недоступний Cellar — саме той збій, від якого рятує другий канал
            # (рев'ю 15.09.2026): картку читає CURIA, а відповідь називає, що
            # Cellar не відповів. Лише коли лежать обидва — типізована відмова.
            checked_sources = (
                f"Cellar SPARQL недоступний ({bindings.reason}); публікації там не перевірено",
            )
            bindings = []

        documents: list[dict[str, Any]] = []
        seen: set[str] = set()
        for binding in bindings:
            celex = _binding_value(binding, "celex")
            if not celex or celex in seen:
                continue
            seen.add(celex)
            kind_code = _binding_value(binding, "kind")
            kind_letter = kind_code[-1] if kind_code else ""
            documents.append(
                {
                    "celex": celex,
                    "kind": _KIND_NAMES.get(kind_letter) or (kind_code or "невідомо"),
                    "date": _binding_value(binding, "date") or None,
                    "url": f"{endpoint('eu.cellar_celex')}/{celex}",
                }
            )
        documents.sort(key=lambda item: (item.get("date") or "", item["celex"]))

        court = _FORUM_NAMES.get(forum, forum)
        published = bool(documents)
        data: dict[str, Any] = {
            "case_number": case,
            # Суфікс на CELEX не впливає, але з номера не зникає: «C-50/00 P» і
            # «C-50/00» — різні провадження в одній справі, і картка мусить
            # показати, яке з них просили.
            "case_suffix": match.group("suffix") or "",
            "court": court,
            "published_status": "published" if published else "not_published",
            "published": published,
            "public_documents": documents,
            "checked_at": checked_at,
            "checked_sources": checked_sources,
            "manual_path": manual_path,
            "channel": "cellar_sparql",
        }

        if not published:
            # Другий канал (15.09.2026): Cellar знає справу лише після першої
            # публікації, а відкрите провадження без неї давало голе
            # «not_published». Легасі-картка CURIA називає дату подання, суд,
            # предмет, мову й документи (sources/curia_legacy.py). Її
            # недоступність — типізована відмова з ручним шляхом, а не
            # «не опубліковано», яке виглядало б перевіреним.
            curia = self._curia.read(case)
            if isinstance(curia, SourceUnavailable):
                return curia
            data.update(curia)
            data["checked_sources"] = (
                *checked_sources,
                "CURIA (легасі-інтерфейс juris.curia.europa.eu): liste.jsf, fiche.jsf, "
                "documents.jsf — сторінка без контракту",
            )
            published = bool(curia.get("public_documents"))
            data["published"] = published
            # «not_published» для зареєстрованої справи без документів вводив в
            # оману (рев'ю 15.09.2026): статус розрізняє справу, якої CURIA не
            # знає, і справу, відкриту без публікацій.
            if published:
                data["published_status"] = "published"
            elif curia.get("case_found"):
                data["published_status"] = "registered_no_documents"
            else:
                data["published_status"] = "case_not_found"

        provenance = ProvenanceStamp(
            source_channel=SourceChannel.LIVE,
            source_url=str(data.get("source_url") or endpoint("eu.cellar_sparql")),
            language="en",
            citation_format=case,
            stale=False,
            is_authentic_version=True,
            is_translation=False,
            attribution=ATTRIBUTION_EU.format(year="—"),
            content_kind=ContentKind.METADATA,
            publication_kind=PublicationKind.CARD,
        )
        return AdapterPayload(
            data=data,
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


register_adapter(EuCaseLawAdapter())
