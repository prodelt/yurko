"""Yurko MCP server v2.4 — публичное право с проверяемой ссылкой.

Узкие MCP-действия поверх единого пути чтения (:func:`routing.perform_read`):
запрос → маршрут по проверенной возможности → официальный источник → проверка
полноты/происхождения → регистрация фрагмента в памяти сессии → выдача.
Украинское ядро (Рада, ЄДРСР, реестры) обёрнуто в тот же контракт адаптера,
поэтому ни один правопорядок не проваливается в чужой обработчик (T121).

Границы, которые держит этот модуль:

* ``legal_order`` у каждого действия, инструменты по странам не размножаются
  (принцип VII);
* транспортная сессия MCP — контекст доказательств; пользовательского
  ``session_id`` в аргументах нет (T122);
* свободный текст пользователя не уходит источникам в юридическом профиле
  (:mod:`yurko_profile`, ADR 0009), исходящий трафик ограничен белым списком
  официальных хостов (:mod:`egress`);
* журналы — только агрегаты (FR-220).
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import re
import sys
import threading
import time
from datetime import timezone
from pathlib import Path
from typing import Any

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse


from core import citations
from core import egress
from core import legal_orders
from core import quote_check
from core import source_cache
import sources
from core import yurko_profile
from registries.cache_manager import CacheManager
from core.contracts import (
    CONTRACT_VERSION,
    CaseCardOutput,
    DecisionOutput,
    DiscoverLawsInput,
    DiscoverLawsOutput,
    DiscoverRegistriesInput,
    DiscoverRegistriesOutput,
    DocumentInput,
    ErrorCode,
    GetArticleOutput,
    GetCaseInput,
    GetDecisionInput,
    GetFragmentInput,
    GetFragmentsInput,
    GetMultipleArticlesOutput,
    GetTenderInput,
    LawMetadataOutput,
    ListLawsInput,
    ListLawsOutput,
    ListRegistriesOutput,
    NotCovered,
    NotFound,
    QueryDocumentInput,
    QueryLawOutput,
    ResolveLawInput,
    ResolveLawOutput,
    SearchAcrossLawsInput,
    SearchAcrossLawsOutput,
    SearchArticlesInput,
    SearchArticlesOutput,
    SearchDebtorsInput,
    SearchDebtorsOutput,
    SearchDecisionsInput,
    SearchDecisionsOutput,
    SearchTendersInput,
    SearchTendersOutput,
    SourceAdapterError,
    SourceRecordNotFound,
    SourceRequiresHuman,
    SourceHealth,
    SourcePolicy,
    SourceUnavailable,
    TenderOutput,
    ToolErrorOutput,
    TypedFailure,
    UnsupportedFilter,
    content_hash,
    dump_tool_output,
    friendly_error,
    is_failure,
    parse_tool_input,
    unknown_legal_order,
    VerifyQuoteInput,
    VerifyQuoteOutput,
)
from registries.court_registry import CourtDecisionsRegistry
from registries.law_registry import LawRegistry
from core.legal_orders import (
    LEGAL_ORDERS,
    DocumentClass,
    Operation,
    Source,
    is_known_legal_order,
    normalize_legal_order_code,
)
from registries.open_data_discovery import OpenDataDiscovery
from core.provenance import (
    ContentKind,
    ProvenanceStamp,
    PublicationKind,
    SourceChannel,
    stamp_from_cache_entry,
)
from registries.rada_open_data import RadaOpenDataClient
from core.routing import (
    DocumentMetadataCache,
    DocumentNotFound,
    LiveFirstLoader,
    ReadRequest,
    perform_read,
    pick_title_match,
    route,
    try_search_laws_live,
)

from search import local_index
from search.search_engine import SearchEngine
from core.session_context import current_session_id
from registries.source_adapters import ZakonRadaAdapter
from sources.base import AdapterPayload, AdapterResult, RegistryAdapter
from registries.state_registries import DebtorsRegistry, OpenDataCatalog, ProzorroRegistry

__version__ = "2.4.0"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stderr,
)
logger = logging.getLogger("ukraine-laws")

# Оточення приводиться до профілю **до** читання прапорців: інакше
# ``USE_PG_BACKEND`` встиг би прочитатися зі старого значення, і вимикати його
# було б уже нічому (T480). Кожна дія називається в журналі вголос — мовчазне
# приведення було б гіршим за падіння.
for _conformed in yurko_profile.conform_environment():
    logging.getLogger("ukraine-laws").warning("profile conform: %s", _conformed)

TRANSPORT = os.getenv("MCP_TRANSPORT", "stdio")
API_KEY = os.getenv("UKRAINE_LAWS_API_KEY", "")
PORT = int(os.getenv("PORT", "8000"))
USE_PG_BACKEND = os.getenv("USE_PG_BACKEND", "0").strip().lower() in ("1", "true", "yes")
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
STARTED_AT = dt.datetime.now(timezone.utc)

# Профиль обработки (T125, ADR 0009): нарушения фиксируются при импорте,
# блокируют запуск в main(); /metrics и list_coverage показывают их.
PROFILE_VIOLATIONS: list[str] = yurko_profile.validate_environment()
if PROFILE_VIOLATIONS and yurko_profile.current_profile() is yurko_profile.Profile.LEGAL:
    for _violation in PROFILE_VIOLATIONS:
        logger.warning("legal profile violation (start will be refused): %s", _violation)

# Сторож исходящего трафика ставится один раз на процесс (T125).
for _extra_host in os.getenv("YURKO_EGRESS_EXTRA_HOSTS", "").split(","):
    egress.allow_host(_extra_host)
egress.install_guard()

BASE_DIR = Path(__file__).parent

# Реєстр законів — дані поставки, а не кеш: читається з теки коду й ніколи не
# пишеться. Усе, що сервер пише під час роботи, лежить у ``source_cache.cache_dir()``
# (каталог ОС; ``YURKO_CACHE_DIR`` перекриває), а не в теці коду (тікет 51, № 22).
LAWS_INDEX_PATH = BASE_DIR / "cache" / "laws.json"
registry = LawRegistry(LAWS_INDEX_PATH)
search_engine = SearchEngine()
source_adapter = ZakonRadaAdapter(registry, search_engine)
open_data_discovery = OpenDataDiscovery(
    source_cache.registry_cache_dir(source_cache.OPEN_DATA_CACHE_SUBDIR)
)
court_registry = CourtDecisionsRegistry(
    source_cache.registry_cache_dir(source_cache.COURT_DECISIONS_CACHE_SUBDIR)
)
debtors_registry = DebtorsRegistry()
open_data_catalog = OpenDataCatalog()
prozorro_registry = ProzorroRegistry()

# The live channel is the primary one; the index is a hot cache filled by demand.
rada_client = RadaOpenDataClient(source_cache.registry_cache_dir(source_cache.RADA_CACHE_SUBDIR))
live_loader = LiveFirstLoader(rada_client, source_adapter)
cache: Any = DocumentMetadataCache(
    CacheManager(source_cache.registry_cache_dir(source_cache.LAWS_CACHE_SUBDIR), registry)
)

# Optional Postgres backend behind a feature flag — только собственная база
# (локальный хост либо YURKO_OWN_DATABASE=1); hosted-путь в legal-профиле не
# инициализируется вовсе (ADR 0009).
hybrid: Any = None
_pg_repo: Any = None
_pg_status = "disabled"
_pg_error: str | None = None
if USE_PG_BACKEND and DATABASE_URL:
    if any("hosted database" in violation for violation in PROFILE_VIOLATIONS) and (
        yurko_profile.current_profile() is yurko_profile.Profile.LEGAL
    ):
        _pg_status = "blocked_by_profile"
        logger.warning("Postgres backend not initialised: hosted DATABASE_URL in legal profile")
    else:
        try:
            from search.embeddings import make_embedder
            from search.hybrid_search import HybridSearch
            from storage.repository import PostgresRepository, RepositoryCacheAdapter

            _embedder = make_embedder()
            _pg_repo = PostgresRepository(DATABASE_URL, embedder=_embedder)
            cache = RepositoryCacheAdapter(_pg_repo, cache)
            hybrid = HybridSearch(_pg_repo, _embedder)
            _pg_status = "active"
            logger.info("Postgres backend enabled (USE_PG_BACKEND=1, embedder=%s)", _embedder.model)
        except Exception as error:  # pragma: no cover - defensive
            _pg_status = "init_failed"
            _pg_error = type(error).__name__
            logger.warning("Postgres backend init failed, using file cache: %s", error)
elif USE_PG_BACKEND and not DATABASE_URL:
    _pg_status = "no_database_url"
    logger.warning("USE_PG_BACKEND=1 but DATABASE_URL is unset; using file cache")

mcp = FastMCP("yurko", version=__version__)

_tool_metrics_lock = threading.Lock()
_tool_metrics: dict[str, Any] = {
    "calls": 0,
    "errors": 0,
    "total_latency_ms": 0.0,
    "db_fetch_bytes": 0,
    "per_tool": {},
}


def _record_tool_metric(
    tool_name: str, elapsed_ms: float, success: bool, db_bytes: int = 0
) -> None:
    with _tool_metrics_lock:
        _tool_metrics["calls"] += 1
        _tool_metrics["total_latency_ms"] += elapsed_ms
        _tool_metrics["db_fetch_bytes"] += db_bytes
        if not success:
            _tool_metrics["errors"] += 1

        per_tool = _tool_metrics["per_tool"].setdefault(
            tool_name, {"calls": 0, "errors": 0, "total_latency_ms": 0.0, "db_fetch_bytes": 0}
        )
        per_tool["calls"] += 1
        per_tool["total_latency_ms"] += elapsed_ms
        per_tool["db_fetch_bytes"] += db_bytes
        if not success:
            per_tool["errors"] += 1


def _tool_metrics_snapshot() -> dict[str, Any]:
    with _tool_metrics_lock:
        calls = int(_tool_metrics["calls"])
        errors = int(_tool_metrics["errors"])
        total_latency = float(_tool_metrics["total_latency_ms"])
        db_fetch_bytes = int(_tool_metrics.get("db_fetch_bytes", 0))
        raw_per_tool = dict(_tool_metrics["per_tool"])

    per_tool: dict[str, Any] = {}
    for name, data in raw_per_tool.items():
        tool_calls = int(data["calls"])
        avg_latency = data["total_latency_ms"] / tool_calls if tool_calls else 0.0
        per_tool[name] = {
            "calls": tool_calls,
            "errors": int(data["errors"]),
            "avg_latency_ms": round(avg_latency, 2),
            "db_fetch_bytes": int(data.get("db_fetch_bytes", 0)),
        }

    avg_latency = total_latency / calls if calls else 0.0
    return {
        "calls": calls,
        "errors": errors,
        "avg_latency_ms": round(avg_latency, 2),
        "db_fetch_bytes": db_fetch_bytes,
        "per_tool": per_tool,
    }


def _metrics_payload() -> dict[str, Any]:
    cache_metrics = cache.get_metrics()
    tool_metrics = _tool_metrics_snapshot()
    sources_health = {
        adapter.name: adapter.health().model_dump(mode="json")
        for adapter in (
            source_adapter,
            court_registry,
            debtors_registry,
            open_data_catalog,
            prozorro_registry,
        )
    }
    avg_latency_ms = tool_metrics.get("avg_latency_ms", 0.0)

    pg_stats: dict[str, Any] = {}
    if USE_PG_BACKEND and _pg_repo is not None:
        try:
            pg_stats = _pg_repo.stats()
        except Exception:
            pass

    return {
        "server_version": __version__,
        "version": __version__,
        "schema_version": CONTRACT_VERSION,
        "transport": TRANSPORT,
        "profile": yurko_profile.profile_summary(),
        "profile_violations": list(PROFILE_VIOLATIONS),
        "backend": cache_metrics.get("backend", "file"),
        "pg_backend": _pg_status,
        "pg_error": _pg_error,
        "laws_total": len(registry.list_laws()),
        "cache_hits": cache_metrics.get("hits", 0),
        "cache_misses": cache_metrics.get("misses", 0),
        "avg_latency_ms": avg_latency_ms,
        "cached_laws": cache_metrics.get("cached_laws", 0),
        "uptime_minutes": round(
            (dt.datetime.now(timezone.utc) - STARTED_AT).total_seconds() / 60, 2
        ),
        "cache": cache_metrics,
        "tools": tool_metrics,
        "sources": sources_health,
        # Aggregates only: no query text anywhere in this payload (FR-220).
        "source_calls": _source_metrics_snapshot(),
        "evidence_sessions": len(citations.SESSION_TEXTS.sessions()),
        "tool_fingerprint": globals().get("TOOL_FINGERPRINT", {}),
        "pg_stats": pg_stats,
    }


if API_KEY:
    from fastmcp.exceptions import ToolError
    from fastmcp.server.dependencies import get_http_headers, get_http_request
    from fastmcp.server.middleware import Middleware, MiddlewareContext

    class ApiKeyAuth(Middleware):
        async def on_call_tool(self, context: MiddlewareContext, call_next):
            try:
                request = get_http_request()
            except Exception:
                request = None

            if request is None:
                return await call_next(context)

            headers = get_http_headers() or {}
            api_key = headers.get("x-api-key") or request.query_params.get("api_key", "")
            if api_key != API_KEY:
                raise ToolError("Unauthorized: invalid or missing API key")
            return await call_next(context)

    mcp.add_middleware(ApiKeyAuth())
    logger.info("HTTP API key auth enabled")


@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> PlainTextResponse:
    return PlainTextResponse("OK")


@mcp.custom_route("/metrics", methods=["GET"])
async def metrics(_: Request) -> JSONResponse:
    return JSONResponse(_metrics_payload())


@mcp.custom_route("/", methods=["GET"])
async def root(_: Request) -> JSONResponse:
    payload = _metrics_payload()
    return JSONResponse(
        {
            "name": "yurko",
            "version": __version__,
            "schema_version": CONTRACT_VERSION,
            "mcp_endpoint": "/mcp",
            "auth_required": bool(API_KEY),
            "profile": payload["profile"],
            "laws_total": payload["laws_total"],
            "cache": payload["cache"],
        }
    )


# ---------------------------------------------------------------------------
# Общие помощники: сессия, отказы, профиль
# ---------------------------------------------------------------------------


def _session() -> str:
    return current_session_id()


def _with_order(payload: dict[str, Any], legal_order: str) -> dict[str, Any]:
    payload.setdefault("legal_order", legal_order)
    payload.setdefault("schema_version", CONTRACT_VERSION)
    return payload


def _free_text_refusal(legal_order: str, source_id: str, tool_name: str) -> dict[str, Any] | None:
    """Отказ на свободный текст в юридическом профиле (ADR 0009).

    ``None`` — отправлять можно (инженерный профиль). Иначе ``not_covered`` с
    операцией ``search_free_text`` и официальным ручным путём: строка запроса
    источнику не уходит, локального покрытия для неё нет.
    """
    if yurko_profile.free_text_egress_allowed():
        return None
    source = legal_orders.get_source(source_id)
    manual = (
        (source.manual_path or source.source_url)
        if source is not None
        else "офіційний портал правопорядку вручну"
    )
    return _with_order(
        NotCovered(
            legal_order=legal_order,
            manual_path=(
                f"Пошук за вільним текстом у профілі legal не виконується: рядок запиту "
                f"джерелу не надсилається. Використовуйте ідентифікатор документа, "
                f"номер справи або код, чи шукайте вручну: {manual}"
            ),
            subject=tool_name,
            operation=Operation.SEARCH_FREE_TEXT.value,
            source_id=source_id,
        ).as_output(),
        legal_order,
    )


def _as_navigation(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Пометить выдачу поиска как навигацию, а не как доказательства (T181).

    Сниппет собирается нами из текста и обрезается по длине; доказательством
    является только то, что зарегистрировано при чтении. Пометка стоит на
    каждой записи, потому что до модели доходит запись, а не ответ целиком.
    """
    stamped: list[dict[str, Any]] = []
    for item in results:
        entry = dict(item)
        entry["navigation_only"] = True
        entry["evidence_id"] = None
        entry["confirmable"] = False
        stamped.append(entry)
    return stamped


def _resolve_law(law_id: str) -> dict[str, Any] | None:
    law = registry.get_law(law_id)
    if law is not None:
        return law
    try:
        return _resolve_from_open_data(law_id)
    except Exception:
        return None


def _safe_token_limit(tokens: int) -> int:
    return tokens * 4


def _resolve_from_open_data(query: str) -> dict[str, Any] | None:
    discovered = open_data_discovery.resolve_best(query)
    if discovered is None:
        return None
    return {
        "id": discovered["law_id"],
        "title": discovered["title"],
        "url": discovered["url"],
        "print_url": discovered["print_url"],
        "cache_ttl_days": 7,
        "discovered": True,
        "source": discovered["source"],
        "accepted_at": discovered.get("accepted_at"),
        "updated_at": discovered.get("updated_at"),
    }


def _live_channel_enabled() -> bool:
    """Whether the live Rada full-text *search* may be used in this process.

    Operator switch ``RADA_LIVE_CHANNEL=0`` turns it off; the legal profile
    turns it off regardless, because the search string would leave the machine
    (ADR 0009). Reading a document by id is a different channel and stays live.
    """
    if not yurko_profile.free_text_egress_allowed():
        return False
    return os.getenv("RADA_LIVE_CHANNEL", "1").strip().lower() not in ("0", "false", "no")


def _live_search(query: str, max_results: int) -> dict[str, Any] | None:
    if not _live_channel_enabled():
        return None
    return try_search_laws_live(rada_client, query, max_results=max_results)


def _resolve_from_live_search(query: str) -> dict[str, Any] | None:
    payload = _live_search(query, max_results=10)
    if not payload:
        return None
    item = pick_title_match(query, payload.get("results", []))
    law_id = str((item or {}).get("law_id") or "").strip()
    if not item or not law_id:
        return None
    return {
        "id": law_id,
        "title": item.get("title") or law_id,
        "url": item.get("url", f"https://zakon.rada.gov.ua/laws/show/{law_id}"),
        "print_url": f"https://zakon.rada.gov.ua/laws/show/{law_id}/print",
        "cache_ttl_days": 7,
        "discovered": True,
        "source": "rada_live_search",
        "source_channel": "live",
    }


def _display_title(cached: dict[str, Any], law: dict[str, Any], fallback: str) -> str:
    if law.get("discovered"):
        return str(law.get("title", fallback))
    return str(cached.get("title", law.get("title", fallback)))


# ---------------------------------------------------------------------------
# Украинское ядро как адаптеры контракта (T121): Рада и ЄДРСР
# ---------------------------------------------------------------------------

_UA = LEGAL_ORDERS["UA"]
_UA_ID_HINT = "ідентифікатор акта за реєстром Ради, напр. 435-15, 922-19, 1178-2022-п"


class UaRadaReader(RegistryAdapter):
    """Читатель законодательства України поверх существующего живого канала.

    Обёртка, а не переписывание: кеш, живой канал и разбор статей остаются
    прежними; здесь они получают контракт адаптера, конверт и вид содержимого,
    чтобы единый путь чтения регистрировал ровно то, что отдано.
    """

    source_id = "ua_rada_open_data"
    id_format_hint = _UA_ID_HINT
    legal_order = "UA"
    source_policy = SourcePolicy.OPEN_DATA_DUMP

    def policy(self) -> Source:
        source = legal_orders.get_source(self.source_id)
        assert source is not None
        return source

    def _stamp(
        self, cached: dict[str, Any], *, content_kind: ContentKind, path: str
    ) -> ProvenanceStamp:
        return stamp_from_cache_entry(
            cached,
            source_url=str(cached.get("url") or "https://zakon.rada.gov.ua"),
            language="uk",
            citation_format=(
                f"стаття {path} акта № {cached.get('law_id', '')}"
                if path
                else _UA.citation_style.examples[0]
            ),
            publication_kind=PublicationKind.CONSOLIDATED,
            content_kind=content_kind,
            version_id=str(cached.get("amendment_date") or "") or None,
        )

    def fetch(self, document_id: str, **options: Any) -> AdapterResult:
        language = str(options.get("language") or "")
        if language and language != "uk":
            return NotCovered(
                legal_order="UA",
                manual_path=(
                    "zakon.rada.gov.ua публікує акти українською; офіційні переклади не "
                    "надаються цим каналом"
                ),
                subject=document_id,
                operation=f"read_document:language={language}",
                source_id=self.source_id,
            )
        law = _resolve_law(document_id)
        if law is None:
            return NotFound(identifier=document_id, id_format_hint=_UA_ID_HINT)
        resolved_id = str(law["id"])
        path = str(options.get("path") or "").strip()
        query = str(options.get("query") or "").strip()
        context_chars = _safe_token_limit(int(options.get("tokens") or 5000))
        try:
            # SQL-level slice for a whole-document read on the own Postgres backend;
            # a miss MUST fall through to the network path (ticket 14 regression).
            cached = None
            if USE_PG_BACKEND and hasattr(cache, "get_current_limited") and not query and not path:
                cached = cache.get_current_limited(resolved_id, context_chars)
            if cached is None:
                cached = cache.get_or_fetch(
                    resolved_id, live_loader, search_engine, bool(options.get("force_refresh"))
                )
        except DocumentNotFound:
            return NotFound(identifier=document_id, id_format_hint=_UA_ID_HINT)
        except SourceAdapterError:
            return SourceUnavailable(source=self.source_id, retry_after="через 15 хвилин")

        articles = cached.get("articles") or search_engine.parse_articles(cached.get("text", ""))
        all_text = str(cached.get("text") or "")
        title = _display_title(cached, law, document_id)
        base = {
            "resolved_document_id": resolved_id,
            "title": title,
            "url": str(cached.get("url") or law.get("url") or ""),
            "from_cache": bool(cached.get("from_cache", False)),
            "doc_kind": cached.get("doc_kind"),
            "articles_count": len(articles),
            "full_text_chars": len(all_text),
        }
        if path:
            matched = search_engine.fuzzy_match_article(path, articles)
            if not matched:
                available = sorted(articles.keys(), key=lambda k: (len(k), k))
                return NotFound(
                    identifier=f"{document_id}#{path}",
                    id_format_hint=(
                        f"номер статті/пункту в межах акта; наявних одиниць {len(available)}"
                    ),
                )
            article_num, article_text = matched
            return AdapterPayload(
                data={**base, "text": article_text, "locator": article_num},
                provenance=self._stamp(cached, content_kind=ContentKind.FRAGMENT, path=article_num),
            )

        extra: dict[str, Any] = {}
        if query:
            # ``search_in_text`` возвращает пустую строку, когда совпадения нет.
            # До T248 он отдавал начало документа, и оно уходило наружу как
            # «фрагмент, отвечающий на запрос»: текст подлинный, утверждение
            # ложное. Начало акта показывается по-прежнему — читателю оно
            # полезно, — но названо тем, что оно есть.
            snippet = search_engine.search_in_text(
                all_text, query, context_chars=context_chars, articles=articles
            )
            query_matched = bool(snippet)
            if not query_matched:
                snippet = all_text[:context_chars]
                extra["query_notice"] = (
                    "Запит у тексті акта не знайдено. Нижче — початок документа, "
                    "а не відповідь на запит: цитувати це як відповідь не можна. "
                    f"Одиниць для цитування в акті: {len(articles)}; "
                    "звузьте запит або візьміть статтю через get_article."
                )
            extra["query_matched"] = query_matched
            if query_matched or len(snippet) < len(all_text):
                kind = ContentKind.FRAGMENT
            else:
                kind = ContentKind.FULL_TEXT
        else:
            snippet = all_text[:context_chars]
            kind = ContentKind.FRAGMENT if len(snippet) < len(all_text) else ContentKind.FULL_TEXT
        return AdapterPayload(
            data={**base, "text": snippet, "locator": "", **extra},
            provenance=self._stamp(cached, content_kind=kind, path=""),
        )

    def search(self, query: str, **options: Any) -> AdapterResult:
        return NotCovered(
            legal_order="UA",
            manual_path=self.policy().manual_path or "https://zakon.rada.gov.ua",
            subject=query,
            operation="search",
            source_id=self.source_id,
        )

    def card(self, document_id: str, **options: Any) -> AdapterResult:
        law = _resolve_law(document_id)
        if law is None:
            return NotFound(identifier=document_id, id_format_hint=_UA_ID_HINT)
        resolved_id = str(law["id"])
        status = cache.get_status(resolved_id)
        source_metadata = source_adapter.metadata(resolved_id)
        entry = cache.get_cached_entry(resolved_id, allow_stale=True) or {}
        stamp = stamp_from_cache_entry(
            {**entry, "from_cache": True} if entry else None,
            source_url=str(law.get("url") or "https://zakon.rada.gov.ua"),
            language="uk",
            citation_format=_UA.citation_style.examples[0],
            publication_kind=PublicationKind.CARD,
            content_kind=ContentKind.METADATA,
        )
        return AdapterPayload(
            data={
                "resolved_document_id": resolved_id,
                "title": entry.get("real_title") or law.get("title", document_id),
                "url": str(law.get("url") or ""),
                "ttl_days": law.get("cache_ttl_days", 30),
                "volatile": registry.is_volatile(resolved_id),
                "cached": status.get("cached", False),
                "fresh": status.get("fresh", False),
                "cached_at": status.get("cached_at"),
                "amendment_date": entry.get("amendment_date"),
                "content_hash": entry.get("content_hash"),
                "adapter": entry.get("adapter", source_adapter.name),
                "char_count": status.get("char_count", 0),
                "articles_count": len(entry.get("articles", {})),
                "doc_kind": entry.get("doc_kind"),
                "in_backoff": source_metadata.backoff.get("blocked", False),
                "retry_after": source_metadata.backoff.get("retry_after"),
            },
            provenance=stamp,
        )

    def health(self) -> SourceHealth:
        return source_adapter.health()


class UaCourtReader(RegistryAdapter):
    """ЄДРСР поверх существующего реестра: чтение по id, поиск по номеру дела."""

    source_id = "ua_court_register_edrsr"
    legal_order = "UA"
    source_policy = SourcePolicy.HTML_PRINT
    supports_date_filter = False

    def policy(self) -> Source:
        source = legal_orders.get_source(self.source_id)
        assert source is not None
        return source

    def fetch(self, document_id: str, **options: Any) -> AdapterResult:
        try:
            decision = court_registry.get_decision(document_id)
        except ValueError:
            return NotFound(
                identifier=document_id,
                id_format_hint="числовий id рішення з посилання reyestr.court.gov.ua/Review/<id>",
            )
        except SourceAdapterError:
            return SourceUnavailable(source=self.source_id, retry_after="через 15 хвилин")
        text = str(decision.get("text") or "")
        stamp = stamp_from_cache_entry(
            decision,
            source_url=str(decision.get("url") or court_registry.name),
            language="uk",
            citation_format=f"рішення ЄДРСР № {document_id}",
            publication_kind=PublicationKind.COURT_PUBLICATION,
            content_kind=ContentKind.FULL_TEXT,
        )
        return AdapterPayload(
            data={
                "resolved_document_id": document_id,
                "title": f"Рішення ЄДРСР {document_id}",
                "url": decision.get("url", ""),
                "text": text,
                "from_cache": bool(decision.get("from_cache", False)),
                "char_count": len(text),
                "material_kind": "court_text",
                "locator": "",
            },
            provenance=stamp,
        )

    id_format_hint = "числовий id рішення з посилання reyestr.court.gov.ua/Review/<id>"

    #: Номер справи ЄДРСР: «NNN/NNNNN/YY», «2-к/759/12/23» — цифри, кирилиця,
    #: скісні риски й дефіси, без пробілів. Загальний шаблон ідентифікатора
    #: кирилицю не пропускає, тому форум оголошує свою форму (T182).
    case_number_pattern = re.compile(r"^[0-9А-Яа-яІЇЄҐіїєґ][0-9А-Яа-яІЇЄҐіїєґ/\-.]{2,63}$")

    def search(self, query: str, **options: Any) -> AdapterResult:
        case_number = str(options.get("case_number") or "").strip()
        max_results = int(options.get("max_results") or 10)
        try:
            result = court_registry.search(query, case_number, max_results)
        except SourceAdapterError as error:
            return SourceUnavailable(
                source=self.source_id,
                retry_after="через 15 хвилин",
                message=f"ЄДРСР тимчасово недоступний або повернув антибот-заглушку: {type(error).__name__}",
            )
        stamp = ProvenanceStamp(
            source_channel=SourceChannel.LIVE,
            source_url=str(result.get("source") or court_registry.name),
            language="uk",
            citation_format="рішення ЄДРСР № <id>",
            stale=False,
            is_authentic_version=True,
            is_translation=False,
            publication_kind=PublicationKind.CARD,
            content_kind=ContentKind.METADATA,
        )
        return AdapterPayload(data=result, provenance=stamp)

    def health(self) -> SourceHealth:
        return court_registry.health()


sources.register_adapter(UaRadaReader())
sources.register_adapter(UaCourtReader())


# ---------------------------------------------------------------------------
# Чтение нормативных документов (query_law / get_article / get_multiple_articles / get_law_metadata)
# ---------------------------------------------------------------------------


def _read(
    tool_name: str,
    legal_order: str,
    document_id: str,
    *,
    operation: Operation,
    document_class: DocumentClass = DocumentClass.ACT,
    path: str = "",
    language: str = "",
    as_of: str = "",
    force_refresh: bool = False,
    agent_id: str = "",
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    request = ReadRequest(
        legal_order=legal_order,
        operation=operation,
        document_class=document_class,
        document_id=document_id,
        path=path,
        language=language,
        as_of=as_of,
        tool=tool_name,
        force_refresh=force_refresh,
        agent_id=agent_id,
        options=dict(options or {}),
    )
    return perform_read(request, session_id=_session())


def _document_class_for(legal_order: str, document_id: str) -> DocumentClass:
    """Процессуальные акты Суда ЄС — класс procedure (ADR 0006), остальное — act.

    Вид документа выбирает источник внутри правопорядка: когда у него несколько
    источников, документ обязан попасть к тому, кто его публикует. Без этого
    различения запрос уходил бы не в тот реестр и возвращал «не найдено» —
    отказ, который выглядит как отсутствие документа, хотя документ существует
    и читается (ADR 0003).
    """
    if legal_order == "INTL" or document_id.lower().startswith("treaty:"):
        return DocumentClass.TREATY
    if legal_order == "EU" and re.match(
        r"^(12016E/PRO/03|1\d{4}E|32012Q|02012Q|32024Y|32024D2490)", document_id
    ):
        return DocumentClass.PROCEDURE
    if legal_order == "EU" and re.match(
        # ELI процесуальних правил Суду (``…/eli/proc_rules/2024/2173/oj``), тікет 21.
        r"^(?:https?://[^/]+/(?:resource/)?)?eli/proc_rules/",
        document_id.strip(),
        re.IGNORECASE,
    ):
        return DocumentClass.PROCEDURE
    return DocumentClass.ACT


@mcp.tool()
def get_article(
    legal_order: str,
    document_id: str,
    path: str,
    language: str = "",
    as_of: str = "",
    force_refresh: bool = False,
    agent_id: str = "",
) -> dict[str, Any]:
    """Fetch the text of one citable unit (article/paragraph) from the source.

    Returns the unit's text with its locator, the resolved document id and the
    source envelope (url, language, retrieval time), plus an ``evidence_id``
    naming this reading in the session's text cache so the same text can be
    re-read cheaply. Nothing is written outside the process.

    ``document_id`` — the source's identifier (CELEX 32016R0679, Rada id
    435-15, ECLI); ``path`` — the unit's address (2(1),
    5a, 637); ``language`` — requested language version (empty = authentic);
    ``as_of`` — YYYY-MM-DD for the revision in force on that date (only where a
    verified revision resolver exists; otherwise ``revision_unknown``);
    ``agent_id`` — optional label of the reading role, which gets its own share
    of the session cache so one reader cannot exhaust it for the others.
    """
    started = time.perf_counter()
    success = False
    try:
        params = parse_tool_input(
            GetFragmentInput,
            legal_order=legal_order,
            document_id=document_id,
            path=path,
            language=language,
            as_of=as_of,
            force_refresh=force_refresh,
            agent_id=agent_id,
        )
        if isinstance(params, dict):
            success = True
            return params
        result = _read(
            "get_article",
            params.legal_order,
            params.document_id,
            operation=Operation.READ_FRAGMENT,
            document_class=_document_class_for(params.legal_order, params.document_id),
            path=params.path,
            language=params.language,
            as_of=params.as_of,
            force_refresh=params.force_refresh,
            agent_id=params.agent_id,
        )
        success = True
        if is_failure(result):
            return _with_order(result, params.legal_order)
        return dump_tool_output(GetArticleOutput, result)
    finally:
        _record_tool_metric("get_article", (time.perf_counter() - started) * 1000, success)


@mcp.tool()
def query_law(
    legal_order: str,
    document_id: str,
    query: str = "",
    tokens: int = 5000,
    language: str = "",
    as_of: str = "",
    force_refresh: bool = False,
    agent_id: str = "",
) -> dict[str, Any]:
    """Fetch a document, or the fragment of it that answers ``query`` (local search).

    ``query`` is matched locally against the fetched text; it is never sent to
    the source. For a citable unit prefer ``get_article``.
    """
    started = time.perf_counter()
    success = False
    try:
        params = parse_tool_input(
            QueryDocumentInput,
            legal_order=legal_order,
            document_id=document_id,
            query=query,
            tokens=tokens,
            language=language,
            as_of=as_of,
            force_refresh=force_refresh,
            agent_id=agent_id,
        )
        if isinstance(params, dict):
            success = True
            return params
        result = _read(
            "query_law",
            params.legal_order,
            params.document_id,
            operation=Operation.READ_DOCUMENT,
            document_class=_document_class_for(params.legal_order, params.document_id),
            language=params.language,
            as_of=params.as_of,
            force_refresh=params.force_refresh,
            agent_id=params.agent_id,
            options={"query": params.query, "tokens": params.tokens},
        )
        success = True
        if is_failure(result):
            return _with_order(result, params.legal_order)
        text = str(result.get("text") or "")
        result.setdefault("char_count", len(text))
        result.setdefault("full_text_chars", len(text))
        result.setdefault("articles_count", 0)
        return dump_tool_output(QueryLawOutput, result)
    finally:
        _record_tool_metric("query_law", (time.perf_counter() - started) * 1000, success)


#: Реквизиты, принадлежащие одному прочитанному фрагменту, а не документу.
#: Собираются в конверт ``get_multiple_articles`` вместе с остальным ответом по
#: первому локатору и снимаются оттуда: наверху они утверждали бы про весь
#: ответ то, что верно только про первую статью.
_FRAGMENT_IDENTITY_FIELDS: tuple[str, ...] = ("content_hash", "unit_id", "citation_format")


@mcp.tool()
def get_multiple_articles(
    legal_order: str,
    document_id: str,
    paths: list,
    language: str = "",
    as_of: str = "",
    force_refresh: bool = False,
    agent_id: str = "",
) -> dict[str, Any]:
    """Fetch several citable units of one document; each path gets its own result or refusal."""
    started = time.perf_counter()
    success = False
    try:
        params = parse_tool_input(
            GetFragmentsInput,
            legal_order=legal_order,
            document_id=document_id,
            paths=paths,
            language=language,
            as_of=as_of,
            force_refresh=force_refresh,
            agent_id=agent_id,
        )
        if isinstance(params, dict):
            success = True
            return params
        fragments: dict[str, dict[str, Any]] = {}
        not_found: list[str] = []
        first_success: dict[str, Any] | None = None
        first_failure: dict[str, Any] | None = None
        for requested in params.paths:
            result = _read(
                "get_multiple_articles",
                params.legal_order,
                params.document_id,
                operation=Operation.READ_FRAGMENT,
                document_class=_document_class_for(params.legal_order, params.document_id),
                path=requested,
                language=params.language,
                as_of=params.as_of,
                force_refresh=params.force_refresh,
                agent_id=params.agent_id,
            )
            if is_failure(result):
                not_found.append(requested)
                fragments[requested] = {
                    "code": result.get("code"),
                    "error": result.get("error"),
                    "details": result.get("details", {}),
                }
                first_failure = first_failure or result
                continue
            first_success = first_success or result
            fragments[requested] = {
                "path": result.get("locator") or requested,
                "text": result.get("text", ""),
                "evidence_id": result.get("evidence_id"),
                "confirmable": result.get("confirmable", False),
                "version_id": result.get("version_id"),
                "language": result.get("language"),
                "content_hash": result.get("content_hash"),
                # Посилання належить фрагменту: «п. 10» і «ст. 5» у межах одного
                # виклику різні, а з конверта воно зняте (тікет 21).
                "citation_format": result.get("citation_format"),
            }
        success = True
        if first_success is None:
            failure = dict(first_failure or {})
            failure["fragments"] = fragments
            failure["not_found"] = not_found
            return _with_order(failure, params.legal_order)
        # Конверт собирается из ответа по первому удавшемуся локатору, и это
        # его единственный источник реквизитов документа. Поэтому реквизиты
        # **фрагмента** отсюда снимаются поимённо: ответ, где на верхнем уровне
        # стоит `evidence_id: null` и `confirmable: false`, но рядом лежит
        # `content_hash` и `unit_id` первой статьи, читается как хеш всего
        # ответа — то есть как доказательство, которого здесь нет (D7, проба
        # 2026-09-09). Что осталось наверху — свойства документа (редакция,
        # язык, вид публикации), одинаковые для всех фрагментов.
        payload = {
            **first_success,
            "document_id": params.document_id,
            "fragments": fragments,
            "not_found": not_found,
            "evidence_id": None,
            "confirmable": False,
            "locator": "",
            "path": None,
            # Прямо названный источник конверта: не «поля неизвестно откуда», а
            # ответ по этому локатору. Реквизиты каждого фрагмента — в
            # `fragments`, и только там.
            "envelope_of": str(first_success.get("locator") or "") or None,
        }
        payload.pop("text", None)
        for fragment_field in _FRAGMENT_IDENTITY_FIELDS:
            payload.pop(fragment_field, None)
        return dump_tool_output(GetMultipleArticlesOutput, payload)
    finally:
        _record_tool_metric(
            "get_multiple_articles", (time.perf_counter() - started) * 1000, success
        )


@mcp.tool()
def get_law_metadata(
    legal_order: str, document_id: str, language: str = "", as_of: str = ""
) -> dict[str, Any]:
    """The document's card: dates, revisions, languages, cache status. A card never proves a quote."""
    started = time.perf_counter()
    success = False
    try:
        params = parse_tool_input(
            DocumentInput,
            legal_order=legal_order,
            document_id=document_id,
            language=language,
            as_of=as_of,
        )
        if isinstance(params, dict):
            success = True
            return params
        decision = route(
            params.legal_order,
            subject="get_law_metadata",
            operation=Operation.CARD,
            document_class=_document_class_for(params.legal_order, params.document_id),
        )
        if not decision.allowed or decision.source is None:
            success = True
            return _with_order(decision.as_output() or {}, params.legal_order)
        adapter = sources.get_adapter(decision.source.effective_adapter_id)
        if adapter is None:
            success = True
            return _with_order(
                NotCovered(
                    legal_order=params.legal_order,
                    manual_path=decision.source.manual_path or decision.source.source_url,
                    subject="get_law_metadata",
                    operation="card",
                    source_id=decision.source.id,
                ).as_output(),
                params.legal_order,
            )
        result = adapter.card(params.document_id, language=params.language, as_of=params.as_of)
        success = True
        if isinstance(result, TypedFailure):
            return _with_order(result.as_output(), params.legal_order)
        data = dict(result.data) if isinstance(result.data, dict) else {}
        payload = {
            "legal_order": params.legal_order,
            "document_id": params.document_id,
            "resolved_document_id": data.pop("resolved_document_id", params.document_id),
            "title": data.pop("title", params.document_id),
            "url": data.pop("url", result.provenance.source_url),
            "confirmable": False,
            "content_kind": ContentKind.METADATA.value,
            "publication_kind": PublicationKind.CARD.value,
            "retrieved_at": dt.datetime.now(timezone.utc).isoformat(),
            "source_id": decision.source.id,
            **data,
            **result.provenance.as_envelope(),
        }
        payload["confirmable"] = False
        payload["content_kind"] = ContentKind.METADATA.value
        return dump_tool_output(LawMetadataOutput, payload)
    finally:
        _record_tool_metric("get_law_metadata", (time.perf_counter() - started) * 1000, success)


# ---------------------------------------------------------------------------
# Поиск и разрешение (UA: локальный индекс/каталог; живой свободный поиск — только в engineering)
# ---------------------------------------------------------------------------


@mcp.tool()
def list_laws(category_filter: str = "", legal_order: str = "") -> dict[str, Any]:
    """List the curated working set of Ukrainian acts (not a whitelist of what is readable)."""
    started = time.perf_counter()
    success = False
    try:
        params = parse_tool_input(
            ListLawsInput, legal_order=legal_order, category_filter=category_filter
        )
        if isinstance(params, dict):
            success = True
            return params
        if params.legal_order and params.legal_order != "UA":
            success = True
            return _with_order(
                NotCovered(
                    legal_order=params.legal_order,
                    manual_path="реєстр робочого набору існує лише для UA; для інших правопорядків читайте документ за ідентифікатором",
                    subject="list_laws",
                    operation="list_laws",
                ).as_output(),
                params.legal_order,
            )
        laws = []
        for law in registry.list_laws():
            if params.category_filter and law.get("category") != params.category_filter:
                continue
            status = cache.get_status(law["id"])
            laws.append({**law, **status})
        success = True
        return dump_tool_output(
            ListLawsOutput,
            {
                "legal_order": "UA",
                "laws": laws,
                "total": len(laws),
                "version": __version__,
                "server_version": __version__,
            },
        )
    finally:
        _record_tool_metric("list_laws", (time.perf_counter() - started) * 1000, success)


@mcp.tool()
def resolve_law_id(legal_order: str, query: str) -> dict[str, Any]:
    """Resolve a law's name or number to the source's identifier (local index and catalogue)."""
    started = time.perf_counter()
    success = False
    try:
        params = parse_tool_input(ResolveLawInput, legal_order=legal_order, query=query)
        if isinstance(params, dict):
            success = True
            return params
        if params.legal_order != "UA":
            success = True
            decision = route(
                params.legal_order,
                subject="resolve_law_id",
                operation=Operation.SEARCH_BY_IDENTIFIER,
                # Класс документа обязателен: без него маршрут для LV выбирал
                # набор судебной практики, у которого та же операция объявлена
                # проверенной, и запрос названия закона уходил не тому
                # источнику. Разрешает название акта только законодательство.
                document_class=DocumentClass.ACT,
            )
            refusal = decision.as_output()
            if refusal is not None or decision.source is None:
                return _with_order(
                    refusal
                    or NotCovered(
                        legal_order=params.legal_order,
                        manual_path=(
                            "назва → ідентифікатор для цього правопорядку не "
                            "розв'язується автоматично; використовуйте офіційний ідентифікатор"
                        ),
                        subject="resolve_law_id",
                        operation=Operation.SEARCH_BY_IDENTIFIER.value,
                    ).as_output(),
                    params.legal_order,
                )
            source = decision.source
            # T230. До этого ветка обрывалась здесь: маршрут мог быть открыт, а
            # инструмент всё равно отвечал отказом, потому что за отказом не
            # стояло попытки. Резолв идёт **локальным** индексом источника,
            # поэтому формулировка юриста наружу не уходит и FR-306 не
            # ослабляется.
            adapter = sources.get_adapter(source.effective_adapter_id)
            resolver = getattr(adapter, "resolve", None) if adapter is not None else None
            if resolver is None:
                return _with_order(
                    NotCovered(
                        legal_order=params.legal_order,
                        manual_path=(
                            source.manual_path
                            or "назва → ідентифікатор для цього правопорядку не "
                            "розв'язується автоматично; використовуйте офіційний ідентифікатор"
                        ),
                        subject="resolve_law_id",
                        operation=Operation.SEARCH_BY_IDENTIFIER.value,
                        source_id=source.id,
                    ).as_output(),
                    params.legal_order,
                )
            outcome = resolver(params.query)
            if isinstance(outcome, TypedFailure):
                return _with_order(outcome.as_output(), params.legal_order)
            data = dict(outcome.data) if isinstance(outcome.data, dict) else {}
            return _with_order(
                dump_tool_output(
                    ResolveLawOutput,
                    {
                        "legal_order": params.legal_order,
                        "id": str(data.get("resolved_document_id") or "") or None,
                        "title": data.get("title") or None,
                        "url": data.get("url") or None,
                        "source": source.id,
                        "candidates": data.get("candidates") or [],
                        "notice": data.get("notice")
                        or (
                            "Ідентифікатор здобуто локальним індексом карти сайту джерела: "
                            "формулювання запиту назовні не надсилалось."
                        ),
                    },
                ),
                params.legal_order,
            )
        if not params.query:
            success = True
            return dump_tool_output(
                ResolveLawOutput,
                {
                    "legal_order": params.legal_order,
                    "id": None,
                    **friendly_error("Query is empty.", ErrorCode.INVALID_INPUT),
                },
            )

        resolved = source_adapter.resolve(params.query)
        if resolved is None:
            resolved = _resolve_from_live_search(params.query)
        if resolved is None:
            try:
                resolved = _resolve_from_open_data(params.query)
            except Exception:
                resolved = None
        if resolved is None:
            success = True
            payload = {
                "legal_order": params.legal_order,
                "id": None,
                **friendly_error(
                    (
                        f"Law not found: '{params.query}'. "
                        "Try list_laws() or known aliases (e.g. 'КМУ 1178')."
                        + (
                            " Живий пошук Ради у профілі legal вимкнено."
                            if not _live_channel_enabled()
                            else ""
                        )
                    ),
                    ErrorCode.NOT_FOUND,
                    {"query_length": len(params.query)},
                ),
            }
            return dump_tool_output(ResolveLawOutput, payload)

        status = cache.get_status(resolved["id"])
        success = True
        return dump_tool_output(
            ResolveLawOutput,
            {
                "legal_order": params.legal_order,
                "id": resolved["id"],
                "title": resolved.get("title", resolved["id"]),
                "url": resolved.get("url", ""),
                "ttl_days": resolved.get("cache_ttl_days", 30),
                "cache_ttl_days": resolved.get("cache_ttl_days", 30),
                "cached": status.get("cached", False),
                "fresh": status.get("fresh", False),
                "discovered": resolved.get("discovered", False),
                "source": resolved.get("source"),
                "source_channel": resolved.get("source_channel", "index"),
                "accepted_at": resolved.get("accepted_at"),
                "updated_at": resolved.get("updated_at"),
            },
        )
    finally:
        _record_tool_metric("resolve_law_id", (time.perf_counter() - started) * 1000, success)


#: Горячий кеш, над которым строится локальный индекс. Тот же каталог, что
#: наполняет чтение: индексируется прочитанное, а не свод (принцип I).
LOCAL_TEXTS_DIR = source_cache.registry_cache_dir(source_cache.LAWS_CACHE_SUBDIR)


def _local_index_search(query: str, limit: int) -> Any:
    """Поиск по локальному индексу; одна попытка постройки, если его нет.

    Постройка здесь, а не при старте: индекс нужен не каждому сеансу, а
    измеренная сборка на текущем кеше занимает 1–2 секунды. Ошибка постройки
    не глотается в пустую выдачу — вызывающий получает типизированный отказ и
    ручной путь, потому что «ничего не нашлось» и «поиск не выполнялся» для
    юриста означают разное (принцип III).
    """
    outcome = local_index.search_local(query, limit=limit)
    if isinstance(outcome, local_index.IndexUnavailable) and outcome.reason in (
        "not_built",
        "schema_mismatch",
    ):
        try:
            local_index.build_index(LOCAL_TEXTS_DIR)
        except Exception as error:  # noqa: BLE001 — отказ постройки остаётся отказом
            logger.warning("local index build failed: %s", error)
            return outcome
        outcome = local_index.search_local(query, limit=limit)
    return outcome


def _hit_as_result(hit: Any, law: dict[str, Any] | None) -> dict[str, Any]:
    """Запись выдачи из попадания индекса. Номер статьи не выдумывается."""
    article = str(hit.article_num or "")
    return {
        "law_id": hit.law_id,
        "document_id": hit.law_id,
        "title": (law or {}).get("title") or hit.law_title or hit.law_id,
        "snippet": hit.snippet[:700],
        "url": (law or {}).get("url", ""),
        # Ключ фрагмента (``frag-0001``) номером статьи не является и печатать
        # его как номер нельзя: локатор, которого нет у источника, — это
        # выдуманная ссылка.
        "article": article if not article.startswith("frag-") else None,
        "heading": hit.heading,
        "rank": hit.bm25,
    }


@mcp.tool()
def search_across_laws(legal_order: str, query: str, max_results: int = 5) -> dict[str, Any]:
    """Search locally cached documents of one legal order (UA working set and catalogue)."""
    started = time.perf_counter()
    success = False
    try:
        params = parse_tool_input(
            SearchAcrossLawsInput, legal_order=legal_order, query=query, max_results=max_results
        )
        if isinstance(params, dict):
            success = True
            return params
        if params.legal_order != "UA":
            success = True
            refusal = _free_text_refusal(
                params.legal_order, _primary_source_id(params.legal_order), "search_across_laws"
            )
            if refusal is not None:
                return refusal
            return _with_order(
                NotCovered(
                    legal_order=params.legal_order,
                    manual_path="пошук по кількох документах для цього правопорядку не реалізовано; читайте документ за ідентифікатором",
                    subject="search_across_laws",
                    operation=Operation.SEARCH_FREE_TEXT.value,
                ).as_output(),
                params.legal_order,
            )
        if not params.query:
            success = True
            return dump_tool_output(
                SearchAcrossLawsOutput,
                {
                    "legal_order": params.legal_order,
                    "query": params.query,
                    "results": [],
                    "searched_laws": 0,
                    "found_in": 0,
                },
            )

        limit = params.max_results

        # T243/FR-422. До этого ранжирование считалось плотностью подстроки на
        # слово (``search_engine.py:199``), и на запрос из двух слов первым
        # выходил ЦК, «Стаття 179 - 1. Поняття цифрової речі»: первое слово
        # встречалось ноль раз, второе — один, и короткая статья
        # на 88 слов выигрывала у длинной по делу. Уверенный ответ не по делу
        # хуже отказа (принцип III), поэтому ранжирует bm25 по индексу FTS5, а
        # не арифметика вхождений подстроки. Выбор бэкенда и цена альтернатив —
        # ``docs/adr/0014-search-backend.md``.
        warmed: list[str] = []

        def _warm_cache_from_catalogue() -> None:
            """Догрузить кандидатов в горячий кеш и перестроить индекс.

            Живой канал первичен (принцип I): индекс покрывает прочитанное, и
            расширяется он чтением, а не обещанием. Догрузка идёт только тогда,
            когда индекс ничего не дал, — иначе каждый запрос платил бы за
            обход каталога.
            """
            candidates: list[dict[str, Any]] = list(registry.find_by_alias(params.query))
            live = _live_search(params.query, min(limit, 3)) or {}
            for item in live.get("results", []):
                law_id = str(item.get("law_id") or "").strip()
                if law_id:
                    candidates.append(
                        {
                            "id": law_id,
                            "title": item.get("title") or law_id,
                            "url": item.get("url", ""),
                            "cache_ttl_days": 7,
                            "discovered": True,
                        }
                    )
            try:
                discovered = open_data_discovery.search(params.query, max_results=min(limit, 3))
            except Exception:  # noqa: BLE001 — недоступность каталога не рушит поиск
                discovered = {"results": []}
            for item in discovered.get("results", []):
                law_id = str(item.get("law_id") or "").strip()
                if law_id:
                    candidates.append(
                        {
                            "id": law_id,
                            "title": item.get("title", law_id),
                            "url": item.get("url", ""),
                            "cache_ttl_days": 7,
                            "discovered": True,
                        }
                    )

            seen: set[str] = set()
            for law in candidates:
                law_id = str(law.get("id") or "")
                if not law_id or law_id in seen:
                    continue
                seen.add(law_id)
                if cache.get_cached_entry(law_id, allow_stale=True) is not None:
                    continue
                try:
                    if cache.get_or_fetch(law_id, live_loader, search_engine, force_refresh=False):
                        warmed.append(law_id)
                except Exception:  # noqa: BLE001 — один недоступный закон не рушит поиск
                    continue
            if warmed:
                try:
                    local_index.build_index(LOCAL_TEXTS_DIR)
                except Exception as error:  # noqa: BLE001
                    logger.warning("local index rebuild after warm-up failed: %s", error)

        outcome = _local_index_search(params.query, limit)
        if isinstance(outcome, local_index.IndexUnavailable) or not outcome.hits:
            _warm_cache_from_catalogue()
            if warmed:
                outcome = _local_index_search(params.query, limit)

        if isinstance(outcome, local_index.IndexUnavailable):
            # Поиск не выполнялся. Пустая выдача сказала бы «не нашлось», то есть
            # неправду о непокрытом (принцип III).
            success = True
            return _with_order(
                NotCovered(
                    legal_order=params.legal_order,
                    manual_path=outcome.manual_path,
                    subject=params.query,
                    operation=Operation.SEARCH_FREE_TEXT.value,
                    message=(f"Локальний пошук не виконано ({outcome.reason}): {outcome.detail}"),
                ).as_output(),
                params.legal_order,
            )

        results = _as_navigation(
            [_hit_as_result(hit, _resolve_law(hit.law_id)) for hit in outcome.hits]
        )
        # Поиск состоялся и не нашёл ничего — это законный ответ, но он обязан
        # отличаться от «поиска не было» словами, а не только кодом, и обязан
        # называть ручной путь: иначе пустой список читается как «нормы нет».
        empty_note = (
            ""
            if results
            else (
                "; у гарячому кеші нічого не знайдено — це не означає, що норми "
                f"немає: поза кешем пошук не йшов. Ручний шлях: {local_index.MANUAL_PATH}"
            )
        )
        success = True
        return dump_tool_output(
            SearchAcrossLawsOutput,
            {
                "legal_order": params.legal_order,
                "query": params.query,
                "results": results,
                # Документы, а не фрагменты: `indexed_documents` считает строки
                # таблицы `docs`, то есть статьи и куски. На горячем кеше это
                # 3073 против 22 — число, которое агент прочитал бы как «искали
                # по трём тысячам законов».
                "searched_laws": len(outcome.covered_law_ids),
                "found_in": len(results),
                "source_channel": "live+index" if warmed else "index",
                "search_scope": outcome.scope_note
                + (f"; догружено до індексу документів: {len(warmed)}" if warmed else "")
                + "; живий повнотекстовий пошук Ради "
                + ("увімкнено" if _live_channel_enabled() else "вимкнено профілем legal")
                + empty_note,
            },
        )
    finally:
        _record_tool_metric("search_across_laws", (time.perf_counter() - started) * 1000, success)


def _primary_source_id(legal_order: str) -> str:
    candidates = legal_orders.sources_for(legal_order)
    return candidates[0].id if candidates else ""


@mcp.tool()
def search_articles(
    legal_order: str, query: str, document_id: str = "", max_results: int = 5
) -> dict[str, Any]:
    """Hybrid search over locally indexed UA articles (own Postgres backend only)."""
    started = time.perf_counter()
    success = False
    try:
        params = parse_tool_input(
            SearchArticlesInput,
            legal_order=legal_order,
            query=query,
            document_id=document_id,
            max_results=max_results,
        )
        if isinstance(params, dict):
            success = True
            return params
        if params.legal_order != "UA":
            success = True
            return _with_order(
                NotCovered(
                    legal_order=params.legal_order,
                    manual_path="індекс статей існує лише для UA; читайте документ за ідентифікатором",
                    subject="search_articles",
                    operation=Operation.SEARCH_FREE_TEXT.value,
                ).as_output(),
                params.legal_order,
            )
        if hybrid is None:
            success = True
            return dump_tool_output(
                ToolErrorOutput,
                friendly_error(
                    # FR-422: отказ обязан называть путь, а не только запрет.
                    # `search_across_laws` перестал быть отсылкой в никуда: он
                    # ищет по индексу FTS5 над горячим кешем и говорит, что
                    # именно покрыл (ADR 0014).
                    "Hybrid article search requires the own Postgres backend "
                    "(USE_PG_BACKEND=1, local or YURKO_OWN_DATABASE=1). The delivery ships "
                    "without it. Working route: search_across_laws — a local FTS5 index over "
                    "the hot cache, which states in its answer how much it covered; then "
                    "get_article by the identifier it returns. Manual route: "
                    "https://zakon.rada.gov.ua",
                    ErrorCode.SOURCE_UNAVAILABLE,
                    {
                        "backend": "file",
                        "pg_backend": _pg_status,
                        "working_alternative": "search_across_laws",
                    },
                ),
            )

        if not params.query:
            success = True
            return dump_tool_output(
                SearchArticlesOutput,
                {
                    "legal_order": params.legal_order,
                    "query": params.query,
                    "document_id": params.document_id or None,
                    "results": [],
                    "found": 0,
                    "backend": "postgres",
                },
            )

        try:
            results = hybrid.search_articles(
                params.query, params.document_id or None, params.max_results
            )
        except Exception as error:
            success = True
            return dump_tool_output(
                ToolErrorOutput,
                friendly_error(
                    "Hybrid search backend is temporarily unavailable.",
                    ErrorCode.SOURCE_UNAVAILABLE,
                    {"backend": "postgres", "error": type(error).__name__},
                ),
            )

        success = True
        navigation = _as_navigation(list(results))
        return dump_tool_output(
            SearchArticlesOutput,
            {
                "legal_order": params.legal_order,
                "query": params.query,
                "document_id": params.document_id or None,
                "results": navigation,
                "found": len(navigation),
                "backend": "postgres",
            },
        )
    finally:
        _record_tool_metric("search_articles", (time.perf_counter() - started) * 1000, success)


@mcp.tool()
def discover_laws(query: str = "", max_results: int = 10, legal_order: str = "") -> dict[str, Any]:
    """Find Ukrainian acts by title words in the local catalogue of document cards."""
    started = time.perf_counter()
    success = False
    try:
        params = parse_tool_input(
            DiscoverLawsInput, legal_order=legal_order, query=query, max_results=max_results
        )
        if isinstance(params, dict):
            success = True
            return params
        if params.legal_order and params.legal_order != "UA":
            success = True
            return _with_order(
                NotCovered(
                    legal_order=params.legal_order,
                    manual_path="каталог карток існує лише для UA",
                    subject="discover_laws",
                    operation="discover",
                ).as_output(),
                params.legal_order,
            )
        live = _live_search(params.query, params.max_results)
        if live and live.get("results"):
            success = True
            return dump_tool_output(DiscoverLawsOutput, live)

        result = open_data_discovery.search(params.query, params.max_results)
        result["source_channel"] = "index"
        channel_notice = (
            "Живий пошук Ради вимкнено профілем legal (рядок запиту не надсилається) — "
            "відповідь із локального каталогу карток."
            if not _live_channel_enabled()
            else (
                "Живий пошук Ради недоступний — відповідь із локального каталогу карток."
                if live is None
                else "Живий пошук Ради не знайшов збігів — відповідь із локального каталогу карток."
            )
        )
        # Вік каталогу дописується, а не затирається: канал і свіжість — різні
        # факти, і мовчання про застарілу копію вже коштувало б юристові актів,
        # ухвалених після дати цієї копії (рев'ю тікета 15).
        catalogue_notice = str(result.get("freshness_notice") or "")
        result["freshness_notice"] = " ".join(
            part for part in (channel_notice, catalogue_notice) if part
        )
        success = True
        return dump_tool_output(DiscoverLawsOutput, result)
    except Exception as error:
        success = True
        return dump_tool_output(
            ToolErrorOutput,
            friendly_error(
                "Official open data discovery is temporarily unavailable.",
                ErrorCode.SOURCE_UNAVAILABLE,
                {"source": "rada_open_data_doc_cards", "error": type(error).__name__},
            ),
        )
    finally:
        _record_tool_metric("discover_laws", (time.perf_counter() - started) * 1000, success)


# ---------------------------------------------------------------------------
# Судебная практика
# ---------------------------------------------------------------------------


def _applied_filters(data: dict[str, Any], params: Any) -> dict[str, Any]:
    """Что из фильтров действительно дошло до источника — со слов адаптера (T232).

    До этого поле было эхом входа: инструмент утверждал применение фильтра
    независимо от того, отправил ли его адаптер в сеть. Утверждение о фильтре,
    которого не было, — то же самое, что усечённая выдача, поданная как полная
    (принцип III), только незаметнее: юрист видит «date_from применён» и считает
    выдачу отобранной по дате.

    Адаптер объявляет применённое ключом ``applied_filters`` в ``data``. Не
    объявил — поле не выдумывает за него: оно называет запрошенное запрошенным
    и говорит, что применение не подтверждено.
    """
    declared = data.get("applied_filters")
    if isinstance(declared, dict):
        return {"declared_by_adapter": True, **declared}
    return {
        "declared_by_adapter": False,
        "requested": {
            "case_number": params.case_number or None,
            "cites": params.cites or None,
            "date_from": params.date_from or None,
            "date_to": params.date_to or None,
            "free_text": bool(params.query),
        },
        "notice": (
            "Джерело не повідомило, які фільтри застосовано; наведено запит, "
            "а не застосоване. Вважати видачу відібраною за цими полями не можна."
        ),
    }


@mcp.tool()
def search_decisions(
    legal_order: str,
    query: str = "",
    case_number: str = "",
    cites: str = "",
    date_from: str = "",
    date_to: str = "",
    max_results: int = 10,
) -> dict[str, Any]:
    """Search case law of one forum by case number, by cited act (structural), or free text (engineering only).

    ``cites`` is a CELEX act identifier: documents that cite it (T-cites) —
    structural, like ``case_number``, not free text. ``date_from``/``date_to``
    are inclusive decision dates; a source that cannot apply them answers
    ``unsupported_filter`` instead of ignoring them.
    """
    started = time.perf_counter()
    success = False
    try:
        params = parse_tool_input(
            SearchDecisionsInput,
            legal_order=legal_order,
            query=query,
            case_number=case_number,
            cites=cites,
            date_from=date_from,
            date_to=date_to,
            max_results=max_results,
        )
        if isinstance(params, dict):
            success = True
            return params
        if not params.query and not params.case_number and not params.cites:
            success = True
            return dump_tool_output(
                ToolErrorOutput,
                friendly_error(
                    "Provide a case_number, cites (CELEX of a cited act), or a query "
                    "in the engineering profile.",
                    ErrorCode.INVALID_INPUT,
                ),
            )
        operation = (
            Operation.SEARCH_BY_CITATION
            if params.cites
            else (
                Operation.SEARCH_BY_IDENTIFIER if params.case_number else Operation.SEARCH_FREE_TEXT
            )
        )
        decision = route(
            params.legal_order,
            subject="search_decisions",
            operation=operation,
            document_class=DocumentClass.DECISION,
        )
        if not decision.allowed or decision.source is None:
            success = True
            return _with_order(decision.as_output() or {}, params.legal_order)
        source = decision.source
        if params.query:
            refusal = _free_text_refusal(params.legal_order, source.id, "search_decisions")
            if refusal is not None:
                success = True
                return refusal
        adapter = sources.get_adapter(source.effective_adapter_id)
        if adapter is None:
            success = True
            return _with_order(
                NotCovered(
                    legal_order=params.legal_order,
                    manual_path=source.manual_path or source.source_url,
                    subject="search_decisions",
                    operation=operation.value,
                    source_id=source.id,
                ).as_output(),
                params.legal_order,
            )
        if params.case_number:
            # Номер дела — единственное поле поиска практики, которое уходит
            # источнику (ADR 0009, H2). До этой проверки он уходил после одного
            # `strip`, и HUDOC превращал произвольную строку в поиск подстроки
            # по названию дела: свободный текст покидал процесс через поле,
            # названное «номером». Форма номера объявляется форумом; форум,
            # который её не объявил, проверяется общим шаблоном.
            try:
                egress.assert_identifier(
                    params.case_number,
                    pattern=getattr(adapter, "case_number_pattern", None),
                )
            except egress.EgressDenied:
                success = True
                return _with_order(
                    NotCovered(
                        legal_order=params.legal_order,
                        manual_path=(
                            "Значення поля case_number не є номером справи цього форуму, "
                            "тому джерелу воно не надсилається: пошук за вільним текстом "
                            f"у профілі legal не виконується. Ручний шлях: "
                            f"{source.manual_path or source.source_url}"
                        ),
                        subject="search_decisions",
                        operation=Operation.SEARCH_FREE_TEXT.value,
                        source_id=source.id,
                    ).as_output(),
                    params.legal_order,
                )
        if params.wants_date_filter and not getattr(adapter, "supports_date_filter", False):
            success = True
            return _with_order(
                UnsupportedFilter(
                    source_id=source.id,
                    filter="date_from/date_to",
                    manual_path=source.manual_path or source.source_url,
                ).as_output(),
                params.legal_order,
            )
        result = adapter.search(
            params.query,
            case_number=params.case_number,
            cites=params.cites,
            date_from=params.date_from,
            date_to=params.date_to,
            max_results=params.max_results,
        )
        success = True
        if isinstance(result, TypedFailure):
            return _with_order(result.as_output(), params.legal_order)
        data = dict(result.data) if isinstance(result.data, dict) else {}
        payload = {
            "legal_order": params.legal_order,
            "query": params.query,
            "case_number": params.case_number or None,
            "cites": params.cites or None,
            "results": _as_navigation(list(data.get("results") or [])),
            "found": int(data.get("found") or 0),
            "source": str(data.get("source") or source.id),
            "retrieved_at": str(
                data.get("retrieved_at") or dt.datetime.now(timezone.utc).isoformat()
            ),
            "complete": bool(data.get("complete", False)),
            "applied_filters": _applied_filters(data, params),
            "failures": list(data.get("failures") or []),
            "source_id": source.id,
            "notice": " ".join(
                part
                for part in (
                    "Відсутність результату не доводить відсутності практики; "
                    "неповна видача не обіцяє повноти.",
                    str(data.get("notice") or ""),
                )
                if part
            ),
        }
        return dump_tool_output(SearchDecisionsOutput, payload)
    finally:
        _record_tool_metric("search_decisions", (time.perf_counter() - started) * 1000, success)


@mcp.tool()
def get_decision(
    legal_order: str,
    document_id: str,
    language: str = "",
    path: str = "",
    agent_id: str = "",
) -> dict[str, Any]:
    """Fetch the official text of one judicial document (or one paragraph) with its provenance.

    ``document_id`` is the forum's identifier: ЄДРСР numeric id, ECLI, HUDOC
    itemid, ICJ document id. An international forum's answer
    carries ``binding_scope`` (principle VI).
    """
    started = time.perf_counter()
    success = False
    try:
        params = parse_tool_input(
            GetDecisionInput,
            legal_order=legal_order,
            document_id=document_id,
            language=language,
            path=path,
            agent_id=agent_id,
        )
        if isinstance(params, dict):
            success = True
            return params
        result = _read(
            "get_decision",
            params.legal_order,
            params.document_id,
            operation=Operation.READ_FRAGMENT if params.path else Operation.READ_DOCUMENT,
            document_class=DocumentClass.DECISION,
            path=params.path,
            language=params.language,
            agent_id=params.agent_id,
        )
        success = True
        if is_failure(result):
            return _with_order(result, params.legal_order)
        text = str(result.get("text") or "")
        result.setdefault("char_count", len(text))
        result.setdefault("material_kind", "unknown")
        return dump_tool_output(DecisionOutput, result)
    finally:
        _record_tool_metric("get_decision", (time.perf_counter() - started) * 1000, success)


@mcp.tool()
def verify_quote(
    legal_order: str,
    document_id: str,
    path: str,
    quote: str,
    language: str = "",
    as_of: str = "",
) -> dict[str, Any]:
    """Check a quote word for word against the official text at that address.

    Read-only and stateless: the unit is fetched through the same readers as
    ``get_article``/``get_decision``, nothing is registered as evidence.
    ``path`` is the unit's address (2(1), 5a, paragraph 45); empty searches the
    whole document, which is the usual case for a judgment. The quote must be
    at least 15 characters.

    Statuses: ``exact`` — the string stands in the source verbatim;
    ``normalized`` — it matched after the listed typographic rules (whitespace,
    typographic quotes and apostrophes, dashes, soft hyphens, zero-width
    characters, non-breaking spaces) and only the rules that actually mattered
    are named; ``mismatch`` — it did not, and the nearest fragment of the same
    length comes back with the differences marked, or ``reason`` says why no
    fragment was computed; ``not_found`` (no such document) and
    ``path_not_found`` (no such unit in it) are different answers; typed
    ``source_unavailable`` / ``not_covered``. ``document_class_guessed`` says
    whether the reader was chosen from the identifier's shape.

    A textual match proves only that the string is in the source at that
    address. It does **not** prove that the quote supports the proposition it
    is cited for.
    """
    started = time.perf_counter()
    success = False
    try:
        params = parse_tool_input(
            VerifyQuoteInput,
            legal_order=legal_order,
            document_id=document_id,
            path=path,
            quote=quote,
            language=language,
            as_of=as_of,
        )
        if isinstance(params, dict):
            success = True
            return params
        result = quote_check.verify(
            legal_order=params.legal_order,
            document_id=params.document_id,
            path=params.path,
            quote=params.quote,
            language=params.language,
            as_of=params.as_of,
            session_id=_session(),
            document_class=_document_class_for(params.legal_order, params.document_id),
        )
        success = True
        if is_failure(result):
            return _with_order(result, params.legal_order)
        return dump_tool_output(VerifyQuoteOutput, result)
    finally:
        _record_tool_metric("verify_quote", (time.perf_counter() - started) * 1000, success)


# ---------------------------------------------------------------------------
# Украинские реестры (структурные ключи; свободный текст — только в engineering)
# ---------------------------------------------------------------------------


@mcp.tool()
def search_debtors(
    name: str = "", code: str = "", max_results: int = 10, legal_order: str = ""
) -> dict[str, Any]:
    """Search the enforcement-debtors register by company code or РНОКПП ``code`` (name only in engineering profile)."""
    started = time.perf_counter()
    success = False
    try:
        params = parse_tool_input(
            SearchDebtorsInput,
            legal_order=legal_order,
            name=name,
            code=code,
            max_results=max_results,
        )
        if isinstance(params, dict):
            success = True
            return params
        if params.legal_order and params.legal_order != "UA":
            success = True
            return _with_order(
                route(params.legal_order, subject="search_debtors").as_output() or {},
                params.legal_order,
            )
        if not params.name and not params.code:
            success = True
            return dump_tool_output(
                ToolErrorOutput,
                friendly_error(
                    "Provide a company code or РНОКПП (or a name in the engineering profile).",
                    ErrorCode.INVALID_INPUT,
                ),
            )
        if params.name:
            refusal = _free_text_refusal("UA", "ua_debtors_register", "search_debtors")
            if refusal is not None:
                success = True
                return refusal
        if params.code and not re.match(r"^\d{8,10}$", params.code):
            success = True
            return dump_tool_output(
                ToolErrorOutput,
                friendly_error(
                    "code must be 8 (company code) or 10 (РНОКПП) digits.", ErrorCode.INVALID_INPUT
                ),
            )
        try:
            result = debtors_registry.search(params.name, params.code, params.max_results)
        except SourceRequiresHuman as boundary:
            # Реєстр працює, але відповідає лише людині за браузером. Це межа
            # покриття з ручним шляхом, а не «спробуйте пізніше».
            success = True
            return _with_order(
                NotCovered(
                    legal_order="UA",
                    manual_path=str(boundary),
                    subject="search_debtors",
                    operation=Operation.SEARCH_BY_IDENTIFIER.value,
                    source_id="ua_debtors_register",
                ).as_output()
                or {},
                "UA",
            )
        except SourceAdapterError as error:
            success = True
            return dump_tool_output(
                ToolErrorOutput,
                friendly_error(
                    "Debtors register is temporarily unavailable.",
                    ErrorCode.SOURCE_UNAVAILABLE,
                    {"source": debtors_registry.name, "error": type(error).__name__},
                ),
            )
        success = True
        return dump_tool_output(SearchDebtorsOutput, {"legal_order": "UA", **result})
    finally:
        _record_tool_metric("search_debtors", (time.perf_counter() - started) * 1000, success)


@mcp.tool()
def discover_registries(
    query: str = "", max_results: int = 10, legal_order: str = ""
) -> dict[str, Any]:
    """Discover data.gov.ua datasets by keyword (engineering profile only: the keyword leaves the machine)."""
    started = time.perf_counter()
    success = False
    try:
        params = parse_tool_input(
            DiscoverRegistriesInput, legal_order=legal_order, query=query, max_results=max_results
        )
        if isinstance(params, dict):
            success = True
            return params
        if params.legal_order and params.legal_order != "UA":
            success = True
            return _with_order(
                route(params.legal_order, subject="discover_registries").as_output() or {},
                params.legal_order,
            )
        if not params.query:
            success = True
            return dump_tool_output(
                ToolErrorOutput, friendly_error("Query is empty.", ErrorCode.INVALID_INPUT)
            )
        refusal = _free_text_refusal("UA", "ua_open_data_catalog", "discover_registries")
        if refusal is not None:
            success = True
            return refusal
        try:
            result = open_data_catalog.search(params.query, params.max_results)
        except SourceAdapterError as error:
            success = True
            return dump_tool_output(
                ToolErrorOutput,
                friendly_error(
                    "data.gov.ua catalog is temporarily unavailable.",
                    ErrorCode.SOURCE_UNAVAILABLE,
                    {"source": open_data_catalog.name, "error": type(error).__name__},
                ),
            )
        success = True
        return dump_tool_output(DiscoverRegistriesOutput, {"legal_order": "UA", **result})
    finally:
        _record_tool_metric("discover_registries", (time.perf_counter() - started) * 1000, success)


_TENDER_NUMBER = re.compile(r"^UA-\d{4}-\d{2}-\d{2}-\d{6}(?:-[a-z])?$", re.IGNORECASE)


@mcp.tool()
def search_tenders(query: str = "", max_results: int = 10, legal_order: str = "") -> dict[str, Any]:
    """Search Prozorro by tender number UA-YYYY-MM-DD-NNNNNN (free text only in engineering profile)."""
    started = time.perf_counter()
    success = False
    try:
        params = parse_tool_input(
            SearchTendersInput, legal_order=legal_order, query=query, max_results=max_results
        )
        if isinstance(params, dict):
            success = True
            return params
        if params.legal_order and params.legal_order != "UA":
            success = True
            return _with_order(
                route(params.legal_order, subject="search_tenders").as_output() or {},
                params.legal_order,
            )
        if not params.query:
            success = True
            return dump_tool_output(
                ToolErrorOutput, friendly_error("Query is empty.", ErrorCode.INVALID_INPUT)
            )
        if not _TENDER_NUMBER.match(params.query):
            refusal = _free_text_refusal("UA", "ua_prozorro", "search_tenders")
            if refusal is not None:
                success = True
                return refusal
        try:
            result = prozorro_registry.search(params.query, params.max_results)
        except SourceAdapterError as error:
            success = True
            return dump_tool_output(
                ToolErrorOutput,
                friendly_error(
                    "Prozorro search is temporarily unavailable.",
                    ErrorCode.SOURCE_UNAVAILABLE,
                    {"source": prozorro_registry.name, "error": type(error).__name__},
                ),
            )
        success = True
        return dump_tool_output(SearchTendersOutput, {"legal_order": "UA", **result})
    finally:
        _record_tool_metric("search_tenders", (time.perf_counter() - started) * 1000, success)


@mcp.tool()
def get_tender(tender_id: str = "", legal_order: str = "") -> dict[str, Any]:
    """Fetch a tender summary from the official Prozorro central database API by internal id."""
    started = time.perf_counter()
    success = False
    try:
        params = parse_tool_input(GetTenderInput, legal_order=legal_order, tender_id=tender_id)
        if isinstance(params, dict):
            success = True
            return params
        if params.legal_order and params.legal_order != "UA":
            success = True
            return _with_order(
                route(params.legal_order, subject="get_tender").as_output() or {},
                params.legal_order,
            )
        if not params.tender_id:
            success = True
            return dump_tool_output(
                ToolErrorOutput, friendly_error("tender_id is empty.", ErrorCode.INVALID_INPUT)
            )
        if not re.match(
            r"^[0-9a-f]{32}$|^UA-\d{4}-\d{2}-\d{2}-\d{6}(?:-[a-z])?$",
            params.tender_id,
            re.IGNORECASE,
        ):
            success = True
            return dump_tool_output(
                ToolErrorOutput,
                friendly_error(
                    "tender_id must be a 32-hex internal id or a UA-… number.",
                    ErrorCode.INVALID_INPUT,
                ),
            )
        try:
            result = prozorro_registry.get_tender(params.tender_id)
        except SourceRecordNotFound:
            # Джерело відповіло «такого немає». Радити «спробуйте пізніше» тут
            # означало б послати юриста чекати на запис, якого не буде.
            success = True
            return dump_tool_output(
                ToolErrorOutput,
                friendly_error(
                    "Prozorro has no tender with this id.",
                    ErrorCode.NOT_FOUND,
                    {"tender_id": params.tender_id, "source": prozorro_registry.name},
                ),
            )
        except SourceAdapterError as error:
            success = True
            return dump_tool_output(
                ToolErrorOutput,
                friendly_error(
                    "Prozorro is temporarily unavailable.",
                    ErrorCode.SOURCE_UNAVAILABLE,
                    {"tender_id": params.tender_id, "error": type(error).__name__},
                ),
            )
        success = True
        return dump_tool_output(TenderOutput, {"legal_order": "UA", **result})
    finally:
        _record_tool_metric("get_tender", (time.perf_counter() - started) * 1000, success)


@mcp.tool()
def list_registries(legal_order: str = "") -> dict[str, Any]:
    """List the Ukrainian register adapters, the tools that query each, and their live health."""
    started = time.perf_counter()
    success = False
    try:
        filter_order = normalize_legal_order_code(legal_order)
        if filter_order and not is_known_legal_order(filter_order):
            success = True
            return unknown_legal_order(legal_order)
        if filter_order and filter_order != "UA":
            success = True
            return dump_tool_output(
                ListRegistriesOutput,
                {"legal_order": filter_order, "registries": [], "total": 0},
            )
        entries: list[tuple[Any, str, list[str]]] = [
            (
                source_adapter,
                "Legislation — zakon.rada.gov.ua",
                [
                    "resolve_law_id",
                    "query_law",
                    "get_article",
                    "get_multiple_articles",
                    "search_across_laws",
                    "search_articles",
                    "discover_laws",
                    "list_laws",
                    "get_law_metadata",
                ],
            ),
            (
                court_registry,
                "Court decisions — ЄДРСР (reyestr.court.gov.ua)",
                ["search_decisions", "get_decision"],
            ),
            (
                debtors_registry,
                "Enforcement debtors — ЄРБ (erb.minjust.gov.ua)",
                ["search_debtors"],
            ),
            (open_data_catalog, "Open-data catalog — data.gov.ua (CKAN)", ["discover_registries"]),
            (
                prozorro_registry,
                "Public procurement — Prozorro (prozorro.gov.ua)",
                ["search_tenders", "get_tender"],
            ),
        ]
        registries = []
        for adapter, description, tools in entries:
            health = adapter.health()
            registries.append(
                {
                    "adapter": adapter.name,
                    "description": description,
                    "source_policy": health.source_policy.value,
                    "ok": health.ok,
                    "tools": tools,
                    "details": health.details,
                }
            )
        success = True
        return dump_tool_output(
            ListRegistriesOutput,
            {"legal_order": "UA", "registries": registries, "total": len(registries)},
        )
    finally:
        _record_tool_metric("list_registries", (time.perf_counter() - started) * 1000, success)


# ---------------------------------------------------------------------------
# Договоры, публичная карточка, покрытие, методика, сверка
# ---------------------------------------------------------------------------


@mcp.tool()
def get_case(legal_order: str, case_number: str) -> dict[str, Any]:
    """Public card of a court proceeding by case number: published documents and links, dated.

    Only what the court publishes; no closed e-Curia data. Absence of a
    publication does not mean absence of a filing.
    """
    started = time.perf_counter()
    success = False
    try:
        params = parse_tool_input(GetCaseInput, legal_order=legal_order, case_number=case_number)
        if isinstance(params, dict):
            success = True
            return params
        decision = route(
            params.legal_order,
            subject="get_case",
            operation=Operation.PUBLIC_CASE_CARD,
            document_class=DocumentClass.DECISION,
        )
        if not decision.allowed or decision.source is None:
            success = True
            return _with_order(decision.as_output() or {}, params.legal_order)
        adapter = sources.get_adapter(decision.source.effective_adapter_id)
        public_card = getattr(adapter, "public_card", None)
        if adapter is None or public_card is None:
            success = True
            return _with_order(
                NotCovered(
                    legal_order=params.legal_order,
                    manual_path=decision.source.manual_path or decision.source.source_url,
                    subject="get_case",
                    operation=Operation.PUBLIC_CASE_CARD.value,
                    source_id=decision.source.id,
                ).as_output(),
                params.legal_order,
            )
        result = public_card(params.case_number)
        success = True
        if isinstance(result, TypedFailure):
            return _with_order(result.as_output(), params.legal_order)
        data = dict(result.data) if isinstance(result.data, dict) else {}
        envelope = result.provenance.as_envelope()
        payload = {
            "legal_order": params.legal_order,
            "case_number": params.case_number,
            "source_id": decision.source.id,
            **data,
            **envelope,
            # Дата получения считается последней и поверх конверта: пустое поле
            # конверта не должно затирать уже известное время обращения. Карточка
            # без даты получения бесполезна — «не опубликовано» без даты не
            # говорит, на какой момент это проверялось (ADR 0006).
            "source_url": str(
                data.get("source_url") or envelope.get("source_url") or decision.source.source_url
            ),
            "fetched_at": str(
                data.get("fetched_at")
                or envelope.get("fetched_at")
                or dt.datetime.now(timezone.utc).isoformat()
            ),
        }
        return dump_tool_output(CaseCardOutput, payload)
    finally:
        _record_tool_metric("get_case", (time.perf_counter() - started) * 1000, success)


@mcp.tool()
def list_coverage(legal_order: str = "*") -> dict[str, Any]:
    """Coverage map: which operations are verified for each source."""
    started = time.perf_counter()
    success = False
    try:
        code = normalize_legal_order_code(legal_order) or "*"
        if code != "*" and not is_known_legal_order(code):
            success = True
            return unknown_legal_order(legal_order)
        described = legal_orders.describe_coverage(None if code == "*" else code)
        payload: dict[str, Any] = {
            "legal_order": code,
            "schema_version": CONTRACT_VERSION,
            "profile": yurko_profile.profile_summary(),
            "legal_orders": list(LEGAL_ORDERS),
            "sources": list(described),
            "notice": (
                "layer — перевірений шар за операціями; declared_layer — задекларований. "
                "Карта не містить правового висновку."
            ),
        }
        success = True
        return payload
    finally:
        _record_tool_metric("list_coverage", (time.perf_counter() - started) * 1000, success)


# ---------------------------------------------------------------------------
# T021 — описания инструментов как недоверенный ввод (FR-028, принцип VIII)
# ---------------------------------------------------------------------------

_INSTRUCTION_MARKERS: tuple[str, ...] = (
    "ignore previous",
    "ignore all previous",
    "disregard the above",
    "disregard previous",
    "system:",
    "assistant:",
    "<system>",
    "you must always",
    "you should always",
    "always call",
    "never mention",
    "do not tell the user",
    "new instructions",
    "override the",
    "забудь попередні",
    "ігноруй попередні",
    "ти повинен завжди",
    "не кажи користувачу",
)

_REDACTED = "[опис вилучено: містив вказівку моделі, а не опис інструменту]"


def normalize_tool_description(description: str | None) -> str:
    """Привести описание инструмента к виду, безопасному для контекста модели.

    Строка, содержащая указание модели, вырезается целиком: обезвредить её
    частично нельзя, а оставить нельзя тем более.
    """
    text = str(description or "")
    if not text.strip():
        return ""

    kept: list[str] = []
    redacted = False
    for line in text.splitlines():
        lowered = line.lower()
        if any(marker in lowered for marker in _INSTRUCTION_MARKERS):
            redacted = True
            continue
        kept.append(line)

    if redacted:
        kept.append(_REDACTED)
    return "\n".join(kept).strip()


def _normalize_registered_descriptions() -> dict[str, str]:
    """Отпечаток «имя → хеш описания»; изменение состава обнаруживается по нему."""
    fingerprint: dict[str, str] = {}
    for name, tool in _list_registered_tools().items():
        cleaned = normalize_tool_description(getattr(tool, "description", ""))
        try:
            tool.description = cleaned
        except Exception:  # pragma: no cover — не все реализации допускают запись
            logger.warning("tool %s: description is read-only, left as registered", name)
        fingerprint[name] = content_hash(f"{name}\n{cleaned}")[:16]
    return fingerprint


def _list_registered_tools() -> dict[str, Any]:
    """Зарегистрированные инструменты синхронно, из любого контекста."""
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        tools = loop.run_until_complete(mcp.list_tools(run_middleware=False))
    finally:
        loop.close()
    return {tool.name: tool for tool in tools}


TOOL_FINGERPRINT: dict[str, str] = _normalize_registered_descriptions()


# ---------------------------------------------------------------------------
# T022 — журналирование сводится к агрегатам (FR-220)
# ---------------------------------------------------------------------------

_source_metrics_lock = threading.Lock()
_source_metrics: dict[str, dict[str, Any]] = {}


def _source_metrics_snapshot() -> dict[str, Any]:
    with _source_metrics_lock:
        raw = {name: dict(data) for name, data in _source_metrics.items()}
    snapshot: dict[str, Any] = {}
    for name, data in raw.items():
        calls = int(data["calls"])
        snapshot[name] = {
            "calls": calls,
            "errors": int(data["errors"]),
            "avg_latency_ms": round(data["total_latency_ms"] / calls, 2) if calls else 0.0,
        }
    return snapshot


class _NoQueryTextFilter(logging.Filter):
    """Не пропускать в журнал записи, несущие текст запроса пользователя."""

    def filter(self, record: logging.LogRecord) -> bool:
        if getattr(record, "user_text", False):
            return False
        return True


logger.addFilter(_NoQueryTextFilter())

# Связывание адаптеров с реестром покрытия: адаптеры внешних источников
# регистрируют себя при импорте; украинские обёртки зарегистрированы выше.
sources.load_adapters()


app = mcp.http_app(path="/mcp")


def _prewarm_cache() -> None:
    """Load into memory only laws already cached on disk — no network, no Playwright."""
    laws = registry.list_laws()
    warmed = 0
    for law_item in laws:
        law_id = law_item["id"]
        try:
            entry = cache.get_cached_entry(law_id, allow_stale=False)
            if entry:
                warmed += 1
        except Exception as error:
            logger.warning("prewarm disk-read failed for %s: %s", law_id, error)
    logger.info("Disk-prewarm complete: %d/%d laws in memory (no network used)", warmed, len(laws))


def main() -> None:
    # Профиль применяется при запуске: недопустимая конфигурация не стартует (T125).
    yurko_profile.enforce_profile()
    if TRANSPORT == "http":
        logger.info(
            "Starting yurko v%s (HTTP on port %d, profile=%s)",
            __version__,
            PORT,
            yurko_profile.current_profile().value,
        )
        if API_KEY:
            logger.info("Authentication enabled")
        else:
            logger.warning("Authentication disabled. Set UKRAINE_LAWS_API_KEY in production.")
        threading.Thread(target=_prewarm_cache, daemon=True, name="prewarm").start()
        # Сессия транспорта — носитель памяти доказательств, а не деталь связи
        # (T482). ``stateless_http=True`` открывает новую сессию на каждый
        # HTTP-запрос, поэтому чтение кладёт фрагмент туда, куда следующий вызов
        # уже не смотрит: ``session_context.current_session_id`` берёт идентичность
        # из ``Context.session_id``, а ``citations.SESSION_TEXTS`` хранит записи
        # ключом ``(session_id, evidence_id)``.
        #
        # Чего это стоило, измерено на проде 11.09.2026: ~40 чтений права,
        # ``get_session_ledger`` → ``evidence_total: 0``, ``attest_claim`` →
        # ``evidence_not_in_session``, ``render_attested`` → ``unattested_norm_claim``.
        # Юрист получил пакет с припиской «сверить каждую цитату вручную» —
        # то есть ровно то, ради отмены чего ядро и существует. Риск был записан
        # непроверенным ещё в ``docs/architecture-review-002.md``, а смягчением там
        # назван «один процесс на задачу — stdio в плагине»; после заморозки
        # плагина работа переехала именно на непроверенную ветку.
        #
        # Параметр не заменён на ``False``, а убран: ``False`` — умолчание FastMCP,
        # и явная передача умолчания читалась бы как осознанный выбор режима там,
        # где выбора быть не должно. Проверяется транспортом:
        # ``tests/integration/test_http_evidence_session.py``.
        mcp.run(transport="http", host="0.0.0.0", port=PORT)
    else:
        logger.info(
            "Starting yurko v%s (stdio, profile=%s)",
            __version__,
            yurko_profile.current_profile().value,
        )
        mcp.run()


if __name__ == "__main__":
    main()
