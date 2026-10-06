"""Адаптер практики ЄСПЛ поверх неофіційного JSON-інтерфейсу HUDOC (T063–T065, T146).

**Джерело без гарантій.** Офіційного API HUDOC не підтверджено (research/02); фактичний
шлях — JSON через URL-запити до пошукового ендпойнта, за яким стоять неофіційні
скрапери (`echr-extractor` та подібні), а не документований контракт. Такий інтерфейс
може зникнути без попередження — аналогічний профільний сервер за однією з цільових
юрисдикцій уже переведено в архівний режим 07.07.2026 (`research.md`, розділ ризиків
спеки). Тому зникнення інтерфейсу тут ніколи не є винятком: воно завжди перетворюється
на типізований відказ ``source_unavailable`` із посиланням на ручний шлях —
`https://hudoc.echr.coe.int` для пошуку вручну за номером справи або ECLI.

**Живі проби 2026-09-07** (research/13-runtime-probes-2026-09-07.md, розділ D) виправили
дві реальні поломки цього модуля:

1. ``/app/query/results`` без параметра ``sort`` (навіть порожнього) відповідає
   статичною 404-сторінкою. Тепер ``sort=""`` іде в кожному запиті.
2. ``contains(itemid, "...")``/``contains(ecli, "...")`` — це неточний збіг:
   ``contains(itemid,"001-114082")`` повертав 4935 результатів із чужою справою
   першою. Точний синтаксис — ``(itemid="...")``/``(ecli="...")``. **Колонка
   ``content`` не повертається** query API навіть при явному запиті — тому
   підстановка ``conclusion``/``docname`` замість тексту рішення (яку робив
   попередній ``fetch``) була брехнею, а не скороченням: за резюме видавали
   рішення. Повний текст читається лише через конвертацію PDF
   (``/app/conversion/pdf/`` з фолбеком на ``/app/conversion/docx/pdf`` для
   старих документів) і розбирається ``pypdf``.

**Мовчазна підміна видачі — головна пастка цього інтерфейсу (T215).** Живі проби
2026-09-08 показали: на клаузу, якої HUDOC
не розуміє, він відповідає ``200 OK`` і ДОБІРКОЮ ЗА ЗАМОВЧУВАННЯМ — не помилкою і
не порожнечею. ``contains(appno,"14038/88")`` на п'яти різних номерах заяв віддавав
ті самі десять чужих документів (першим завжди ÜLGER v. TURKEY), а два різні
запити ``contains(docname,…)`` — ту саму десятку між собою. Звідси два наслідки,
і другий важливіший за перший:

- Номер заяви шукається лише формою ``appno:"NNNNN/YY"`` — вона фільтрує
  (``appno:"14038/88"`` → Soering v. the United Kingdom). Номер іде голим:
  суфікс «+» (``55508/07+``), склейка через «;» і форма без слеша дають нуль
  записів, тому нормалізуються до голого номера перед відправкою.
- Сама форма запиту нічого не гарантує: будь-яка майбутня клауза, якої джерело
  не зрозуміє, знову дасть підміну. Тому пошук ЗА ІДЕНТИФІКАТОРОМ звіряє видачу
  з запитаним ідентифікатором (``results_contain_identifier``) і за відсутності
  збігу віддає відказ, а не документи: результат, що не стосується запиту, гірший
  за порожній (принцип III). ``appno`` в об'єднаних справах приходить склейкою
  («31253/96;14038/88;37112/97»), тому звірка розбирає його на частини.

**Локатор абзацу.** ``path`` (``§ 78``, ``78``, ``para 78``) шукається як початок
рядка ``78.`` у витягнутому з PDF тексті. Наївний пошук першого збігу ненадійний:
довгі рішення містять вкладені переліки (перелік заявників у додатку, перелік
статей конвенції), які теж нумеруються з ``1.`` і псують результат (перевірено
живцем на 001-114082 — «78.» трапляється і в § 78 постанови, і в переліку
заявників додатку). Тому нумерація абзаців рахується послідовно: наступний
абзац приймається, лише якщо його номер точно на одиницю більший за
попередній прийнятий — вкладені переліки, що починаються знову з «1.», це
природно відсікає. Межа тексту рішення — перший рядок ЦІЛКОМ великими
літерами зі словом «OPINION» і одним із маркерів (DISSENTING/CONCURRING/
SEPARATE та їх французькі відповідники): усе після цієї межі — додані окремі
думки суддів, вони не входять до тексту рішення і окремо не витягуються.

**Мова.** ECLI не унікальний за мовою: у HUDOC один ECLI ділять оригінал і всі
неофіційні переклади (перевірено живцем — запит по ECLI справи Catan повернув
28 записів різними мовами). Автентичні мови Суду — англійська й французька
(ст. 34 Регламенту Суду); усе інше — переклад. Якщо запитано мову, якої немає
серед знайдених записів, — ``not_found`` з переліком наявних мов, а не мовчазна
підміна перекладом (принцип V).

**Контрактне правило (FR-019, принцип VI).** Рішення міжнародного форуму обов'язкове
лише для сторін спору і в межах цієї справи. Кожен успішний ``fetch`` несе поле
``binding_scope`` та текст ``binding_notice``, який прямо це каже — подати рішення
міжнародного суду як обов'язковий прецедент є порушенням межі продукту, а не
стилістичною деталлю.

**Фільтр за датою.** Підтримка ``kpdate`` як фільтра пошуку в цьому зрізі не
доведена живим запитом, тому ``supports_date_filter = False``: ``date_from``/
``date_to`` дають ``unsupported_filter``, а не мовчазне ігнорування (ADR 0008).
"""

from __future__ import annotations

import datetime as dt
import io
import logging
import re
from typing import Any

import requests

from core.contracts import NotFound, SourceHealth, SourcePolicy, SourceUnavailable, TypedFailure
from core.legal_orders import CoverageLayer, Source, SourceAccess, SourceLimits
from core.provenance import ContentKind, ProvenanceStamp, PublicationKind, SourceChannel
from core.source_endpoints import endpoint
from sources import register_adapter
from sources.base import AdapterPayload, AdapterResult, RegistryAdapter, typed_failures
from sources.transport import SourceTransport

logger = logging.getLogger("ukraine-laws")

__all__ = [
    "EchrAdapter",
    "echr_adapter",
    "identifier_kind",
    "results_contain_identifier",
]

#: Ручний шлях, який юрист отримує замість автоматичного результату — і в
#: source_unavailable, і в not_found (формат ідентифікатора там теж пояснюється тут).
MANUAL_PATH = (
    "https://hudoc.echr.coe.int — ручний пошук за номером справи, ECLI або назвою "
    "сторін; офіційного API немає, інтерфейс може змінитися без попередження"
)

#: Обов'язкове поле контракту get_decision для міжнародного форуму (FR-019).
BINDING_NOTICE = (
    "Рішення Європейського суду з прав людини є обов'язковим лише для сторін спору і "
    "в межах цієї справи (ст. 46 Конвенції); за межами сторін і справи воно не є "
    "обов'язковим прецедентом."
)

ID_FORMAT_HINT = (
    "ECLI (напр. ECLI:CE:ECHR:2018:1109JUD007140910) або номер справи HUDOC " "(напр. 001-187186)"
)

_SEARCH_PATH = "/app/query/results"
_DEFAULT_TIMEOUT = 15
_PDF_TIMEOUT = 30
_RETRY_AFTER = "через 15 хвилин"

_SELECT_FIELDS = (
    "itemid,ecli,appno,docname,kpdate,conclusion,languageisocode,"
    "doctype,doctypebranch,documentcollectionid2"
)

_ITEMID_RE = re.compile(r"^\d{3}-\d+$")
_ECLI_RE = re.compile(r"^ECLI:[A-Za-z]{2}:[A-Za-z0-9]+:\d{4}:[A-Za-z0-9.]+$")
#: Номер жалобы ЄСПЛ: «12345/06», иногда с суффиксом «12345/06+».
_APPNO_RE = re.compile(r"^\d{1,6}/\d{2,4}\+?$")


def _citation_format(hit: dict[str, Any], *, locator: str = "") -> str:
    """Форма ссылки на **прочитанное** решение, а не пример из другого дела.

    Здесь стоял литерал ``ECLI:CE:ECHR:2018:1109JUD007140910, § 78`` — реальный
    ECLI дела Hodžić v. Croatia, зашитый в шаблон. Для любого другого решения конверт
    происхождения называл, таким образом, чужое решение: юрист, скопировавший
    ``citation_format``, подал бы в суд ссылку на дело, которого не читал.

    Собирается из того, что HUDOC отдал по этому документу. Чего источник не
    дал — того в строке нет; ECLI чужого дела не подставляется никогда.
    """
    ecli = str(hit.get("ecli") or "").strip()
    appno = str(hit.get("appno") or "").strip()
    name = str(hit.get("docname") or "").strip()
    itemid = str(hit.get("itemid") or "").strip()
    parts = [part for part in (name, f"заява № {appno}" if appno else "", ecli) if part]
    if not parts and itemid:
        parts = [f"HUDOC {itemid}"]
    reference = ", ".join(parts)
    tail = str(locator or "").strip()
    if tail:
        reference = f"{reference}, § {tail.lstrip('§ ')}" if reference else f"§ {tail.lstrip('§ ')}"
    return reference or "HUDOC (реквізитів джерело не віддало)"


#: Разделитель номеров в объединённом деле: HUDOC отдаёт колонку `appno` склейкой
#: «31253/96;14038/88;37112/97» (живая проба на 14038/88 и 55508/07). Запятая и
#: пробел добавлены как наблюдаемые вариации той же склейки, а не как догадка
#: о синтаксисе: разбор здесь только читает ответ, наружу ничего не уходит.
_APPNO_SPLIT_RE = re.compile(r"[;,\s]+")
#: Что вообще допустимо отправить HUDOC как номер дела (T182): ECLI, itemid
#: или номер жалобы. Всё прочее HUDOC превращает в поиск подстроки по
#: ``docname``, то есть в отправку свободного текста наружу.
_CASE_NUMBER_RE = re.compile(
    "|".join(f"(?:{pattern.pattern})" for pattern in (_ECLI_RE, _ITEMID_RE, _APPNO_RE))
)

#: Мови, автентичні для Суду (ст. 34 Регламенту Суду) — все інше є перекладом.
_AUTHENTIC_LANGS_3 = frozenset({"ENG", "FRA"})

_LANG_3_TO_2 = {
    "ENG": "en",
    "FRA": "fr",
    "RUS": "ru",
    "UKR": "uk",
    "GER": "de",
    "ITA": "it",
    "SPA": "es",
    "TUR": "tr",
    "POL": "pl",
    "ROM": "ro",
    "LAV": "lv",
    "LIT": "lt",
}
_LANG_2_TO_3 = {v: k for k, v in _LANG_3_TO_2.items()}

_DOCTYPE_LABELS = {
    "HEJUD": "judgment",
    "HEDEC": "decision",
    "HECOM": "communicated_case",
}
_BRANCH_LABELS = {
    "GRANDCHAMBER": "grand_chamber",
    "CHAMBER": "chamber",
    "COMMITTEE": "committee",
}

#: Маркери заголовку доданих окремих думок — уся всесвітньовідома множина, яку
#: реально спостережено (research/13): англійська й французька термінологія.
#: Заголовок розпізнається лише тоді, коли рядок ЦІЛКОМ великими літерами —
#: інакше згадка «Dissenting Opinion of Judge X» в тексті постанови (посилання
#: на іншу справу) хибно обривала б текст рішення (перевірено на 001-114082).
_OPINION_MARKERS = (
    "DISSENTING",
    "CONCURRING",
    "SEPARATE",
    "DISSIDENTE",
    "CONCORDANTE",
    "SÉPARÉE",
    "SEPAREE",
)

_PARAGRAPH_START_RE = re.compile(r"^\s*(\d+)\.\s+\S")
_PATH_NUMBER_RE = re.compile(r"(\d+)")


class EchrAdapter(RegistryAdapter):
    """Пошук і читання практики ЄСПЛ через HUDOC."""

    source_id = "echr_hudoc"
    id_format_hint = ID_FORMAT_HINT
    legal_order = "ECHR"
    source_policy = SourcePolicy.RESTRICTED
    #: kpdate як фільтр пошуку живим запитом не підтверджено (research/13) —
    #: мовчазне ігнорування дало б хибну повноту (ADR 0008).
    supports_date_filter = False
    case_number_pattern = _CASE_NUMBER_RE

    def __init__(self, *, session: Any = None) -> None:
        #: Мережа адаптера — лише через спільний транспорт: скинуте після
        #: простою пулове з'єднання інакше стає хибним ``source_unavailable``
        #: (живий збій 15.09.2026, :mod:`sources.transport`).
        self._transport = SourceTransport(session or requests.Session())

    # -- об'явлення політики -------------------------------------------

    def policy(self) -> Source:
        return Source(
            id=self.source_id,
            legal_order=self.legal_order,
            layer=CoverageLayer.REACHABLE_BY_ID,
            access=SourceAccess.UNOFFICIAL_INTERFACE,
            license="статус не з'ясовано — офіційної ліцензії HUDOC не підтверджено",
            license_forbids_commercial=False,
            attribution_required=True,
            attribution="© Council of Europe / European Court of Human Rights",
            limits=SourceLimits(
                technical_restrictions=(
                    "офіційного API не підтверджено; де факто JSON через URL-запити, "
                    "без документованого контракту й гарантії доступності; текст рішення "
                    "доступний лише через конвертацію PDF"
                ),
                notes=(
                    "неофіційний інтерфейс може зникнути без попередження — аналогічний "
                    "профільний сервер вже переведено в архівний режим 07.07.2026"
                ),
            ),
            manual_path=MANUAL_PATH,
            source_url=endpoint("echr.hudoc"),
            description="Практика ЄСПЛ через неофіційний JSON-інтерфейс HUDOC",
        )

    # -- робота з джерелом ------------------------------------------------

    @typed_failures(source_id="echr_hudoc")
    def search(self, query: str, **options: Any) -> AdapterResult:
        max_results = int(options.get("max_results") or 10)
        case_number = str(options.get("case_number") or "").strip()

        # Голка одна на клаузу і на звірку: якби її рахували двічі, звіряли б не
        # той ідентифікатор, за яким фільтрували, — це гірше за відсутність звірки.
        needle = _search_needle(query=query, case_number=case_number)
        clause = _search_clause(needle)
        outcome = self._query_hudoc(clause, length=max_results)
        if not isinstance(outcome, list):
            return outcome  # typed failure

        if identifier_kind(needle) is not None:
            if not outcome:
                # Порожньо на точний фільтр — це чесне «немає такого», а не збій:
                # `appno:"99999/99"` живцем повертає нуль записів.
                return NotFound(identifier=needle, id_format_hint=ID_FORMAT_HINT)
            if not results_contain_identifier(outcome, needle):
                return _filter_not_applied(self.source_id, needle)

        results = [self._summarize(hit) for hit in outcome]
        provenance = self._provenance(source_url=f"{endpoint('echr.hudoc')}{_SEARCH_PATH}")
        return AdapterPayload(
            data={
                "query": query,
                "case_number": case_number or None,
                "results": results,
                "found": len(results),
                "source": self.source_id,
                "retrieved_at": _now_iso(),
                # T232: що з фільтрів справді дійшло до HUDOC. Голка йде в
                # клаузу пошуку, дати HUDOC як фільтр не підтверджені живою
                # пробою (``supports_date_filter = False``), вільний текст у
                # профілі legal сюди не доходить узагалі.
                "applied_filters": {
                    "case_number": needle or None,
                    "search_clause": clause,
                    "date_from": None,
                    "date_to": None,
                    "free_text": False,
                },
            },
            provenance=provenance,
            attribution=self.policy().attribution,
        )

    @typed_failures(source_id="echr_hudoc")
    def fetch(self, document_id: str, **options: Any) -> AdapterResult:
        identifier = str(document_id or "").strip()
        if not identifier:
            return NotFound(identifier=identifier, id_format_hint=ID_FORMAT_HINT)

        kind = identifier_kind(identifier)
        if kind not in ("ecli", "itemid"):
            return NotFound(identifier=identifier, id_format_hint=ID_FORMAT_HINT)
        is_ecli = kind == "ecli"
        # ECLI зводиться до канонічного верхнього регістру: `_ECLI_RE` пропускає
        # змішаний регістр, а HUDOC на «ecli=…jud001403888» віддає нуль записів —
        # тобто не знайшов би реального рішення (жива проба). Адаптер обслуговує
        # лише ЄСПЛ, де всі спостережені ECLI канонічно великими літерами.
        identifier = identifier.upper() if is_ecli else identifier

        clause = _search_clause(identifier)
        outcome = self._query_hudoc(clause, length=50 if is_ecli else 1)
        if not isinstance(outcome, list):
            return outcome  # typed failure — вже source_unavailable
        if not outcome:
            return NotFound(identifier=identifier, id_format_hint=ID_FORMAT_HINT)
        # Та сама пастка, що й у пошуку: відповідь без запитаного ідентифікатора —
        # це чужі документи, а не це рішення; віддавати їх за нього не можна.
        if not results_contain_identifier(outcome, identifier):
            return _filter_not_applied(self.source_id, identifier)

        requested_language = str(options.get("language") or "").strip().lower()
        hit = _pick_language_hit(outcome, requested_language)
        if hit is None:
            available = sorted(
                {str(h.get("languageisocode") or "") for h in outcome if h.get("languageisocode")}
            )
            return NotFound(
                identifier=identifier,
                id_format_hint=ID_FORMAT_HINT,
                message=(
                    f"Мовна версія «{requested_language}» рішення {identifier} у HUDOC "
                    f"відсутня; наявні мови: {', '.join(available) or 'невідомо'}. Переклад "
                    "не підставляється замість оригіналу."
                ),
            )

        itemid = str(hit.get("itemid") or "").strip()
        if not itemid:
            return NotFound(identifier=identifier, id_format_hint=ID_FORMAT_HINT)

        pdf_bytes, pdf_url, pdf_failure = self._fetch_pdf(itemid)
        if pdf_failure is not None:
            return pdf_failure
        assert pdf_bytes is not None

        try:
            full_text = _extract_pdf_text(pdf_bytes)
        except Exception as error:  # noqa: BLE001 — межа розбору PDF, не адаптера
            logger.warning("echr.hudoc pdf parse failed for %s: %s", itemid, type(error).__name__)
            return SourceUnavailable(
                source=self.source_id,
                retry_after=_RETRY_AFTER,
                message=(
                    f"PDF рішення {itemid} отримано, але pypdf не зміг його розібрати. "
                    f"Ручний шлях: {MANUAL_PATH}"
                ),
            )

        stub = self.reject_stub(
            full_text[:4000],
            document_id=identifier,
            extra_markers=("just a moment", "checking your browser"),
        )
        if stub is not None:
            return stub

        lines = full_text.splitlines()
        boundary = _judgment_boundary(lines)
        starts = _paragraph_index(lines, boundary)
        has_opinions = boundary < len(lines)

        requested_path = str(options.get("path") or "").strip()
        paragraph_number: str | None = None
        if requested_path:
            match = _PATH_NUMBER_RE.search(requested_path)
            if not match:
                return NotFound(
                    identifier=identifier,
                    id_format_hint="номер абзацу рішення, напр. «§ 78», «78», «para 78»",
                )
            paragraph_number = match.group(1)

        if paragraph_number is not None:
            n = int(paragraph_number)
            text = _paragraph_text(lines, starts, boundary, n)
            if text is None:
                last = max(starts) if starts else 0
                return NotFound(
                    identifier=identifier,
                    id_format_hint=f"абзац {paragraph_number} не знайдено; у рішенні {last} абзаців",
                )
            content_kind = ContentKind.FRAGMENT
            locator = f"§{paragraph_number}"
        else:
            text = "\n".join(line.strip() for line in lines[:boundary] if line.strip())
            text = re.sub(r"[ \t]+", " ", text).strip()
            content_kind = ContentKind.FULL_TEXT
            locator = ""

        language3 = str(hit.get("languageisocode") or "").upper()
        language2 = _LANG_3_TO_2.get(language3, language3.lower() or "en")
        is_authentic = language3 in _AUTHENTIC_LANGS_3
        doctype = str(hit.get("doctype") or "")
        branch = str(hit.get("doctypebranch") or "")

        provenance = ProvenanceStamp(
            source_channel=SourceChannel.LIVE,
            source_url=pdf_url,
            language=language2,
            citation_format=_citation_format(hit, locator=locator),
            stale=False,
            is_authentic_version=is_authentic,
            is_translation=not is_authentic,
            translation_outdated=None if is_authentic else False,
            publication_kind=PublicationKind.COURT_PUBLICATION,
            content_kind=content_kind,
            version_id=itemid,
            attribution=self.policy().attribution,
        )

        payload: dict[str, Any] = {
            "decision_id": identifier,
            "resolved_document_id": itemid,
            "itemid": itemid,
            "ecli": hit.get("ecli", ""),
            "appno": hit.get("appno", ""),
            "case_name": hit.get("docname", ""),
            "judgment_date": hit.get("kpdate", ""),
            "document_type": _DOCTYPE_LABELS.get(doctype, doctype.lower() or "unknown"),
            "chamber": _BRANCH_LABELS.get(branch, branch.lower() or None),
            "language": language2,
            "url": pdf_url,
            "locator": locator,
            "text": text,
            "char_count": len(text),
            # Резюме — окремим полем із явною позначкою, що це не текст (T146).
            "conclusion_summary": hit.get("conclusion") or None,
            "has_separate_opinions": has_opinions,
            "from_cache": False,
            "source": self.source_id,
            "retrieved_at": _now_iso(),
            # FR-019 / принцип VI — обов'язкове поле, а не примітка збоку.
            "binding_scope": "parties_and_case_only",
            "binding_notice": BINDING_NOTICE,
        }
        if has_opinions and paragraph_number is None:
            payload["separate_opinions_note"] = (
                "PDF містить додані окремі думки суддів після тексту рішення; вище наведено "
                "лише текст рішення, окремі думки цим адаптером не витягуються і за рішення "
                "не видаються."
            )
        return AdapterPayload(
            data=payload, provenance=provenance, attribution=self.policy().attribution
        )

    def health(self) -> SourceHealth:
        try:
            response = self._transport.get(
                f"{endpoint('echr.hudoc')}{_SEARCH_PATH}",
                params={
                    "query": '(itemid="001-114082")',
                    "select": "itemid",
                    "sort": "",
                    "start": 0,
                    "length": 1,
                },
                timeout=_DEFAULT_TIMEOUT,
                headers=_headers(),
            )
        except requests.RequestException as error:
            return SourceHealth(
                adapter=self.source_id,
                ok=False,
                source_policy=self.source_policy,
                details={"error": type(error).__name__},
            )
        ok = response.status_code == 200
        return SourceHealth(
            adapter=self.source_id,
            ok=ok,
            source_policy=self.source_policy,
            details={"status_code": response.status_code},
        )

    # -- внутрішнє ----------------------------------------------------------

    def _query_hudoc(self, clause: str, *, length: int) -> list[dict[str, Any]] | SourceUnavailable:
        try:
            response = self._transport.get(
                f"{endpoint('echr.hudoc')}{_SEARCH_PATH}",
                params={
                    "query": clause,
                    "select": _SELECT_FIELDS,
                    "sort": "",
                    "start": 0,
                    "length": max(1, min(int(length or 1), 50)),
                },
                timeout=_DEFAULT_TIMEOUT,
                headers=_headers(),
            )
        except requests.RequestException as error:
            logger.warning("echr.hudoc unreachable: %s", type(error).__name__)
            return SourceUnavailable(
                source=self.source_id,
                retry_after=_RETRY_AFTER,
                message=(
                    "HUDOC тимчасово недоступний або неофіційний інтерфейс змінився. "
                    f"Розумна наступна спроба: {_RETRY_AFTER}. Ручний шлях: {MANUAL_PATH}"
                ),
            )

        if response.status_code != 200:
            return SourceUnavailable(
                source=self.source_id,
                retry_after=_RETRY_AFTER,
                message=(
                    f"HUDOC відповів кодом {response.status_code}, придатної копії немає. "
                    f"Розумна наступна спроба: {_RETRY_AFTER}. Ручний шлях: {MANUAL_PATH}"
                ),
            )

        try:
            body = response.json()
        except ValueError:
            return SourceUnavailable(
                source=self.source_id,
                retry_after=_RETRY_AFTER,
                message=(
                    "HUDOC повернув відповідь, яку не вдалося розібрати як JSON — "
                    "неофіційний інтерфейс, ймовірно, змінив форму. "
                    f"Ручний шлях: {MANUAL_PATH}"
                ),
            )

        hits = body.get("results") if isinstance(body, dict) else body
        if not isinstance(hits, list):
            return []
        normalized: list[dict[str, Any]] = []
        for entry in hits:
            if not isinstance(entry, dict):
                continue
            columns = entry.get("columns")
            normalized.append(columns if isinstance(columns, dict) else entry)
        return normalized

    def _fetch_pdf(self, itemid: str) -> tuple[bytes | None, str, TypedFailure | None]:
        """Отримати PDF рішення: пряма конвертація, потім фолбек docx→pdf.

        Фолбек потрібен для старих документів (перевірено живцем на 001-63769) —
        обидва методи повертають той самий байтовий вміст, коли працює прямий шлях,
        тож спроба фолбеку після успіху першого не робиться.
        """
        base = endpoint("echr.hudoc")
        attempts = (
            (
                f"{base}/app/conversion/pdf/",
                {"filename": f"{itemid}.pdf", "id": itemid, "library": "ECHR"},
            ),
            (
                f"{base}/app/conversion/docx/pdf",
                {"filename": "CEDH.pdf", "id": itemid, "library": "ECHR"},
            ),
        )
        last_status: int | None = None
        last_url = attempts[0][0]
        for url, params in attempts:
            last_url = url
            try:
                response = self._transport.get(
                    url, params=params, timeout=_PDF_TIMEOUT, headers=_pdf_headers()
                )
            except requests.RequestException as error:
                logger.warning("echr.hudoc pdf fetch failed: %s", type(error).__name__)
                return (
                    None,
                    url,
                    SourceUnavailable(
                        source=self.source_id,
                        retry_after=_RETRY_AFTER,
                        message=(
                            f"HUDOC не віддав PDF рішення {itemid} (мережева помилка). "
                            f"Розумна наступна спроба: {_RETRY_AFTER}. Ручний шлях: {MANUAL_PATH}"
                        ),
                    ),
                )
            last_status = response.status_code
            content = bytes(getattr(response, "content", b"") or b"")
            if response.status_code == 200 and content[:5] == b"%PDF-":
                return content, url, None

        return (
            None,
            last_url,
            SourceUnavailable(
                source=self.source_id,
                retry_after=_RETRY_AFTER,
                message=(
                    f"HUDOC не віддав PDF рішення {itemid} (останній статус {last_status}); "
                    f"жоден зі шляхів конвертації не спрацював. Розумна наступна спроба: "
                    f"{_RETRY_AFTER}. Ручний шлях: {MANUAL_PATH}"
                ),
            ),
        )

    def _summarize(self, hit: dict[str, Any]) -> dict[str, Any]:
        language3 = str(hit.get("languageisocode") or "").upper()
        doctype = str(hit.get("doctype") or "")
        branch = str(hit.get("doctypebranch") or "")
        return {
            "itemid": hit.get("itemid", ""),
            "ecli": hit.get("ecli", ""),
            "appno": hit.get("appno", ""),
            "case_name": hit.get("docname", ""),
            "judgment_date": hit.get("kpdate", ""),
            "language": _LANG_3_TO_2.get(language3, language3.lower() or None),
            "document_type": _DOCTYPE_LABELS.get(doctype, doctype.lower() or "unknown"),
            "chamber": _BRANCH_LABELS.get(branch, branch.lower() or None),
        }

    def _provenance(self, *, source_url: str) -> ProvenanceStamp:
        """Конверт выдачи поиска. Здесь документ не один, и ECLI у конверта нет.

        Стоявший тут литерал ``ECLI:CE:ECHR:2018:1109JUD007140910`` — реальный
        ECLI дела Hodžić v. Croatia: любая выдача поиска называла его, чем бы
        ни искали. Форма ссылки описывается формой, а не чужим делом; ECLI
        каждой найденной карточки лежит в самой карточке.
        """
        return ProvenanceStamp(
            source_channel=SourceChannel.LIVE,
            source_url=source_url,
            language="en",
            citation_format="<назва справи>, заява № <номер>, <ECLI>, § <абзац>",
            stale=False,
            is_authentic_version=True,
            is_translation=False,
            publication_kind=PublicationKind.CARD,
            content_kind=ContentKind.METADATA,
            attribution=self.policy().attribution,
        )


def identifier_kind(value: str) -> str | None:
    """Якою формою ідентифікатора HUDOC є рядок: ``ecli``, ``itemid``, ``appno``.

    ``None`` — форма не впізнана, тобто звіряти видачу нема з чим; такий рядок
    для HUDOC є вільним текстом, а не номером.
    """
    needle = str(value or "").strip()
    if _ECLI_RE.match(needle):
        return "ecli"
    if _ITEMID_RE.match(needle):
        return "itemid"
    if _APPNO_RE.match(needle):
        return "appno"
    return None


def results_contain_identifier(hits: list[dict[str, Any]], identifier: str) -> bool:
    """Чи є у видачі HUDOC хоч один запис саме із запитаним ідентифікатором.

    Це не оптимізація і не перестраховка, а єдиний спосіб відрізнити застосований
    фільтр від незастосованого: HUDOC на незрозумілу клаузу відповідає ``200 OK`` і
    добіркою за замовчуванням (перевірено живцем — ``contains(appno,…)`` на п'яти
    різних номерах заяв дав ті самі десять чужих документів). Отже сам факт
    відповіді не доводить нічого; сходиться лише звірка ідентифікатора.

    Номер заяви звіряється з розібраною склейкою: в об'єднаній справі HUDOC віддає
    ``appno`` як «31253/96;14038/88;37112/97», і запитаний номер там усередині.
    Невпізнана форма ідентифікатора дає ``False`` — звірити нема з чим, а видавати
    непідтверджену видачу за відповідь на запит не можна (принцип III).
    """
    kind = identifier_kind(identifier)
    if kind == "appno":
        wanted = _normalize_appno(identifier)
        return any(wanted in _appnos_of(hit) for hit in hits)
    if kind == "ecli":
        wanted = identifier.strip().upper()
        return any(str(hit.get("ecli") or "").strip().upper() == wanted for hit in hits)
    if kind == "itemid":
        wanted = identifier.strip().lower()
        return any(str(hit.get("itemid") or "").strip().lower() == wanted for hit in hits)
    return False


def _appnos_of(hit: dict[str, Any]) -> set[str]:
    """Номери заяв одного запису: об'єднана справа несе їх кілька в одному полі."""
    raw = str(hit.get("appno") or "")
    return {_normalize_appno(part) for part in _APPNO_SPLIT_RE.split(raw) if part.strip()}


def _normalize_appno(value: str) -> str:
    """«55508/07+» → «55508/07»: HUDOC розуміє лише голий номер.

    Жива проба: ``appno:"55508/07+"``, ``appno:"1403888"`` і
    ``appno:"55508/07;29520/09"`` віддають нуль записів, тоді як
    ``appno:"55508/07"`` знаходить Janowiec and Others v. Russia. Тобто суфікс,
    склейка й форма без слеша — не інший синтаксис, а мовчазна порожнеча.
    """
    return value.strip().rstrip("+").strip()


def _search_needle(*, query: str, case_number: str) -> str:
    """Рядок, за яким справді йде запит: номер справи має перевагу над запитом."""
    return case_number.strip() or query.strip()


def _search_clause(needle: str) -> str:
    """Клауза HUDOC під форму запиту.

    Для номера заяви єдина форма, яку HUDOC реально застосовує, — ``appno:"…"``
    (жива проба 2026-09-08: ``appno:"14038/88"`` → Soering v. the United Kingdom,
    тоді як ``contains(appno,"14038/88")`` віддавав чужу добірку за замовчуванням).

    Запасний варіант із ``contains(docname,…)`` залишено як був, але він НЕ фільтрує:
    живцем два різні запити ``contains(docname,…)`` повернули ту саму десятку
    сторонніх записів. Ця гілка закрита не тут, а на рівні
    можливості: операція ``search_free_text`` для джерела оголошена непокритою, і
    вільний текст у профілі legal назовні не йде (ADR 0009).
    """
    kind = identifier_kind(needle)
    if kind == "ecli":
        return f'(ecli="{needle.strip().upper()}")'
    if kind == "itemid":
        return f'(itemid="{needle.strip()}")'
    if kind == "appno":
        return f'appno:"{_normalize_appno(needle)}"'
    escaped = needle.replace('"', '\\"')
    return f'contains(docname,"{escaped}") or contains(appno,"{escaped}")'


def _filter_not_applied(source_id: str, identifier: str) -> SourceUnavailable:
    """Відказ замість видачі, у якій запитаного ідентифікатора немає.

    Це відмова про стан інтерфейсу, а не про документ: ми не знаємо, чи існує
    документ, — ми знаємо, що джерело нас не зрозуміло й підмінило відповідь.
    Тому ``source_unavailable`` з ручним шляхом, а не ``not_found``.
    """
    return SourceUnavailable(
        source=source_id,
        retry_after=_RETRY_AFTER,
        message=(
            "Джерело відповіло, але у видачі немає запитаного ідентифікатора "
            f"«{identifier}»: фільтр не застосовано — HUDOC на незрозумілий запит "
            "віддає добірку за замовчуванням замість помилки. Ці записи запиту не "
            f"стосуються і за результат не видаються. Ручний шлях: {MANUAL_PATH}"
        ),
    )


def _pick_language_hit(
    hits: list[dict[str, Any]], requested_language: str
) -> dict[str, Any] | None:
    """Вибрати запис за мовою; ``None`` — запитаної мовної версії немає.

    Один ECLI ділять оригінал і всі неофіційні переклади (перевірено живцем).
    Без запиту мови перевага — автентичним мовам Суду (ENG, потім FRA); переклад
    ніколи не підставляється мовчки замість оригіналу чи іншої запитаної мови.
    """
    if requested_language:
        wanted3 = _LANG_2_TO_3.get(requested_language, requested_language.upper())
        for hit in hits:
            if str(hit.get("languageisocode") or "").upper() == wanted3:
                return hit
        return None
    for preferred in ("ENG", "FRA"):
        for hit in hits:
            if str(hit.get("languageisocode") or "").upper() == preferred:
                return hit
    return hits[0] if hits else None


def _extract_pdf_text(data: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def _judgment_boundary(lines: list[str]) -> int:
    """Індекс рядка, де починаються додані окремі думки суддів (або кінець тексту).

    Заголовок розпізнається лише рядком ЦІЛКОМ великими літерами — інакше
    згадка «Dissenting Opinion of Judge X» в прозі постанови (посилання на
    іншу справу) хибно обірвала б текст рішення (перевірено на 001-114082,
    де саме так і трапляється до справжнього заголовку).
    """
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped != stripped.upper():
            continue
        if "OPINION" in stripped and any(marker in stripped for marker in _OPINION_MARKERS):
            return index
    return len(lines)


def _paragraph_index(lines: list[str], boundary: int) -> dict[int, int]:
    """Номер абзацу → рядок початку, лише для строго послідовної нумерації.

    Вкладені переліки (заявники в додатку, перелік статей конвенції) теж
    нумеруються з «1.» — приймається лише той збіг, чий номер точно на
    одиницю більший за попередній прийнятий; це природно відсіює вкладені
    переліки без окремого розпізнавання їх форми (перевірено на 001-114082
    і 001-63769 — нуль пропусків у послідовності).
    """
    current = 0
    starts: dict[int, int] = {}
    for index, line in enumerate(lines[:boundary]):
        match = _PARAGRAPH_START_RE.match(line)
        if not match:
            continue
        number = int(match.group(1))
        if number == current + 1:
            current = number
            starts[number] = index
    return starts


def _paragraph_text(
    lines: list[str], starts: dict[int, int], boundary: int, number: int
) -> str | None:
    if number not in starts:
        return None
    start = starts[number]
    end = starts.get(number + 1, boundary)
    text = " ".join(line.strip() for line in lines[start:end] if line.strip())
    return re.sub(r"\s+", " ", text).strip()


def _headers() -> dict[str, str]:
    return {"Accept": "application/json", "User-Agent": "yurko-mcp/1.0 (+intl-law-expansion)"}


def _pdf_headers() -> dict[str, str]:
    return {"User-Agent": "yurko-mcp/1.0 (+intl-law-expansion)"}


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


echr_adapter = register_adapter(EchrAdapter())
