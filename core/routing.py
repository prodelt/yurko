"""Channel routing: the live Rada channel is primary, the index is a hot cache.

Three things live here, and nothing else:

* **document classification** — ``classify_doc_kind`` / ``extract_real_title``. Both
  are pure functions of the loaded text. They produce the two seam fields
  (``doc_kind``, ``real_title``) that the persistence gate reads before deciding
  what may be stored. This module only *produces* them; the gate itself is not here.
* **the loader** — :class:`LiveFirstLoader` duck-types the source adapter that
  ``CacheManager.get_or_fetch`` calls, and puts the live open-data channel in front
  of the print-page scraper. A miss in the index therefore still reaches the network:
  that ability is the whole point of this map and a regression in it has already
  happened once (ticket 14).
* **provenance** — :func:`provenance` turns a cache entry into the "where did this
  come from and how old is it" block. An out-of-date answer with no such marker is
  a defect in a legal product, not a cosmetic omission.
* **coverage routing** (T015) — :func:`route` decides, from the pair *legal order +
  coverage layer*, whether a request reaches an adapter at all. A request into the
  ``not_covered`` layer is answered with a refusal and a manual path instead of
  being sent to a source that cannot serve it (principle III).

  The registry :func:`route` consults is empty until the coverage map fills it
  (T025–T028), and that is deliberate: the map must be the single source of truth
  about coverage, so this module holds the mechanism and no list of its own —
  a divergence between the map and the behaviour must not be constructible
  (FR-004). Wiring the tools onto this function is T030's job, not this one's.

Measured behaviour of the two Rada hosts: ``docs/research-rada-search.md``.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass, field
from datetime import timezone
from typing import Any, Mapping

from core.contracts import NotCovered, SourceAdapterError, TypedFailure, unknown_legal_order
from core.legal_orders import (
    LEGAL_ORDERS,
    Capability,
    CoverageLayer,
    DocumentClass,
    LegalOrderKind,
    Operation,
    Source,
    capabilities_for,
    capabilities_of,
    get_legal_order,
    is_known_legal_order,
    normalize_legal_order_code,
    verified_layer,
)
from core.legal_orders import sources_for as _registered_sources_for
from core import source_cache
from core.provenance import ProvenanceStamp, stamp_from_cache_entry
from registries.rada_open_data import (
    STATUS_IN_FORCE,
    TYPE_CODE,
    TYPE_LAW,
    RadaDocumentNotFound,
    RadaOpenDataError,
)

logger = logging.getLogger("ukraine-laws")


class DocumentNotFound(LookupError):
    """No such document exists — as opposed to a source we could not reach.

    Kept apart from :class:`SourceAdapterError` on purpose: the two need opposite
    answers. An unreachable source deserves a retry and a stale copy; a document
    that does not exist deserves an immediate, honest "not found".
    """


KIND_LAW = "law"
KIND_CODE = "code"
KIND_CONSTITUTION = "constitution"
KIND_BYLAW = "bylaw"
KIND_UNKNOWN = "unknown"

#: Kinds that are allowed to settle in the working set. Everything else is read
#: live and thrown away (rule Q11-B). The gate that enforces this is not here.
CACHEABLE_KINDS = (KIND_LAW, KIND_CODE, KIND_CONSTITUTION)

TYPE_CONSTITUTION = "216"
_TYPE_TO_KIND = {
    TYPE_LAW: KIND_LAW,
    TYPE_CODE: KIND_CODE,
    TYPE_CONSTITUTION: KIND_CONSTITUTION,
}

#: Rada's own header line, e.g.
#: "Верховна Рада України; Кодекс України, Закон, Кодекс від 05.04.2001 № 2341-III".
#: Only what stands between the issuer and the date is a requisite — the title above
#: it routinely contains the word "Закон" while naming a Cabinet resolution, so
#: classifying on the whole head window would mislabel подзаконка as law.
_DESCRIPTOR_RE = re.compile(r"(?m)^[^\n]{0,200};\s*([^\n;]{2,120}?)\s+від\s+\d{2}\.\d{2}\.\d{4}")

#: Print-page first line: "Про лікарські засоби | від 04.04.1996 № 123/96-ВР (Текст для друку)"
_PRINT_TITLE_RE = re.compile(r"^(.+?)\s*\|\s*від\s+\d{2}\.\d{2}\.\d{4}")

_BYLAW_WORDS = (
    "постанова",
    "наказ",
    "указ",
    "розпорядження",
    "рішення",
    "лист",
    "інструкція",
    "положення",
    "порядок",
    "декрет",
    "угода",
    "конвенція",
)

#: Cabinet resolutions and orders carry the year in the id: 1178-2022-п, 815-2026-р.
_BYLAW_ID_RE = re.compile(r"(?i)-\d{4}-(?:п|р)$|^z\d+")

_BOILERPLATE_PREFIXES = (
    "стан:",
    "ідентифікатор:",
    "текст документа",
    "друкувати",
    "допомога",
    "шрифт",
    "[ image ]",
    "документ",
    "сторінка",
)


def classify_doc_kind(
    text: str = "",
    *,
    doc_type_code: str | int | None = None,
    law_id: str = "",
) -> str:
    """Return ``law`` / ``code`` / ``constitution`` / ``bylaw`` / ``unknown``.

    ``doc_type_code`` is Rada's own type number when we happen to know it (1 Закон,
    21 Кодекс, 216 Конституція) and wins outright. Otherwise the header line of the
    loaded text decides, and only that line — see ``_DESCRIPTOR_RE``.
    """
    if doc_type_code is not None:
        code = str(doc_type_code).strip()
        if code in _TYPE_TO_KIND:
            return _TYPE_TO_KIND[code]
        if code:
            return KIND_BYLAW

    head = str(text or "")[:4000]

    for requisites in _DESCRIPTOR_RE.findall(head):
        lowered = str(requisites).lower()
        if "конституція" in lowered:
            return KIND_CONSTITUTION
        if "кодекс" in lowered:
            return KIND_CODE
        if "закон" in lowered:
            return KIND_LAW
        if any(word in lowered for word in _BYLAW_WORDS):
            return KIND_BYLAW

    if law_id and _BYLAW_ID_RE.search(str(law_id).strip()):
        return KIND_BYLAW

    # Print pages carry no requisite line; the issuer heading is what is left. It is
    # the FIRST all-caps heading that decides — scanning the window as one blob
    # mislabels a law amending a code as a code, and a law citing a ministry order
    # as подзаконка. Both were observed on real cached texts.
    return _classify_by_heading(head)


def _classify_by_heading(head: str) -> str:
    for line in head.splitlines():
        candidate = " ".join(line.split())
        if len(candidate) < 4 or len(candidate) > 90:
            continue
        if candidate != candidate.upper():
            continue
        if "КОНСТИТУЦІЯ" in candidate:
            return KIND_CONSTITUTION
        if "КОДЕКС" in candidate:
            return KIND_CODE
        if "ЗАКОН" in candidate:
            return KIND_LAW
        if any(
            marker in candidate
            for marker in (
                "КАБІНЕТ МІНІСТРІВ",
                "МІНІСТЕРСТВО",
                "ПОСТАНОВА",
                "НАКАЗ",
                "УКАЗ",
                "РОЗПОРЯДЖЕННЯ",
                "РІШЕННЯ",
                "ПОЛОЖЕННЯ",
            )
        ):
            return KIND_BYLAW
    return KIND_UNKNOWN


def extract_real_title(text: str) -> str:
    """Pull the document's own title out of the loaded text, or return ``""``.

    Needed because ``law_registry._dynamic_law`` fills in the placeholder
    "Zakon Rada document {id}" for anything outside the local index: the text that
    comes back is right while the title stays a stub. Both shapes we load are
    handled — the open-data page opens with the bare title, the print page opens
    with "Назва | від 04.04.1996 № 123/96-ВР (Текст для друку)".
    """
    lines = [" ".join(line.split()) for line in str(text or "").splitlines()]
    for index, line in enumerate(lines):
        candidate = line
        if not candidate:
            continue

        print_match = _PRINT_TITLE_RE.match(candidate)
        if print_match:
            candidate = print_match.group(1).strip()

        lowered = candidate.lower()
        if any(lowered.startswith(prefix) for prefix in _BOILERPLATE_PREFIXES):
            continue
        # The requisite line is not a title even though it sits near the top.
        if _DESCRIPTOR_RE.match(candidate):
            continue
        if len(candidate) < 5 or len(candidate) > 500:
            continue
        return _undo_ellipsis(candidate, lines[index + 1 : index + 60])
    return ""


def _undo_ellipsis(candidate: str, following: list[str]) -> str:
    """Rada truncates the title in a print page header ("Про затвердження ...").

    The untruncated title is repeated further down the same page, so a header that
    ends in an ellipsis is replaced by the first later line that starts the same way.
    """
    if not candidate.endswith(("...", "…")):
        return candidate
    prefix = candidate.rstrip(".… ")[:24]
    if len(prefix) < 12:
        return candidate
    for line in following:
        if len(line) > len(candidate) and line.startswith(prefix) and len(line) <= 500:
            return line
    return candidate


#: Язык и формат ссылки, которыми описывается ответ по украинскому каналу.
#: Берутся из реестра правопорядков, а не задаются здесь: иначе описание канала
#: и описание правопорядка разъезжаются молча.
_UA = LEGAL_ORDERS["UA"]


def provenance_stamp(
    entry: dict[str, Any] | None,
    *,
    legal_order: str = "UA",
    source_url: str = "https://zakon.rada.gov.ua",
) -> ProvenanceStamp:
    """Собрать полный конверт происхождения из записи горячего кеша."""
    order = get_legal_order(legal_order) or _UA
    return stamp_from_cache_entry(
        entry,
        source_url=source_url,
        language=order.authentic_languages[0],
        citation_format=order.citation_style.examples[0],
    )


def provenance(
    entry: dict[str, Any] | None,
    *,
    legal_order: str = "UA",
    source_url: str = "https://zakon.rada.gov.ua",
) -> dict[str, Any]:
    """Describe where an answer came from and how old it is.

    Returned keys are merged straight into tool output. Since T016 this is the
    **whole** envelope, not the five fields it used to be: an output model that
    carries the text of a norm refuses to validate without every one of them,
    which is what "a tool returning a norm without an envelope does not conform"
    means in practice.

    ``freshness_notice`` is kept alongside the contract's ``notice`` and holds
    the same string. Not compatibility for its own sake: the name appears in the
    README, in the shipped skill and in the deployed methodology, and renaming it
    is a documentation change that spans work this phase does not own. One value
    under two names is the smaller lie; the coordinator collapses it when the
    single methodology lands.
    """
    stamp = provenance_stamp(entry, legal_order=legal_order, source_url=source_url)
    envelope = stamp.as_envelope()
    envelope["freshness_notice"] = stamp.notice
    return envelope


# ---------------------------------------------------------------------------
# T015 — маршрутизация по паре «правопорядок + слой покрытия»
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RouteDecision:
    """Куда пошёл запрос — или почему он никуда не пошёл.

    Ровно одно из двух полей заполнено. ``source`` означает «обращайся к
    адаптеру», ``failure`` — «не обращайся, отдай этот отказ». Третьего исхода
    нет: молчаливое обращение к источнику, который не может ответить, и есть
    тот способ выдать непокрытие за пустой результат, который принцип III
    запрещает. ``capability`` — проверенная возможность, по которой выбран
    источник (T120); отсутствует у решений без операции.
    """

    source: Source | None = None
    failure: TypedFailure | None = None
    capability: Capability | None = None

    @property
    def allowed(self) -> bool:
        return self.source is not None

    def as_output(self) -> dict[str, Any] | None:
        """Отказ в виде ответа инструмента, или ``None``, если проходить можно."""
        return None if self.failure is None else self.failure.as_output()


#: Правопорядок, чей канал существует вне контракта ``sources.RegistryAdapter``
#: (``source_adapters.py``, ``rada_open_data.py`` — код, предшествующий этой
#: карте). Карта покрытия обязана его учитывать, не имея записи о нём в
#: реестре адаптеров; сервер регистрирует для него обёртки-читатели
#: (``server.py``), а до их регистрации он считается подключённым кодом.
_LEGACY_CHANNEL_ORDER = "UA"


def _source_is_actually_wired(source: Source) -> bool:
    """Слой в карте — не то же самое, что работающий код.

    Маршрутизация проверяет не только слой, но и то, зарегистрирован ли для
    источника адаптер (по ``adapter_id``, которым коллекция указывает на
    общий читатель, ADR 0002). Украинское ядро подключено кодом, который
    этому контракту не подчиняется, и потому проверяется отдельно.
    """
    from sources import get_adapter  # локальный импорт — реестр адаптеров, не путать с get_source

    if get_adapter(source.effective_adapter_id) is not None:
        return True
    return source.legal_order == _LEGACY_CHANNEL_ORDER


def route(
    legal_order: Any,
    *,
    subject: str = "",
    layers: tuple[CoverageLayer, ...] = (
        CoverageLayer.CONNECTED,
        CoverageLayer.REACHABLE_BY_ID,
    ),
    sources: tuple[Source, ...] | None = None,
    operation: Operation | None = None,
    document_class: DocumentClass = DocumentClass.ANY,
) -> RouteDecision:
    """Решить, доходит ли запрос до адаптера.

    ``layers`` — слои, которых достаточно этому вызову: чтению по
    идентификатору хватает ``reachable_by_id``, поиску нужен ``connected``.
    Без ``operation`` решение принимается по проверенному слою источника;
    с ``operation`` — по проверенной возможности именно этой операции
    (T120, FR-203): источник, у которого чтение проверено, а редакция на дату —
    нет, для ``revision_as_of`` не выбирается.

    ``sources`` подставляется тестами; по умолчанию берётся реестр покрытия.
    Тестовые источники, поданные явно, проверкой «зарегистрирован ли адаптер»
    и проверкой возможностей не связаны — так тест маршрутизации не требует
    боевого адаптера.
    """
    code = normalize_legal_order_code(legal_order)
    if not is_known_legal_order(code):
        return RouteDecision(failure=_unknown_order_failure(code))

    using_default_registry = sources is None
    candidates = _registered_sources_for(code) if sources is None else tuple(sources)

    #: Кандидат нужного слоя без зарегистрированного адаптера: причина отказа
    #: у него другая (адаптер не подключён, а не лицензия или отсутствие
    #: покрытия), и ручной путь обязан называть именно её.
    unwired_match: Source | None = None
    #: Источник, покрытый в целом, но без проверенной возможности этой операции.
    uncovered_operation: Source | None = None
    #: Он же, но объявивший запрошенный вид документа: его ручной путь точнее.
    uncovered_for_class: Source | None = None

    for source in candidates:
        if not using_default_registry:
            if source.layer in layers:
                return RouteDecision(source=source)
            continue

        if operation is None:
            layer = verified_layer(source)
            if layer not in layers:
                continue
            if not _source_is_actually_wired(source):
                unwired_match = unwired_match or source
                continue
            return RouteDecision(source=source)

        matching = [
            cap
            for cap in capabilities_for(code, operation, document_class)
            if cap.source_id == source.id and cap.layer in layers
        ]
        if not matching:
            if verified_layer(source) in layers:
                uncovered_operation = uncovered_operation or source
            # Ручной путь в отказе обязан вести к источнику, который этим видом
            # документа занимается. У правопорядка их несколько (UA: законы,
            # реестр решений, реестр должников, закупки), и без этого выбора
            # запрос судебного решения отправлял бы юриста в реестр должников —
            # формально отказ, по существу дезориентация. Источник, объявивший
            # этот вид документа хоть какой-нибудь операцией, знает о нём
            # больше, даже если сама операция у него не покрыта.
            if uncovered_for_class is None and document_class is not DocumentClass.ANY:
                if any(cap.document_class is document_class for cap in capabilities_of(source.id)):
                    uncovered_for_class = source
            continue
        if not _source_is_actually_wired(source):
            unwired_match = unwired_match or source
            continue
        return RouteDecision(source=source, capability=matching[0])

    if unwired_match is not None:
        manual_path = unwired_match.manual_path or (
            f"Джерело {unwired_match.id} внесене до карти покриття, але програмний адаптер "
            f"до нього не підключено. Першоджерело: {unwired_match.source_url}"
        )
        return RouteDecision(
            failure=NotCovered(
                legal_order=code,
                manual_path=manual_path,
                subject=subject,
                operation=operation.value if operation else "",
                source_id=unwired_match.id,
            )
        )

    uncovered_operation = uncovered_for_class or uncovered_operation
    if uncovered_operation is not None and operation is not None:
        manual_path = uncovered_operation.manual_path or (
            f"Операцію {operation.value} для джерела {uncovered_operation.id} не перевірено. "
            f"Першоджерело: {uncovered_operation.source_url}"
        )
        return RouteDecision(
            failure=NotCovered(
                legal_order=code,
                manual_path=manual_path,
                subject=subject,
                operation=operation.value,
                source_id=uncovered_operation.id,
            )
        )

    manual_path = next(
        (source.manual_path for source in candidates if source.manual_path),
        "",
    )
    if not manual_path:
        # Правопорядок известен, источника нет ни одного, ручного пути тоже.
        # Это не отсутствие данных, а незаполненная карта покрытия, и признать
        # это честнее, чем выдать за «искали и не нашли».
        manual_path = (
            f"Джерело для правопорядку {code} ще не внесене до карти покриття. "
            "Шукайте першоджерело вручну на офіційному порталі цього правопорядку."
        )

    return RouteDecision(
        failure=NotCovered(
            legal_order=code,
            manual_path=manual_path,
            subject=subject,
            operation=operation.value if operation else "",
        )
    )


def _unknown_order_failure(code: str) -> TypedFailure:
    """Отказ ``unknown_legal_order`` как объект, а не как готовый словарь."""
    from core.contracts import UnknownLegalOrder

    payload = unknown_legal_order(code)
    return UnknownLegalOrder(
        message=payload["error"],
        received=payload["details"]["received"],
        known_codes=tuple(payload["details"]["known_codes"]),
    )


class LiveFirstLoader:
    """Loader for ``CacheManager``: live open data first, print-page scraper second.

    Duck-types the slice of :class:`source_adapters.SourceAdapter` that the cache
    layer touches, so it can be handed to ``cache.get_or_fetch`` in place of the
    adapter. Everything it does not fetch itself is delegated to that adapter.
    """

    name = "rada_live"

    def __init__(self, client: Any, fallback: Any) -> None:
        self._client = client
        self._fallback = fallback
        self.source_policy = fallback.source_policy
        # One sentinel for the whole project — scraper.py owns the type.
        self.backoff = fallback.backoff

    # -- the loader contract -------------------------------------------------

    def fetch_law(self, law_id: str, law_info: dict[str, Any]) -> dict[str, Any]:
        payload: dict[str, Any] | None = None
        live_error: Exception | None = None

        try:
            document = self._client.fetch_document(law_id)
        except RadaDocumentNotFound:
            # The catalogue answered: there is no such document. Trying the print
            # page and then Playwright turns that into ~50 seconds ending in
            # "source temporarily unavailable" — slow, and a lie about the source.
            logger.info("rada has no document %s; not trying the print page", law_id)
            raise DocumentNotFound(f"Rada has no document {law_id}") from None
        except RadaOpenDataError as error:
            live_error = error
            logger.info("live rada channel failed for %s: %s", law_id, error)
        else:
            payload = {
                "law_id": law_id,
                "title": str(law_info.get("title") or law_id),
                "url": str(document.get("public_url") or law_info.get("url") or ""),
                "text": str(document.get("text") or ""),
                "source": str(document.get("source") or "rada_open_data"),
                "adapter": self.name,
                "source_policy": str(getattr(self.source_policy, "value", self.source_policy)),
                "retrieved_at": document.get("retrieved_at"),
            }

        if payload is None:
            try:
                payload = dict(self._fallback.fetch_law(law_id, law_info))
            except Exception as error:
                raise SourceAdapterError(
                    f"both Rada channels failed for {law_id}: live={live_error}; print={error}"
                ) from error

        return self.annotate(payload, law_id)

    @staticmethod
    def annotate(payload: dict[str, Any], law_id: str = "") -> dict[str, Any]:
        """Attach the two seam fields the persistence gate reads.

        ``real_title`` replaces the registry placeholder when we managed to read a
        real one; the placeholder is what currently reaches the database.
        """
        text = str(payload.get("text") or "")
        real_title = extract_real_title(text)
        payload["real_title"] = real_title
        payload["doc_kind"] = classify_doc_kind(
            text, law_id=str(law_id or payload.get("law_id") or "")
        )
        current_title = str(payload.get("title") or "")
        if real_title and (not current_title or current_title.startswith("Zakon Rada document")):
            payload["title"] = real_title
        return payload

    # -- delegated to the print-page adapter ---------------------------------

    def resolve(self, query: str) -> dict[str, Any] | None:
        resolved: dict[str, Any] | None = self._fallback.resolve(query)
        return resolved

    def fetch(self, law_id: str, law_info: dict[str, Any]) -> Any:
        return self._fallback.fetch(law_id, law_info)

    def search(self, law_id: str, query: str, max_results: int = 5) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = self._fallback.search(law_id, query, max_results)
        return results

    def metadata(self, law_id: str) -> Any:
        return self._fallback.metadata(law_id)

    def health(self) -> Any:
        return self._fallback.health()


class DocumentMetadataCache:
    """Keeps ``doc_kind``/``real_title`` alive across the file cache.

    ``CacheManager`` rebuilds its entry from a fixed set of keys, so the two seam
    fields would be dropped between the loader and ``repository.store_cache_entry``.
    This wrapper re-derives them from the entry that comes back — both are pure
    functions of the text, so re-deriving is equivalent to carrying them through and
    needs no change inside the cache layer itself.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def get_or_fetch(
        self, law_id: str, scraper: Any, search: Any, force_refresh: bool = False
    ) -> dict[str, Any]:
        entry: dict[str, Any] = self._inner.get_or_fetch(law_id, scraper, search, force_refresh)
        return self._annotated(law_id, entry)

    def get_cached_entry(self, law_id: str, allow_stale: bool = False) -> dict[str, Any] | None:
        entry: dict[str, Any] | None = self._inner.get_cached_entry(law_id, allow_stale=allow_stale)
        return None if entry is None else self._annotated(law_id, entry)

    def get_status(self, law_id: str) -> dict[str, Any]:
        status: dict[str, Any] = self._inner.get_status(law_id)
        return status

    def get_metrics(self) -> dict[str, Any]:
        metrics: dict[str, Any] = self._inner.get_metrics()
        return metrics

    def count_cached_laws(self) -> int:
        return int(self._inner.count_cached_laws())

    @staticmethod
    def _annotated(law_id: str, entry: dict[str, Any]) -> dict[str, Any]:
        if entry.get("real_title") is not None and entry.get("doc_kind"):
            return entry
        result = dict(entry)
        return LiveFirstLoader.annotate(result, law_id)


def search_laws_live(
    client: Any,
    query: str,
    max_results: int = 10,
    in_force_only: bool = True,
) -> dict[str, Any]:
    """Live "find a law" over Rada's full-text search, laws and codes only.

    Without the type filter the answer fills up with Cabinet resolutions: measured
    563 hits unfiltered against 47 filtered for the same query.
    """
    payload: dict[str, Any] = client.search(
        query,
        textl="2",
        types=(TYPE_LAW, TYPE_CODE),
        status=STATUS_IN_FORCE if in_force_only else "",
        max_results=max_results,
    )
    results = []
    for item in payload.get("results", [])[:max_results]:
        results.append(
            {
                "law_id": item.get("law_id", ""),
                "title": item.get("title", ""),
                "status": item.get("status", ""),
                "url": item.get("url", ""),
                "source": "rada_live_search",
            }
        )
    return {
        "query": payload.get("query", query),
        "results": results,
        "found": int(payload.get("found", len(results)) or 0),
        "source": "https://zakon.rada.gov.ua/laws/find/a",
        "source_channel": "live",
        "from_cache": bool(payload.get("from_cache", False)),
        "format": payload.get("format", ""),
        "retrieved_at": str(
            payload.get("retrieved_at") or dt.datetime.now(timezone.utc).isoformat()
        ),
        "freshness_notice": (
            "Живий повнотекстовий пошук Ради, лише Закони та Кодекси, чинні редакції."
        ),
    }


def _normalize_title(value: str) -> str:
    cleaned = re.sub(r"[«»\"'`,.;:()]+", " ", str(value or "").lower())
    cleaned = re.sub(r"^\s*закон(?:\s+україни)?\s+", " ", cleaned)
    return " ".join(cleaned.split())


def pick_title_match(query: str, results: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Choose the hit whose *title* answers the query, or nothing at all.

    Full-text search ranks by where the words occur in the body, so its first hit
    is routinely a different law that merely mentions the subject: asking for
    «Про рейтингування» returned «Про публічно-приватне партнерство» first, measured
    live. Naming the wrong law is worse than admitting we did not find it, so a hit
    is only accepted when its title matches — otherwise the caller falls through.
    """
    wanted = _normalize_title(query)
    if not wanted:
        return None

    best: tuple[int, dict[str, Any]] | None = None
    for item in results:
        title = _normalize_title(str(item.get("title") or ""))
        if not title:
            continue
        if title == wanted:
            score = 3
        elif wanted in title:
            score = 2
        elif title in wanted:
            score = 1
        else:
            continue
        if best is None or score > best[0]:
            best = (score, item)
    return best[1] if best else None


def try_search_laws_live(
    client: Any,
    query: str,
    max_results: int = 10,
) -> dict[str, Any] | None:
    """``search_laws_live`` that answers ``None`` instead of raising.

    Used where the live channel is one of several: an unreachable Rada must degrade
    into an index answer with a marker, never into an exception surfacing as "no
    such law".
    """
    try:
        return search_laws_live(client, query, max_results=max_results)
    except Exception as error:  # noqa: BLE001 - any transport failure degrades to the index
        # The query text is the user's, and user query texts are not logged
        # (FR-029, R-13). The failure type is enough to diagnose a dead channel.
        logger.info("live rada search failed: %s", type(error).__name__)
        return None


# ---------------------------------------------------------------------------
# T121/T123 — единый путь чтения: запрос → маршрут → источник → проверка →
# регистрация доказательства → выдача (ADR 0002, data-model «Состояния чтения»)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReadRequest:
    """Что именно читаем: правопорядок, операция, документ, локатор, язык, дата."""

    legal_order: str
    operation: Operation
    document_class: DocumentClass
    document_id: str
    path: str = ""
    language: str = ""
    as_of: str = ""
    tool: str = ""
    force_refresh: bool = False
    #: Метка роли, читающей в этой сессии (T180). Расходует собственную квоту
    #: памяти доказательств; пусто — читает в общий лимит сессии.
    agent_id: str = ""
    #: Дополнительные структурные параметры операции (query/tokens для локального
    #: поиска внутри документа, paragraph и т. п.). Наружу они не уходят.
    options: Mapping[str, Any] = field(default_factory=dict)


def _binding_scope_fields(code: str) -> dict[str, Any]:
    """Оговорка об обязательности решения международного форума (FR-019, принцип VI)."""
    order = LEGAL_ORDERS.get(code)
    if order is None or order.kind is not LegalOrderKind.INTERNATIONAL_FORUM:
        return {}
    return {
        "binding_scope": "parties_and_this_case_only",
        "binding_scope_notice": (
            f"Рішення {order.name_uk} є обов'язковим лише для сторін і лише у цій справі. "
            "Подавати його як загальнообов'язкове за межами справи не можна."
        ),
    }


def perform_read(
    request: ReadRequest,
    *,
    session_id: str,
    adapters: Mapping[str, Any] | None = None,
    evidence_store: Any = None,
    update_source_cache: bool = True,
) -> dict[str, Any]:
    """Выполнить операцию чтения целиком и вернуть ответ инструмента.

    Состояния: requested → routed → fetched → validated → evidence_registered →
    returned. Ошибка любого обязательного шага исключает выдачу подтверждённого
    текста: отказ маршрута, отказ адаптера, содержимое, не годящееся как
    доказательство (карточка/резюме/неизвестный вид), переполнение памяти
    доказательств — каждое даёт свой типизированный ответ, и никакое не
    подменяется текстом.

    ``adapters`` подставляется тестами; по умолчанию — реестр ``sources``.

    ``update_source_cache=False`` оставляет кеш публичных текстов нетронутым:
    чтение сверяется с ним, но не переписывает его. Это нужно операции, которая
    читает попутно (сверка цитаты): запиши она свой текст в кеш — и следующее
    настоящее чтение сказало бы ``same`` там, где источник изменился, то есть
    изменение нормы исчезло бы молча (D-14).
    """
    from core import citations
    from core.contracts import RevisionUnknown, SourceUnavailable
    from core.legal_orders import capabilities_for as _caps
    from sources import get_adapter

    code = normalize_legal_order_code(request.legal_order)
    decision = route(
        code,
        subject=request.tool,
        operation=request.operation,
        document_class=request.document_class,
    )
    if not decision.allowed or decision.source is None:
        failure = decision.as_output() or {}
        failure.setdefault("legal_order", code)
        return failure
    source = decision.source

    # -- редакция на дату требует отдельно проверенной возможности (ADR 0007) --
    if request.as_of:
        as_of_caps = [
            cap
            for cap in _caps(code, Operation.REVISION_AS_OF, request.document_class)
            if cap.source_id == source.id and cap.layer is not CoverageLayer.NOT_COVERED
        ]
        if not as_of_caps:
            return {
                **RevisionUnknown(
                    document_id=request.document_id,
                    requested_date=request.as_of,
                    known_bounds=(
                        f"джерело {source.id}: редакція на дату не підтримується, "
                        "доступний лише поточний текст; чинну редакцію замість запитаної не підставляємо",
                    ),
                ).as_output(),
                "legal_order": code,
            }

    adapter = (adapters or {}).get(source.effective_adapter_id) if adapters else None
    if adapter is None:
        adapter = get_adapter(source.effective_adapter_id)
    if adapter is None:
        return {
            **NotCovered(
                legal_order=code,
                manual_path=source.manual_path or source.source_url,
                subject=request.tool,
                operation=request.operation.value,
                source_id=source.id,
            ).as_output(),
            "legal_order": code,
        }

    options: dict[str, Any] = {
        **dict(request.options),
        "path": request.path,
        "language": request.language,
        "as_of": request.as_of,
        "force_refresh": request.force_refresh,
        "document_class": request.document_class.value,
    }
    started = dt.datetime.now(timezone.utc)
    result = adapter.fetch(request.document_id, **options)
    if isinstance(result, TypedFailure):
        payload = result.as_output()
        payload["legal_order"] = code
        payload.setdefault("details", {})
        payload["details"].setdefault("source_id", source.id)
        return payload

    data: dict[str, Any] = (
        dict(result.data) if isinstance(result.data, dict) else {"data": result.data}
    )
    stamp: ProvenanceStamp = result.provenance
    text = str(data.get("text") or "")
    updates: dict[str, Any] = {}
    if text and not stamp.content_hash:
        from core.contracts import content_hash as _hash

        updates["content_hash"] = _hash(text)
    if not stamp.fetched_at:
        updates["fetched_at"] = started.isoformat()
    if updates:
        stamp = stamp.model_copy(update=updates)

    locator = str(data.get("locator") or request.path or "")
    resolved_id = str(
        data.get("resolved_document_id") or data.get("document_id") or request.document_id
    )
    confirmable = bool(text) and stamp.confirmable and not stamp.stale
    evidence_id: str | None = None
    store = citations.SESSION_TEXTS if evidence_store is None else evidence_store
    if confirmable:
        try:
            entry = store.remember(
                session_id,
                resolved_id,
                text,
                path=locator,
                source_url=stamp.source_url,
                fetched_at=stamp.fetched_at,
                legal_order=code,
                version_id=stamp.version_id or "",
                language=stamp.language,
                publication_kind=stamp.publication_kind.value,
                content_kind=stamp.content_kind.value,
                locator=locator,
                aliases=tuple(data.get("aliases") or ())
                + ((request.document_id,) if request.document_id != resolved_id else ()),
                # Реквизиты, по которым документ находят в официальном
                # источнике: ECLI, название дела, суд, дата, номер заявления,
                # редакция. Адаптер уже клал их в ``data`` и они уходили в ответ
                # инструмента, но в доказательство не записывались — и печать
                # ссылки их физически не видела, отчего в проект шли «62016CJ0064,
                # 57» вместо ссылки. Берутся по белому
                # списку (``citations.CITATION_META_KEYS``): в юридическую ссылку
                # попадает названное, а не всё, что вернул источник.
                meta=citations.CitationMeta.from_source_data(data),
                # Квитанция чтения (T179): память доказательств принимает только
                # то, что пришло от источника в этом вызове. Единственная точка
                # её выпуска в продукте — эта строка.
                receipt=citations.issue_receipt(stamp),
                agent_id=request.agent_id,
            )
        except citations.EvidenceCapacityExceeded as exhausted:
            return {
                **SourceUnavailable(
                    source=source.id,
                    retry_after="після завершення поточної сесії або її частини",
                    reason="evidence_capacity",
                    message=str(exhausted),
                ).as_output(),
                "legal_order": code,
            }
        evidence_id = entry.evidence_id

    # D-14, FR-530. Публичный текст переживает процесс: он кладётся в кеш по
    # ``content_hash`` и адресуется редакцией и языковой версией. Границу
    # доказательства это не двигает — ``evidence_id`` уже выдан выше памятью
    # **этого** сеанса. Кеш отвечает на другой вопрос: тот ли это текст, что
    # был в прошлый раз. Совпадение делает сверку мгновенной, расхождение —
    # отдельный видимый факт, а не тихое обновление.
    cache_state = "unavailable"
    previous_hash = ""
    if confirmable and text:
        key = source_cache.source_key(
            legal_order=code,
            document_id=resolved_id,
            path=locator,
            version_id=stamp.version_id or "",
            language=stamp.language,
        )
        try:
            comparison = source_cache.compare_with_cached(key, text)
            cache_state = str(comparison.get("state") or "unknown")
            previous_hash = str(comparison.get("previous_hash") or "")
            if update_source_cache:
                source_cache.remember_source(
                    key,
                    str(comparison.get("content_hash") or ""),
                    text,
                    session_id=session_id,
                    source_url=stamp.source_url,
                )
        except (OSError, ValueError):
            # Каталог кеша недоступен — это отказ машины юриста, а не повод не
            # отдать прочитанное: чтение состоялось, доказательство выдано.
            cache_state = "unavailable"

    output: dict[str, Any] = {
        "legal_order": code,
        "document_id": request.document_id,
        "resolved_document_id": resolved_id,
        "source_id": source.id,
        "title": str(data.get("title") or resolved_id),
        "path": locator or None,
        "locator": locator,
        "text": text,
        "url": str(data.get("url") or stamp.source_url),
        "source": source.id,
        "from_cache": bool(data.get("from_cache", stamp.source_channel.value == "index")),
        "retrieved_at": stamp.fetched_at or started.isoformat(),
        "evidence_id": evidence_id,
        "confirmable": confirmable,
        #: Что кеш публичных текстов знает об этом адресе: ``same`` — текст тот
        #: же, что в прошлом сеансе (сверка мгновенна), ``changed`` — источник
        #: изменился, ``unknown`` — адрес встречается впервые,
        #: ``unavailable`` — кеша нет. Названо словом, а не булевым флагом:
        #: «источник изменился» и «мы его раньше не видели» — разные вещи, и
        #: одна из них требует внимания юриста (D-14).
        "cache_state": cache_state,
        "previous_content_hash": previous_hash,
        # Текст, не годящийся как доказательство (карточка, резюме, устаревшая
        # копия), всё равно выдаётся — но с прямым признаком того, чем он
        # является: адресом для следующего чтения, а не основанием цитаты
        # (T181). Без флага агент отличал бы одно от другого только по
        # отсутствию evidence_id, то есть по умолчанию — не отличал бы.
        "navigation_only": not confirmable,
        **{
            k: v
            for k, v in data.items()
            if k not in ("text", "title", "url", "from_cache", "locator", "aliases")
        },
        **stamp.as_envelope(),
        **_binding_scope_fields(code),
    }
    output["freshness_notice"] = stamp.notice
    return output


# ---------------------------------------------------------------------------
# T187 — план задачи: что достижимо по каждому вопросу права
# ---------------------------------------------------------------------------
