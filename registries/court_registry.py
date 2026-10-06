"""Adapter for the Unified State Register of Court Decisions (ЄДРСР).

Source: https://reyestr.court.gov.ua — the official public register. Search is
an HTML form POST; decision pages live at ``/Review/{id}``. Decisions are
immutable once published, so fetched texts are cached on disk with a long TTL.

The base URL is overridable via ``COURT_REGISTRY_URL`` so ops can repoint the
adapter (e.g. a mirror or proxy) without a code release.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import tempfile
from datetime import timezone
from pathlib import Path
from typing import Any

import requests

from core.contracts import SourceAdapterError, SourceHealth, SourcePolicy
from registries.scraper import BackoffSentinel, _HtmlTextExtractor
from sources.stub_detection import detect_stub
from sources.transport import SourceTransport

logger = logging.getLogger("ukraine-laws")

_DEFAULT_BASE_URL = "https://reyestr.court.gov.ua"
_USER_AGENT = "Mozilla/5.0 ukraine-laws-mcp/2"
_REVIEW_LINK = re.compile(r'href="/Review/(\d+)"', re.IGNORECASE)
_ROW_SPLIT = re.compile(r"<tr[\s>]", re.IGNORECASE)
_DECISION_ID = re.compile(r"^\d{1,12}$")
# Decision pages are full documents; anything shorter is a stub/error page.
_MIN_DECISION_CHARS = 200


def _extract_text(html: str) -> str:
    extractor = _HtmlTextExtractor()
    extractor.feed(html)
    return extractor.as_text()


def _antibot_verdict(html: str) -> str | None:
    """Name the marker that shows the register served a challenge, not data.

    The markers themselves moved to ``sources.stub_detection`` in T014: the same
    "is this a document or a stub?" question is asked of every source now, and
    a check that lived only here could not be asked of EUR-Lex or HUDOC. The
    length threshold stays out of it — a challenge page can be long, and this
    call site has its own, stricter minimum for decision bodies.
    """
    verdict = detect_stub(html, min_chars=None)
    if verdict is None:
        return None
    return verdict.as_detected_by()


def _is_antibot_page(html: str) -> bool:
    """Formally successful HTTP 200 that carries no decision data."""
    return _antibot_verdict(html) is not None


#: Слова, за якими сторінка рішення впізнається як сторінка рішення. Перелік
#: короткий і навмисно консервативний: він не намагається розпізнати вид
#: документа, а лише відрізняє текст рішення від сторінки, яка на нього
#: тільки схожа.
_DECISION_MARKERS = (
    "справ",  # «справа», «справі», «справ №»
    "суд",
    "ухвал",
    "рішенн",
    "постанов",
    "вирок",
)


def _identity_mismatch(text: str, decision_id: str) -> str:
    """Чому цей текст не є рішенням із названим ідентифікатором; порожньо — є.

    Карта 006 (T431, FR-661). До неї перевірялися дві речі — заглушка захисту
    й довжина, — і обидві пропускали найгірший випадок: сторінку, яка формально
    є документом, але **іншим**. HTTP 200 із чужим текстом виглядає як успіх,
    і саме тому конституція (принцип III) вимагає впізнавати текст, а не код
    відповіді.

    Перевірка навмисно слабка в один бік: вона не стверджує, що це саме те
    рішення, — вона відкидає те, що рішенням очевидно не є. Сильніша звірка
    можлива лише за номером справи, який викликач часто не знає.
    """
    lowered = text.lower()
    if decision_id in text:
        return ""
    if not any(marker in lowered for marker in _DECISION_MARKERS):
        return (
            "у тексті немає жодної ознаки судового документа "
            "(справа, суд, ухвала, рішення, постанова, вирок)"
        )
    return ""


class CourtDecisionsRegistry:
    """Search and fetch court decisions from the official register."""

    name = "court_decisions"
    source_policy = SourcePolicy.HTML_PRINT
    cache_ttl_days = 365  # published decisions do not change

    def __init__(
        self,
        cache_dir: str | Path,
        session: Any | None = None,
        request_timeout: int = 25,
    ) -> None:
        self._base_url = os.getenv("COURT_REGISTRY_URL", _DEFAULT_BASE_URL).rstrip("/")
        self._cache_dir = Path(cache_dir)
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        #: Мережа — лише через спільний транспорт: скинуте після простою
        #: пулове з'єднання інакше стає хибним «джерело недоступне»
        #: (живий збій 15.09.2026, :mod:`sources.transport`).
        self._transport = SourceTransport(session or requests.Session())
        self._request_timeout = request_timeout
        self.backoff = BackoffSentinel()

    # -- search ----------------------------------------------------------

    def search(
        self,
        query: str = "",
        case_number: str = "",
        max_results: int = 10,
    ) -> dict[str, Any]:
        if self.backoff.is_blocked("search"):
            raise SourceAdapterError(f"{self.name} search is in backoff")

        payload = {
            "SearchExpression": query,
            "CaseNumber": case_number,
            "PagingInfo.ItemsPerPage": str(max_results),
        }
        try:
            # Пошук рішень нічого не змінює — обірваний запит можна повторити.
            response = self._transport.post(
                f"{self._base_url}/",
                safe_to_repeat=True,
                data=payload,
                headers={"User-Agent": _USER_AGENT},
                timeout=self._request_timeout,
            )
            response.raise_for_status()
        except Exception as error:
            self.backoff.mark_failed("search")
            raise SourceAdapterError(f"{self.name} search failed") from error

        if _is_antibot_page(response.text):
            self.backoff.mark_failed("search")
            raise SourceAdapterError(
                f"{self.name} search returned antibot protection page (captcha or login required)"
            )

        self.backoff.mark_success("search")
        results = self._parse_search_results(response.text, max_results)
        return {
            "query": query,
            "case_number": case_number or None,
            "results": results,
            "found": len(results),
            "source": self._base_url,
            "retrieved_at": dt.datetime.now(timezone.utc).isoformat(),
        }

    def _parse_search_results(self, html: str, max_results: int) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in _ROW_SPLIT.split(html):
            match = _REVIEW_LINK.search(row)
            if match is None:
                continue
            decision_id = match.group(1)
            if decision_id in seen:
                continue
            seen.add(decision_id)
            snippet = re.sub(r"\s+", " ", _extract_text(row)).strip()
            results.append(
                {
                    "decision_id": decision_id,
                    "url": f"{self._base_url}/Review/{decision_id}",
                    "snippet": snippet[:500],
                }
            )
            if len(results) >= max_results:
                break
        return results

    # -- fetch one decision ------------------------------------------------

    def get_decision(self, decision_id: str) -> dict[str, Any]:
        decision_id = str(decision_id).strip()
        if not _DECISION_ID.match(decision_id):
            raise ValueError(f"Invalid decision id: '{decision_id}'")

        cached = self._read_cache(decision_id)
        if cached is not None:
            return {**cached, "from_cache": True}

        if self.backoff.is_blocked(decision_id):
            raise SourceAdapterError(f"{self.name} is in backoff for {decision_id}")

        url = f"{self._base_url}/Review/{decision_id}"
        try:
            response = self._transport.get(
                url,
                headers={"User-Agent": _USER_AGENT},
                timeout=self._request_timeout,
            )
            response.raise_for_status()
        except Exception as error:
            self.backoff.mark_failed(decision_id)
            raise SourceAdapterError(f"{self.name} failed to fetch {decision_id}") from error

        if _is_antibot_page(response.text):
            self.backoff.mark_failed(decision_id)
            raise SourceAdapterError(
                f"{self.name} returned antibot protection page for {decision_id} "
                "(captcha or login required)"
            )

        text = _extract_text(response.text)
        if len(text) < _MIN_DECISION_CHARS:
            self.backoff.mark_failed(decision_id)
            raise SourceAdapterError(
                f"{self.name} returned a stub page for {decision_id} "
                f"({len(text)} chars) — decision may not exist"
            )

        mismatch = _identity_mismatch(text, decision_id)
        if mismatch:
            self.backoff.mark_failed(decision_id)
            raise SourceAdapterError(
                f"{self.name} returned a page whose identity does not match "
                f"{decision_id}: {mismatch}"
            )

        self.backoff.mark_success(decision_id)
        entry = {
            "decision_id": decision_id,
            "url": url,
            "text": text,
            "source": self.name,
            "language": "uk",
            "publisher": "Єдиний державний реєстр судових рішень",
            "source_channel": "html_print",
            "retrieved_at": dt.datetime.now(timezone.utc).isoformat(),
        }
        self._write_cache(decision_id, entry)
        return {**entry, "from_cache": False}

    # -- cache -------------------------------------------------------------

    def _cache_path(self, decision_id: str) -> Path:
        return self._cache_dir / f"{decision_id}.json"

    def _read_cache(self, decision_id: str) -> dict[str, Any] | None:
        path = self._cache_path(decision_id)
        if not path.exists():
            return None
        modified_at = dt.datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        if dt.datetime.now(timezone.utc) - modified_at > dt.timedelta(days=self.cache_ttl_days):
            return None
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return loaded if isinstance(loaded, dict) else None

    def _write_cache(self, decision_id: str, entry: dict[str, Any]) -> None:
        path = self._cache_path(decision_id)
        tmp_file = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=self._cache_dir,
            delete=False,
            suffix=".tmp",
        )
        try:
            json.dump(entry, tmp_file, ensure_ascii=False)
            tmp_file.close()
            Path(tmp_file.name).replace(path)
        except OSError as error:
            logger.warning("court decision cache write failed for %s: %s", decision_id, error)
            Path(tmp_file.name).unlink(missing_ok=True)

    # -- health --------------------------------------------------------------

    def health(self) -> SourceHealth:
        return SourceHealth(
            adapter=self.name,
            ok=not self.backoff.is_blocked("search"),
            source_policy=self.source_policy,
            details={
                "base_url": self._base_url,
                "search_backoff": self.backoff.status("search"),
                "cached_decisions": sum(1 for _ in self._cache_dir.glob("*.json")),
            },
        )
