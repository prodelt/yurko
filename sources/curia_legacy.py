"""Картка справи Суду ЄС з легасі-інтерфейсу CURIA (``juris.curia.europa.eu/juris``).

Другий канал :meth:`sources.eu_case_law.EuCaseLawAdapter.public_card`: Cellar
знає справу лише тоді, коли в ньому вже є публікація (повідомлення в OJ C,
рішення, висновок), а до того картка давала голе ``not_published``. Живий
виклик 15.09.2026 на відкриту справу без публікацій саме так і відповів, хоча
провадження вже було зареєстроване.

Легасі-інтерфейс JSF віддає картку серверним HTML (перевірено живим запитом
15.09.2026, docs/research-european-legal-sources.md §3.1):

* ``liste.jsf?num=C-605/26&language=en`` — «Search result: N case(s)» і
  посилання на ``fiche.jsf?id=C;605;26;RP;1;P;1;C2026/0605/P``. Ідентифікатор
  картки кодує вид провадження (``RP``, ``PV``…), тому він береться з переліку, а
  не будується з номера;
* ``fiche.jsf?id=…`` — пари ``<h3>назва</h3>`` + значення: дата подання, суд,
  що звернувся, і держава, предмет, вид провадження, мова справи, АГ, суддя-
  доповідач, дати слухання, висновку й ухвалення;
* ``documents.jsf?num=…`` — таблиця ``tr.table_document_ligne``: назва
  документа, ECLI, дата, посилання.

``curia.europa.eu/juris/liste.jsf`` сьогодні перенаправляє 301 у SPA InfoCuria,
тож адреса — саме ``juris.curia.europa.eu``. Внутрішній бекенд InfoCuria
(``infocuriaws…/elastic-connector``) не API й тут не використовується.

Інтерфейс не оголошено ні стабільним, ні таким, що виводиться з експлуатації:
це сторінка без контракту. Тому зміна розмітки — не «справи немає», а
``source_unavailable`` з причиною ``curia_legacy:unrecognised_page`` і ручним
шляхом. Жодних звернень до e-Curia.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from datetime import timezone
from typing import Any
from urllib.parse import parse_qs, unquote, urlencode, urljoin, urlparse

import requests
from lxml import etree

from core.contracts import SourceUnavailable
from core.source_endpoints import endpoint
from sources.transport import SourceTransport

__all__ = [
    "CHANNEL",
    "CuriaLegacyCard",
    "parse_case_documents",
    "parse_case_fiche",
    "parse_case_list",
]

#: Як канал названо у відповіді ``get_case``.
CHANNEL = "curia_legacy_card"

#: Чесний User-Agent: сторінка публічна, але звертається до неї програма.
_HEADERS = {
    "User-Agent": "Yurko-MCP (public court register reader)",
    "Accept": "text/html",
    "Accept-Language": "en",
}

_NOT_AVAILABLE = "Information not available"


def _text(element: Any) -> str:
    return re.sub(r"\s+", " ", "".join(element.itertext())).strip()


def _parse_html(body: str) -> Any:
    raw = str(body or "").strip()
    if not raw:
        return None
    try:
        return etree.fromstring(raw.encode("utf-8"), parser=etree.HTMLParser(recover=True))
    except (etree.XMLSyntaxError, ValueError):
        return None


def _iso_date(value: str | None) -> str | None:
    """``04/06/2026`` → ``2026-06-04``; нерозпізнане — ``None``, не здогад."""
    match = re.fullmatch(r"(\d{2})/(\d{2})/(\d{4})", str(value or "").strip())
    if match is None:
        return None
    day, month, year = match.groups()
    return f"{year}-{month}-{day}"


def _normalized_case_number(value: str) -> str:
    """``C-605%2F26%20P`` → ``c-605/26 p``: адреси CURIA кодовані двічі."""
    text = str(value or "")
    for _ in range(2):
        unquoted = unquote(text)
        if unquoted == text:
            break
        text = unquoted
    return " ".join(text.strip().lower().split())


@dataclass(frozen=True)
class CaseListing:
    """Що сказав перелік справ за номером."""

    cases_found: int
    fiche_id: str | None
    #: ``in_progress`` / ``closed`` — як позначила сама сторінка; ``None`` — не позначила.
    status: str | None
    #: Перелік має рядки справ, але жоден не той номер, який питали. Це не
    #: «справи немає»: легасі-пошук CURIA номер **звужує**, а не звіряє, тож на
    #: ``C-605/26`` у видачу потрапляють і ``C-605/26 P``, і ``C-605/26 PPU``.
    number_mismatch: bool = False


def parse_case_list(body: str, case_number: str = "") -> CaseListing | None:
    """Перелік ``liste.jsf``; ``None`` — сторінка не того вигляду.

    ``case_number`` — номер, який питали. Кожне посилання на ``fiche.jsf`` несе
    в параметрі ``num`` номер своєї справи, тож картка береться з рядка з тим
    самим номером, а не з першого-ліпшого. До тікета 15 брався перший, і на
    номері із суфіксом (``P``, ``PPU``, ``R``) юрист міг дістати картку чужої
    справи під власним номером — підміну, якої з відповіді не видно.

    Сторінка переглядається **цілком**, а не до першого посилання: рев'ю тікета
    15 показало, що досить одного посилання без ``num`` перед потрібним рядком,
    щоб «перше-ліпше» повернулося тим самим шляхом, який тікет закривав.
    """
    root = _parse_html(body)
    if root is None:
        return None
    page_text = _text(root)
    count = re.search(r"Search result:\s*(\d+)\s*case\(s\)", page_text)
    if count is None:
        return None

    wanted = _normalized_case_number(case_number)
    numbered: list[tuple[str, str]] = []
    numberless: list[str] = []
    for anchor in root.xpath('.//a[contains(@href, "fiche.jsf")]'):
        query = parse_qs(urlparse(anchor.get("href") or "").query)
        ids = query.get("id")
        if not ids:
            continue
        numbers = query.get("num")
        if numbers:
            numbered.append((_normalized_case_number(numbers[0]), ids[0]))
        else:
            numberless.append(ids[0])

    fiche_id: str | None = None
    if wanted:
        fiche_id = next((value for number, value in numbered if number == wanted), None)
    if fiche_id is None and not numbered:
        # Жодне посилання номера не несе (або питали без номера) — вибирати
        # нема з чого, лишається перше.
        fiche_id = next(iter(numberless), None) or (numbered[0][1] if numbered else None)
    if fiche_id is None and not wanted:
        fiche_id = numbered[0][1] if numbered else None

    status = re.search(r"\[Case (in progress|closed)\]", page_text)
    return CaseListing(
        cases_found=int(count.group(1)),
        fiche_id=fiche_id,
        # Посилання з номерами є, але потрібного серед них немає — це саме
        # розбіжність, а не «справи немає».
        number_mismatch=fiche_id is None and bool(numbered),
        status=status.group(1).replace(" ", "_") if status else None,
    )


def _subject_matter(items: list[str]) -> list[str]:
    """Рубрики предмета: ``-  дочірня`` приєднується до попередньої батьківської.

    CURIA друкує рубрикатор двома рівнями окремими ``li``: «Agriculture», потім
    «-  Direct payments». Порізно вони означали б
    дві незалежні рубрики, тому склеюються так, як їх показує сама сторінка.
    """
    merged: list[str] = []
    parent = ""
    parent_used = True
    for item in items:
        if item.startswith("-"):
            child = item.lstrip("-").strip()
            merged.append(f"{parent} - {child}" if parent else child)
            parent_used = True
            continue
        if not parent_used:
            merged.append(parent)
        parent = item
        parent_used = False
    if parent and not parent_used:
        merged.append(parent)
    return merged


def _field_values(heading: Any) -> list[str]:
    """Значення поля картки: усе між ``<h3>`` і наступним заголовком."""
    values: list[str] = []
    node = heading.getnext()
    while node is not None and node.tag not in ("h2", "h3"):
        items = node.xpath(".//li") if node.tag != "li" else [node]
        if node.tag == "ul" or items:
            values.extend(_text(item) for item in items if _text(item))
        else:
            text = _text(node)
            if text:
                values.append(text)
        node = node.getnext()
    return [value for value in values if value != _NOT_AVAILABLE]


def parse_case_fiche(body: str) -> dict[str, Any] | None:
    """Картка ``fiche.jsf`` → поля; ``None`` — сторінка не того вигляду.

    Поле, якого CURIA не має («Information not available»), лишається ``None``
    або порожнім переліком: не відомо — отже, не відомо.
    """
    root = _parse_html(body)
    if root is None:
        return None
    titles = root.xpath('.//div[contains(@class, "details_decision_title")]')
    if not titles:
        return None
    title_lines = [line.strip() for line in titles[0].itertext() if line.strip()]

    fields: dict[str, list[str]] = {}
    for heading in root.xpath(".//h3"):
        label = _text(heading)
        if label and label not in fields:
            fields[label] = _field_values(heading)

    def first(label: str) -> str | None:
        values = fields.get(label) or []
        return values[0] if values else None

    referring_court: str | None = None
    referring_state: str | None = None
    source = first("Source of the question referred for a preliminary ruling")
    if source:
        court, _, state = source.rpartition(" - ")
        referring_court, referring_state = (court, state) if court else (source, None)

    procedure = fields.get("Procedure and result") or []
    return {
        "case_name": title_lines[0] if title_lines else None,
        "lodged_on": _iso_date(first("Date of lodging of the application initiating proceedings")),
        "referring_court": referring_court,
        "referring_state": referring_state,
        "subject_matter": _subject_matter(fields.get("Subject matter") or []),
        "procedure": "; ".join(procedure) or None,
        "language_of_case": list(fields.get("Language(s) of the case") or []),
        "parties": first("Name of the parties"),
        "formation": first("Formation of the Court"),
        "judge_rapporteur": first("Judge-Rapporteur"),
        "advocate_general": first("Advocate General"),
        "hearing_on": _iso_date(first("Date of the hearing")),
        "opinion_on": _iso_date(first("Date of the Opinion")),
        "delivered_on": _iso_date(first("Date of delivery")),
        "official_journal": first("Publication in the Official Journal"),
    }


def _without_session(url: str) -> str:
    """Адреса без ``;jsessionid=…`` і ``cid=…`` сеансу JSF, яким сторінку прочитано.

    Жива сторінка 15.09.2026 вшивала обидва в кожне посилання: такі адреси
    протухають разом із сеансом і не є адресою документа.
    """
    cleaned = re.sub(r";jsessionid=[^?#]*", "", url)
    cleaned = re.sub(r"([?&])cid=\d+&", r"\1", cleaned)
    return re.sub(r"[?&]cid=\d+$", "", cleaned)


def _document_links(row: Any, base: str) -> dict[str, str]:
    """Посилання рядка документа, без мовних варіантів із підказки."""
    links: dict[str, str] = {}
    for anchor in row.xpath('.//a[@href][not(ancestor::div[contains(@class, "tooltip")])]'):
        href = _without_session(urljoin(base, anchor.get("href") or ""))
        if "document.jsf" in href:
            links.setdefault("html_url", href)
        elif "showPdf.jsf" in href:
            links.setdefault("pdf_url", href)
        elif "eur-lex.europa.eu" in href:
            links.setdefault("eurlex_url", href)
        elif href.startswith("http"):
            links.setdefault("external_url", href)
    return links


def parse_case_documents(body: str, *, base: str) -> tuple[int, list[dict[str, Any]]] | None:
    """Перелік ``documents.jsf`` → (заявлена кількість, документи); ``None`` — не той вигляд."""
    root = _parse_html(body)
    if root is None:
        return None
    count = re.search(r"(\d+)\s*document\(s\)", _text(root))
    if count is None:
        return None
    documents: list[dict[str, Any]] = []
    for row in root.xpath('.//tr[contains(@class, "table_document_ligne")]'):
        cells = row.findall("td")
        if len(cells) < 3:
            continue
        title_cell = cells[1]
        ecli_nodes = title_cell.xpath('.//span[contains(@class, "outputEcli")]')
        ecli = _text(ecli_nodes[0]) if ecli_nodes else ""
        title = re.sub(r"\s+", " ", (title_cell.text or "")).strip()
        raw_date = _text(cells[2])
        documents.append(
            {
                "title": title,
                "ecli": ecli or None,
                "date": _iso_date(raw_date),
                **_document_links(row, base),
            }
        )
    documents.sort(key=lambda item: (item.get("date") or "", item["title"]))
    return int(count.group(1)), documents


class CuriaLegacyCard:
    """Читання картки справи з легасі-інтерфейсу CURIA через спільний транспорт."""

    def __init__(self, transport: SourceTransport, *, source_id: str) -> None:
        self._transport = transport
        self._source_id = source_id

    @staticmethod
    def page_url(page: str, **params: str) -> str:
        return f"{endpoint('eu.curia_juris')}/{page}?{urlencode(params, safe='/')}"

    def _unavailable(self, reason: str, manual_url: str) -> SourceUnavailable:
        return SourceUnavailable(
            source=self._source_id,
            retry_after="через 15 хвилин",
            reason=f"curia_legacy:{reason}",
            manual_path=(
                f"Картка справи вручну: {manual_url} (легасі-інтерфейс CURIA); "
                "якщо сторінка не відкривається — пошук за номером справи в InfoCuria "
                "https://infocuria.curia.europa.eu/"
            ),
        )

    def _page(self, page: str, params: dict[str, str], manual_url: str) -> str | SourceUnavailable:
        try:
            response = self._transport.get(
                f"{endpoint('eu.curia_juris')}/{page}",
                params=params,
                headers=_HEADERS,
                timeout=30,
            )
        except requests.RequestException as error:
            return self._unavailable(f"network_error:{type(error).__name__}", manual_url)
        if response.status_code >= 400:
            return self._unavailable(f"http_{response.status_code}", manual_url)
        return str(response.text or "")

    def read(self, case_number: str) -> dict[str, Any] | SourceUnavailable:
        """Поля картки справи або ``source_unavailable`` з ручним шляхом."""
        manual_url = self.page_url("liste.jsf", num=case_number, language="en")
        fetched_at = dt.datetime.now(timezone.utc).isoformat()

        listing_body = self._page("liste.jsf", {"num": case_number, "language": "en"}, manual_url)
        if isinstance(listing_body, SourceUnavailable):
            return listing_body
        listing = parse_case_list(listing_body, case_number)
        if listing is None:
            return self._unavailable("unrecognised_page", manual_url)
        if listing.number_mismatch:
            # Картка чужої справи під запитаним номером гірша за відмову:
            # з відповіді підміну не видно, бо номер у ній — власний.
            return self._unavailable("case_number_mismatch", manual_url)

        card: dict[str, Any] = {
            "channel": CHANNEL,
            "source_url": manual_url,
            "fetched_at": fetched_at,
            "case_found": listing.cases_found > 0,
            "case_status": listing.status,
        }
        if listing.cases_found == 0 or not listing.fiche_id:
            card["public_documents"] = []
            return card

        fiche_url = self.page_url("fiche.jsf", id=listing.fiche_id, language="en")
        fiche_body = self._page("fiche.jsf", {"id": listing.fiche_id, "language": "en"}, manual_url)
        if isinstance(fiche_body, SourceUnavailable):
            return fiche_body
        fiche = parse_case_fiche(fiche_body)
        if fiche is None:
            return self._unavailable("unrecognised_page", manual_url)

        documents_body = self._page(
            "documents.jsf", {"num": case_number, "language": "en"}, manual_url
        )
        if isinstance(documents_body, SourceUnavailable):
            return documents_body
        parsed = parse_case_documents(documents_body, base=endpoint("eu.curia_juris") + "/")
        if parsed is None:
            return self._unavailable("unrecognised_page", manual_url)
        declared, documents = parsed

        card.update(fiche)
        card["case_url"] = fiche_url
        card["public_documents"] = documents
        # Сторінка сама називає, скільки документів у переліку; розбіжність із
        # розібраними рядками означає, що розбір щось пропустив, і про це треба
        # сказати, а не віддати урізаний перелік як повний.
        card["documents_complete"] = declared == len(documents)
        return card
