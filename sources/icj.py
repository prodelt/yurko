"""Адаптер офіційного архіву Міжнародного Суду ООН — ICJ (T144).

**Принцип VI — обов'язковість.** Рішення ICJ обов'язкове лише для сторін спору
і лише в межах цієї справи (ст. 59 Статуту Суду). Кожен успішний ``fetch`` несе
поле ``binding_scope``/``binding_notice``, яке прямо це каже — так само, як у
:mod:`sources.echr`. Резюме Секретаріату та академічний корпус CD-ICJ не є
офіційним джерелом і тут не використовуються (ADR 0005).

**Що дійсно доступно (жива проба 2026-09-07, research/13, розділ E).**
Карткова сторінка справи ``https://www.icj-cij.org/case/<номер>`` віддається
без перешкод (200, HTML) і містить прямі посилання на офіційні PDF —
рішення, накази, усні слухання, кожен у форматі
``<номер справи>-<РРРРММДД>-<ТИП>-<N1>-<N2>-<EN|FR|BI>.pdf`` (перевірено на
справі 143: 127 таких посилань). Це і є :meth:`IcjAdapter.card`.

Сам PDF рішення — ``.../sites/default/files/case-related/143/143-20120203-JUD-01-00-EN.pdf``
— відповідає **403** зі сторінкою Cloudflare «Just a moment…» (JS-challenge).
Перевірено з UA цього сервера, зі звичайним браузерним UA і через
``api.icj-cij.org`` (редиректить на той самий хост) — результат той самий.
``robots.txt`` сайту цей шлях явно дозволяє: блокує не політика сайту, а WAF.

**Межа продукту (принцип III).** Підробка User-Agent, емуляція браузера,
розв'язання challenge, проксі — заборонені незалежно від того, наскільки легко
технічно це було б. Тому :meth:`fetch` на операцію читання тексту/абзацу
повертає :class:`~contracts.NotCovered`, а не :class:`~contracts.SourceUnavailable`:
- ``NotCovered`` описаний у ``contracts.py`` як «операція не має перевіреної
  можливості» (FR-203) і **вимагає** ручного шляху — це наш випадок буквально:
  операція ``read_document``/``read_fragment`` для ICJ сьогодні не має
  перевіреної можливості, а ручний шлях (пряме посилання на офіційний PDF) у
  нас є завжди, бо адресу будує сам ідентифікатор документа.
- ``SourceUnavailable`` натомість обіцяє «розумну наступну спробу» через
  конкретний час (``retry_after``) — тут це була б брехня: захист Cloudflare
  не є тимчасовим збоєм джерела, він не мине сам за 15 хвилин, і адаптер не
  може обіцяти, коли (і чи) зміниться політика сайту.

Це **стан захисту сайту на дату проби, а не постійний факт і не відсутність
документа** — деталі відказу прямо це проговорюють, включно з датою проби й
точним HTTP-статусом останньої спроби, а не застиглим текстом.

Якщо колись PDF почне віддаватися звичайним GET (без обходу захисту) —
``fetch`` сам це виявить (він завжди робить живий запит, а не одразу здається)
і розбере документ через ``pypdf``, повертаючи текст і абзаци так само, як
:mod:`sources.echr`.
"""

from __future__ import annotations

import datetime as dt
import io
import logging
import re
from typing import Any

import requests

from core.contracts import NotCovered, NotFound, SourceHealth, SourcePolicy
from core.legal_orders import CoverageLayer, Source, SourceAccess
from core.provenance import ContentKind, ProvenanceStamp, PublicationKind, SourceChannel
from core.source_endpoints import endpoint
from sources import register_adapter
from sources.base import AdapterPayload, AdapterResult, RegistryAdapter, typed_failures
from sources.transport import SourceTransport

logger = logging.getLogger("ukraine-laws")

__all__ = ["IcjAdapter", "icj_adapter"]

#: Обов'язкове поле контракту get_decision для міжнародного форуму (принцип VI).
BINDING_NOTICE = (
    "Рішення Міжнародного Суду ООН є обов'язковим лише для сторін спору і в межах "
    "цієї справи (ст. 59 Статуту Суду); за межами сторін і справи воно не є "
    "обов'язковим прецедентом."
)

ID_FORMAT_HINT = (
    "ідентифікатор офіційного файла ICJ у форматі "
    "«<номер справи>-<РРРРММДД>-<ТИП>-<NN>-<NN>-<EN|FR|BI>» "
    "(напр. 070-19860627-JUD-01-00-EN); дізнатись його можна через card(<номер справи>)"
)

CASE_ID_FORMAT_HINT = "номер справи ICJ — ціле число (напр. 70)"

_DEFAULT_TIMEOUT = 20
_PDF_TIMEOUT = 30

_DOC_ID_RE = re.compile(r"^(\d{1,5})-(\d{8})-([A-Z]{2,5})-(\d{2})-(\d{2})-(EN|FR|BI)$")
_CASE_NUMBER_RE = re.compile(r"^\d{1,5}$")

_KIND_LABELS = {
    "JUD": "judgment",
    "ORD": "order",
    "ORA": "oral_proceedings_verbatim",
    "AVIS": "advisory_opinion",
    "COM": "press_communique",
}
_LANG_LABELS = {"EN": "en", "FR": "fr", "BI": "bi"}


def _joined(parts: Any) -> str:
    """Реквизиты через запятую; пустого места в ссылке не остаётся."""
    return ", ".join(str(part).strip() for part in parts if str(part or "").strip())


_PARAGRAPH_START_RE = re.compile(r"^\s*(\d+)\.\s+\S")
_PATH_NUMBER_RE = re.compile(r"(\d+)")

_PDF_LINK_RE = re.compile(r'href="([^"]+/case-related/(\d+)/([^"/]+)\.pdf)"', re.IGNORECASE)
_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.IGNORECASE | re.DOTALL)


class IcjAdapter(RegistryAdapter):
    """Офіційний архів ICJ: картка справи (жива) і читання PDF (за наявності доступу)."""

    source_id = "icj_official_archive"
    id_format_hint = ID_FORMAT_HINT
    legal_order = "ICJ"
    source_policy = SourcePolicy.RESTRICTED
    #: Дата фільтрації рішень ICJ живим запитом не перевірялась — пошуку немає взагалі.
    supports_date_filter = False

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
            access=SourceAccess.OFFICIAL_FILE,
            license="офіційний архів icj-cij.org; ліцензія на повторне використання не вказана",
            manual_path=(
                f"{endpoint('icj.court')}/case/<номер справи> вручну; офіційний PDF рішення, "
                "напр. case-related/143/143-20120203-JUD-01-00-EN.pdf"
            ),
            source_url=endpoint("icj.court"),
            description=(
                "Картка справи читається наживо (case-related PDF-посилання); сам PDF "
                "рішення на дату проби закритий Cloudflare JS-challenge (403) — стан "
                "захисту сайту, не постійний факт"
            ),
        )

    # -- робота з джерелом ------------------------------------------------

    def card(self, document_id: str, **options: Any) -> AdapterResult:
        """Картка справи: назва й перелік офіційних PDF за посиланнями зі сторінки.

        ``publication_kind=card``, ``content_kind=metadata`` — картка ніколи не
        підтверджує цитату (це навмисно і чесно, а не недогляд).
        """
        case_number = str(document_id or "").strip()
        if not _CASE_NUMBER_RE.match(case_number):
            return NotFound(identifier=case_number, id_format_hint=CASE_ID_FORMAT_HINT)

        url = f"{endpoint('icj.court')}/case/{case_number}"
        try:
            response = self._transport.get(url, timeout=_DEFAULT_TIMEOUT, headers=_html_headers())
        except requests.RequestException as error:
            logger.warning("icj.court card unreachable: %s", type(error).__name__)
            return NotCovered(
                legal_order=self.legal_order,
                manual_path=url,
                subject=case_number,
                operation="card",
                source_id=self.source_id,
            )
        if response.status_code != 200:
            return NotFound(
                identifier=case_number,
                id_format_hint=CASE_ID_FORMAT_HINT,
                message=(
                    f"Картку справи {case_number} не знайдено (ICJ відповів "
                    f"{response.status_code}). Ручний шлях: {url}"
                ),
            )

        html = _decode(response)
        stub = self.reject_stub(html, document_id=case_number, min_chars=200)
        if stub is not None:
            return stub

        title_match = _TITLE_RE.search(html)
        title = re.sub(r"\s+", " ", title_match.group(1)).strip() if title_match else case_number

        documents = _extract_case_documents(html, case_number)

        provenance = ProvenanceStamp(
            source_channel=SourceChannel.LIVE,
            source_url=url,
            language="en",
            # Карточка дела называет само дело, а не пример из другого:
            # «ICJ Reports 2012, p. 99 (напр.)» стояло здесь на любой карточке.
            citation_format=_joined((f"ICJ, справа № {case_number}", title)),
            stale=False,
            is_authentic_version=True,
            is_translation=False,
            publication_kind=PublicationKind.CARD,
            content_kind=ContentKind.METADATA,
        )
        payload = {
            "case_number": case_number,
            "title": title,
            "url": url,
            "documents": documents[:100],
            "documents_count": len(documents),
            "retrieved_at": _now_iso(),
        }
        if len(documents) > 100:
            payload["documents_truncated"] = True
        return AdapterPayload(data=payload, provenance=provenance)

    def public_card(self, case_number: str) -> AdapterResult:
        """Та сама картка, під іменем і формою, яку шукає інструмент ``get_case``.

        ``get_case`` маршрутизує запит операцією ``public_case_card`` і кличе
        ``adapter.public_card(case_number)`` — той самий генеричний контракт,
        що в :meth:`sources.eu_case_law.EuCaseLawAdapter.public_card`
        (``CaseCardOutput`` вимагає ``court``/``published_status``/
        ``public_documents``, яких немає у формі :meth:`card`, розрахованій на
        ``get_law_metadata``/``Operation.CARD``). Реєстр (``legal_orders.py``)
        ніс лише ``card``/``Operation.CARD``, і ``get_case("ICJ", "143")``
        відмовляв ``not_covered``, хоча ця сама картка справи читається
        цілком (T-icj-card, 2026-09-09). Мережевий запит один — тут лише
        переклад форми; ``card()`` і його контракт лишаються без змін.
        """
        result = self.card(case_number)
        if not isinstance(result, AdapterPayload):
            return result
        data = dict(result.data)
        documents = list(data.get("documents") or [])
        url = str(data.get("url") or f"{endpoint('icj.court')}/case/{case_number}")
        payload: dict[str, Any] = {
            "case_number": data.get("case_number", case_number),
            "court": "Міжнародний Суд ООН (International Court of Justice)",
            "title": data.get("title", ""),
            "published_status": "published" if documents else "not_published",
            "published": bool(documents),
            "public_documents": documents,
            "url": url,
            "checked_at": data.get("retrieved_at", ""),
            "checked_sources": (url,),
            "manual_path": f"{url} вручну",
        }
        if data.get("documents_truncated"):
            payload["documents_truncated"] = True
        return AdapterPayload(data=payload, provenance=result.provenance)

    @typed_failures(source_id="icj_official_archive")
    def fetch(self, document_id: str, **options: Any) -> AdapterResult:
        identifier = str(document_id or "").strip()
        if not identifier:
            return NotFound(identifier=identifier, id_format_hint=ID_FORMAT_HINT)

        match = _DOC_ID_RE.match(identifier)
        if not match:
            return NotFound(identifier=identifier, id_format_hint=ID_FORMAT_HINT)
        case_number, date_raw, kind, n1, n2, lang = match.groups()

        requested_language = str(options.get("language") or "").strip().lower()
        if requested_language:
            wanted = {"en": "EN", "fr": "FR"}.get(requested_language)
            if wanted is not None and wanted != lang:
                return NotFound(
                    identifier=identifier,
                    id_format_hint=ID_FORMAT_HINT,
                    message=(
                        f"Ідентифікатор {identifier} сам визначає мову ({lang}); запитана "
                        f"мова «{requested_language}» їй не відповідає. Переклад не "
                        "підставляється замість запитаної мовної версії — вкажіть ідентифікатор "
                        "потрібної мови (card() покаже наявні)."
                    ),
                )

        pdf_url = f"{endpoint('icj.court')}/sites/default/files/case-related/{case_number}/{identifier}.pdf"
        operation = "read_fragment" if str(options.get("path") or "").strip() else "read_document"

        try:
            response = self._transport.get(pdf_url, timeout=_PDF_TIMEOUT, headers=_pdf_headers())
        except requests.RequestException as error:
            return NotCovered(
                legal_order=self.legal_order,
                manual_path=pdf_url,
                subject=identifier,
                operation=operation,
                source_id=self.source_id,
                message=(
                    f"Мережевий запит до офіційного PDF {identifier} не вдався "
                    f"({type(error).__name__}) — перевірена можливість читання відсутня. "
                    f"Ручний шлях: {pdf_url}"
                ),
            )

        content = bytes(getattr(response, "content", b"") or b"")
        is_pdf = response.status_code == 200 and content[:5] == b"%PDF-"
        if not is_pdf:
            probed_at = _now_iso()
            return NotCovered(
                legal_order=self.legal_order,
                manual_path=pdf_url,
                subject=identifier,
                operation=operation,
                source_id=self.source_id,
                message=(
                    f"Офіційний PDF {identifier} на {probed_at} відповів HTTP "
                    f"{response.status_code} (типово — Cloudflare JS-challenge, «Just a "
                    "moment…»), а не документом. Це стан захисту сайту на дату проби, а не "
                    "відсутність документа і не постійний факт — спробу зафіксовано, обхід "
                    f"захисту не виконувався. Ручний шлях: {pdf_url}"
                ),
            )

        # Якщо колись PDF таки віддається — розбираємо його чесно, без прикидання.
        try:
            full_text = _extract_pdf_text(content)
        except Exception as error:  # noqa: BLE001 — межа розбору PDF
            logger.warning(
                "icj.court pdf parse failed for %s: %s", identifier, type(error).__name__
            )
            return NotCovered(
                legal_order=self.legal_order,
                manual_path=pdf_url,
                subject=identifier,
                operation=operation,
                source_id=self.source_id,
                message=(
                    f"PDF {identifier} отримано, але розібрати pypdf не вдалося. "
                    f"Ручний шлях: {pdf_url}"
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
        starts = _paragraph_index(lines)

        requested_path = str(options.get("path") or "").strip()
        paragraph_number: str | None = None
        if requested_path:
            path_match = _PATH_NUMBER_RE.search(requested_path)
            if not path_match:
                return NotFound(
                    identifier=identifier,
                    id_format_hint="номер параграфа рішення, напр. «§ 78», «78»",
                )
            paragraph_number = path_match.group(1)

        if paragraph_number is not None:
            n = int(paragraph_number)
            text = _paragraph_text(lines, starts, n)
            if text is None:
                last = max(starts) if starts else 0
                return NotFound(
                    identifier=identifier,
                    id_format_hint=f"параграф {paragraph_number} не знайдено; у документі {last} параграфів",
                )
            content_kind = ContentKind.FRAGMENT
            locator = f"§{paragraph_number}"
        else:
            text = "\n".join(line.strip() for line in lines if line.strip())
            text = re.sub(r"[ \t]+", " ", text).strip()
            content_kind = ContentKind.FULL_TEXT
            locator = ""

        formatted_date = f"{date_raw[0:4]}-{date_raw[4:6]}-{date_raw[6:8]}"
        language2 = _LANG_LABELS.get(lang, "en")
        provenance = ProvenanceStamp(
            source_channel=SourceChannel.LIVE,
            source_url=pdf_url,
            language=language2,
            # Тот же дефект, что был в ЄСПЛ: здесь стоял литерал с
            # реквизитами одного конкретного дела, зашитыми в шаблон. Что бы ни
            # читали, конверт называл это дело. Собирается из прочитанного документа.
            citation_format=_joined(
                (
                    case_number,
                    _KIND_LABELS.get(kind, kind.lower()),
                    formatted_date,
                    f"§ {paragraph_number}" if paragraph_number else "",
                )
            )
            or identifier,
            stale=False,
            is_authentic_version=lang in ("EN", "FR", "BI"),
            is_translation=False,
            publication_kind=PublicationKind.COURT_PUBLICATION,
            content_kind=content_kind,
            version_id=identifier,
        )
        payload: dict[str, Any] = {
            "decision_id": identifier,
            "resolved_document_id": identifier,
            "case_number": case_number,
            "document_type": _KIND_LABELS.get(kind, kind.lower()),
            "decision_date": formatted_date,
            "language": language2,
            "url": pdf_url,
            "locator": locator,
            "text": text,
            "char_count": len(text),
            "from_cache": False,
            "source": self.source_id,
            "retrieved_at": _now_iso(),
            "binding_scope": "parties_and_case_only",
            "binding_notice": BINDING_NOTICE,
        }
        return AdapterPayload(data=payload, provenance=provenance)

    @typed_failures(source_id="icj_official_archive")
    def search(self, query: str, **options: Any) -> AdapterResult:
        """Пошуку по ICJ немає — жодного підтвердженого способу знайти справу за текстом."""
        return NotCovered(
            legal_order=self.legal_order,
            manual_path=f"{endpoint('icj.court')}/list-of-all-cases вручну",
            subject=query,
            operation="search_free_text",
            source_id=self.source_id,
        )

    def health(self) -> SourceHealth:
        """Правдива проба: картка (жива) відображена окремо від PDF (закритий).

        ``ok=True`` відповідає рівно тому, що дійсно перевірено — операції
        ``card``; деталі прямо називають, що читання документа/абзацу
        заблоковано Cloudflare на дату проби, щоб ``ok=True`` не читалося як
        «джерело повністю працює».
        """
        url = f"{endpoint('icj.court')}/case/143"
        try:
            response = self._transport.get(url, timeout=_DEFAULT_TIMEOUT, headers=_html_headers())
        except requests.RequestException as error:
            return SourceHealth(
                adapter=self.source_id,
                ok=False,
                source_policy=self.source_policy,
                details={"error": type(error).__name__, "operation": "card"},
            )
        ok = response.status_code == 200
        return SourceHealth(
            adapter=self.source_id,
            ok=ok,
            source_policy=self.source_policy,
            details={
                "operation": "card",
                "status_code": response.status_code,
                "note": (
                    "ok відповідає лише операції card; читання PDF рішення на дату "
                    "останньої проби (2026-09-07) заблоковане Cloudflare "
                    "JS-challenge (403) — це не перевіряється тут повторно щоразу, щоб "
                    "не навантажувати захищений ресурс марними спробами"
                ),
            },
        )


def _extract_case_documents(html: str, case_number: str) -> list[dict[str, str]]:
    seen: dict[str, dict[str, str]] = {}
    for href, matched_case, stem in _PDF_LINK_RE.findall(html):
        if matched_case != case_number:
            continue
        record: dict[str, str] = {"document_id": stem, "url": href}
        match = _DOC_ID_RE.match(stem)
        if match:
            _, date_raw, kind, _n1, _n2, lang = match.groups()
            record["date"] = f"{date_raw[0:4]}-{date_raw[4:6]}-{date_raw[6:8]}"
            record["document_type"] = _KIND_LABELS.get(kind, kind.lower())
            record["language"] = _LANG_LABELS.get(lang, lang.lower())
        seen[stem] = record
    return sorted(seen.values(), key=lambda r: (r.get("date", ""), r["document_id"]))


def _extract_pdf_text(data: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def _paragraph_index(lines: list[str]) -> dict[int, int]:
    """Строго послідовна нумерація параграфів — той самий підхід, що в HUDOC.

    Не звірено живим PDF ICJ (доступ закритий на дату проби), тому це
    консервативне припущення за аналогією з ECHR, а не підтверджений факт.
    """
    current = 0
    starts: dict[int, int] = {}
    for index, line in enumerate(lines):
        match = _PARAGRAPH_START_RE.match(line)
        if not match:
            continue
        number = int(match.group(1))
        if number == current + 1:
            current = number
            starts[number] = index
    return starts


def _paragraph_text(lines: list[str], starts: dict[int, int], number: int) -> str | None:
    if number not in starts:
        return None
    start = starts[number]
    end = starts.get(number + 1, len(lines))
    text = " ".join(line.strip() for line in lines[start:end] if line.strip())
    return re.sub(r"\s+", " ", text).strip()


def _decode(response: Any) -> str:
    text = getattr(response, "text", None)
    if isinstance(text, str) and text:
        return text
    content = getattr(response, "content", b"") or b""
    return bytes(content).decode("utf-8", errors="replace")


def _html_headers() -> dict[str, str]:
    return {
        "Accept": "text/html,application/xhtml+xml",
        "User-Agent": "yurko-mcp/1.0 (+intl-law-expansion)",
    }


def _pdf_headers() -> dict[str, str]:
    return {"User-Agent": "yurko-mcp/1.0 (+intl-law-expansion)"}


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


icj_adapter = register_adapter(IcjAdapter())
