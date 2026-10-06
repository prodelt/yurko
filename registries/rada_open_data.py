"""Client for the official Verkhovna Rada open data API.

Two channels, two hosts, two User-Agent profiles — they are not interchangeable:

* ``data.rada.gov.ua`` serves texts and bulk dumps and only answers to the exact
  User-Agent string ``OpenData``. Any other value earns a 302 to the public site,
  which returns a 34 KB shell instead of the law — so a redirect here is a
  configuration error, never a success.
* ``zakon.rada.gov.ua`` carries the only working full-text search engine and wants
  a browser User-Agent; with ``OpenData`` it answers 403.

Measured behaviour behind these choices: `docs/research-rada-search.md`.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import os
import re
import tempfile
import time
import urllib.parse
from datetime import timezone
from pathlib import Path
from typing import Any, Callable

import requests

from registries.scraper import BackoffSentinel, _HtmlTextExtractor

logger = logging.getLogger(__name__)

OPEN_DATA_USER_AGENT = "OpenData"
#: Браузерний UA тут — свідоме рішення власника, а не залишок старої машини:
#: єдиний робочий повнотекстовий рушій Ради живе на ``zakon.rada.gov.ua`` і на
#: ``OpenData`` відповідає 403. Записано в карті покриття поряд із можливістю
#: ``ua_rada_open_data``/``search_free_text`` (тікет 25) — щоб наступне рев'ю не
#: починало те саме розслідування заново.
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)

DATA_HOST = "https://data.rada.gov.ua"
ZAKON_HOST = "https://zakon.rada.gov.ua"
DUMP_PATH = "/ogd/zak/perv/text/texts.zip"

# Document type codes (data.rada.gov.ua/ogd/zak/laws/data/csv/typ.txt)
TYPE_LAW = "1"
TYPE_CODE = "21"

# Document status codes (.../csv/stan.txt); 5 == "Чинний"
STATUS_IN_FORCE = "5"

_COUNT_RE = re.compile(r"(?is)Знайдено</a>\s*<strong>(\d+)</strong>")
_TOKEN_RE = re.compile(r"/laws/main/(u[0-9a-f-]{36})")
_PARAMS_RE = re.compile(r'(?is)<span class="find_params">(.*?)</span>')
_ITEM_RE = re.compile(
    r'(?is)<li>\s*<div class="doc">\s*<a href="[^"]*/laws/show/([^"?#]+?)(?:/sp:[^"]*)?"[^>]*'
    r'alt="([^"]*)"[^>]*>(.*?)</a>'
)
_PAGES_RE = re.compile(r"/laws/main/u[0-9a-f-]{36}/page(\d+)")
_TAG_RE = re.compile(r"(?s)<[^>]+>")


class RadaOpenDataError(RuntimeError):
    """Raised when a Rada channel cannot be used as documented."""


class RadaDocumentNotFound(RadaOpenDataError):
    """The open data host says this document does not exist (404).

    Distinct from every other failure because it is an answer, not an outage:
    retrying it, falling back to the print page, or starting a browser only
    turns a definitive "no such law" into a minute of waiting followed by
    "source temporarily unavailable", which is both slow and untrue.
    """


def _now() -> dt.datetime:
    return dt.datetime.now(timezone.utc)


def _decode(payload: bytes) -> str:
    """Rada mixes utf-8 and windows-1251 — the WAF pages are always cp1251."""
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError:
        return payload.decode("windows-1251", errors="replace")


def _strip_tags(fragment: str) -> str:
    return " ".join(_TAG_RE.sub(" ", fragment).split())


class RadaOpenDataClient:
    """Reads texts, dumps and search results from the two Rada hosts."""

    _dump_ttl_hours = 1  # texts.zip is rebuilt hourly
    _search_ttl_hours = 24

    def __init__(
        self,
        cache_dir: str | Path,
        request_timeout: int = 60,
        attempts: int = 3,
        sleeper: Callable[[float], None] = time.sleep,
        backoff: BackoffSentinel | None = None,
    ) -> None:
        self._cache_dir = Path(cache_dir)
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._search_cache_dir = self._cache_dir / "rada_search"
        self._search_cache_dir.mkdir(parents=True, exist_ok=True)
        self._dump_path = self._cache_dir / "texts.zip"
        self._request_timeout = request_timeout
        self._attempts = max(1, attempts)
        self._sleep = sleeper
        # One sentinel for the whole project — scraper.py owns the type.
        self.backoff = backoff if backoff is not None else BackoffSentinel()

    # ------------------------------------------------------------------
    # Channel 1 — texts (data.rada.gov.ua, User-Agent: OpenData)
    # ------------------------------------------------------------------

    def fetch_document(self, law_id: str) -> dict[str, Any]:
        """Return the full current text of one document.

        Redirects are refused on purpose: following one lands on zakon.rada.gov.ua,
        which answers with a shell page that looks like a success.
        """
        law_id = str(law_id or "").strip()
        if not law_id:
            raise RadaOpenDataError("law_id is required")
        if self.backoff.is_blocked(law_id):
            status = self.backoff.status(law_id)
            raise RadaOpenDataError(f"Law {law_id} is in backoff until {status.get('retry_after')}")

        url = f"{DATA_HOST}/laws/show/{urllib.parse.quote(law_id, safe='/')}"
        try:
            response = self._request(url, OPEN_DATA_USER_AGENT, allow_redirects=False)
        except RadaOpenDataError:
            self.backoff.mark_failed(law_id)
            raise

        if response.status_code in (301, 302, 303, 307, 308):
            self.backoff.mark_failed(law_id)
            raise RadaOpenDataError(
                f"data.rada redirected {law_id} to {response.headers.get('Location', '?')} — "
                "the OpenData User-Agent was not accepted"
            )
        if response.status_code == 404:
            # Not a failure to record against the host: the host answered.
            raise RadaDocumentNotFound(f"data.rada has no document {law_id} (404)")
        if response.status_code != 200:
            self.backoff.mark_failed(law_id)
            raise RadaOpenDataError(f"data.rada answered {response.status_code} for {law_id}")

        html = _decode(response.content)
        extractor = _HtmlTextExtractor()
        extractor.feed(html)
        text = extractor.as_text()
        # 500 chars is the same floor scraper.py uses to tell a law from an error page.
        if len(text) < 500:
            self.backoff.mark_failed(law_id)
            raise RadaOpenDataError(f"data.rada returned {len(text)} chars for {law_id}")

        self.backoff.mark_success(law_id)
        return {
            "law_id": law_id,
            "text": text,
            "url": url,
            "public_url": f"{ZAKON_HOST}/laws/show/{law_id}",
            "source": "rada_open_data",
            "char_count": len(text),
            "html_bytes": len(response.content),
            "retrieved_at": _now().isoformat(),
        }

    def download_dump(self, force: bool = False) -> dict[str, Any]:
        """Download ``texts.zip`` — every primary act's current text, ~47 MB."""
        if not force and self._dump_is_fresh():
            stat = self._dump_path.stat()
            return {
                "path": str(self._dump_path),
                "bytes": stat.st_size,
                "from_cache": True,
                "retrieved_at": dt.datetime.fromtimestamp(
                    stat.st_mtime, tz=timezone.utc
                ).isoformat(),
            }

        response = self._request(f"{DATA_HOST}{DUMP_PATH}", OPEN_DATA_USER_AGENT)
        if response.status_code != 200:
            raise RadaOpenDataError(f"dump download answered {response.status_code}")
        self._write_atomic(self._dump_path, response.content)
        return {
            "path": str(self._dump_path),
            "bytes": len(response.content),
            "from_cache": False,
            "last_modified": response.headers.get("Last-Modified", ""),
            "retrieved_at": _now().isoformat(),
        }

    def dump_freshness(self) -> dict[str, Any]:
        """Ask whether the dump moved, without pulling 47 MB to find out."""
        response = self._request(f"{DATA_HOST}{DUMP_PATH}", OPEN_DATA_USER_AGENT, method="HEAD")
        remote_modified = response.headers.get("Last-Modified", "")
        local_modified = ""
        stale = True
        if self._dump_path.exists():
            local_dt = dt.datetime.fromtimestamp(self._dump_path.stat().st_mtime, tz=timezone.utc)
            local_modified = local_dt.isoformat()
            remote_dt = self._parse_http_date(remote_modified)
            stale = remote_dt is None or remote_dt > local_dt
        return {
            "url": f"{DATA_HOST}{DUMP_PATH}",
            "remote_last_modified": remote_modified,
            "remote_bytes": int(response.headers.get("Content-Length", 0) or 0),
            "local_last_modified": local_modified,
            "local_exists": self._dump_path.exists(),
            "stale": stale,
            "checked_at": _now().isoformat(),
        }

    def recent_changes(self, max_results: int = 50) -> dict[str, Any]:
        """Documents Rada changed in the last few days (``/laws/main/r.json``)."""
        response = self._request(f"{DATA_HOST}/laws/main/r.json", OPEN_DATA_USER_AGENT)
        if response.status_code != 200:
            raise RadaOpenDataError(f"r.json answered {response.status_code}")
        try:
            payload = json.loads(_decode(response.content))
        except ValueError as error:
            raise RadaOpenDataError(f"r.json is not JSON: {error}") from error

        # Measured shape: {"cnt": N, "from": 1, "max": M, "list": [{nreg, nazva, typ, ...}]}
        if isinstance(payload, dict):
            rows: Any = payload.get("list", [])
            total = int(payload.get("cnt", 0) or 0)
        elif isinstance(payload, list):
            rows = payload
            total = len(payload)
        else:
            rows = []
            total = 0

        results: list[dict[str, Any]] = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            law_id = str(row.get("nreg") or row.get("id") or "").strip()
            if not law_id:
                continue
            changed_at = row.get("orgdat") or row.get("poddat") or row.get("pridat") or ""
            results.append(
                {
                    "law_id": law_id,
                    "title": str(row.get("nazva") or row.get("name") or "").strip(),
                    "doc_type": row.get("typ"),
                    "changed_at": str(changed_at).strip(),
                    "url": f"{ZAKON_HOST}/laws/show/{law_id}",
                }
            )
            if len(results) >= max_results:
                break
        return {
            "results": results,
            "found": len(results),
            "total": total,
            "source": f"{DATA_HOST}/laws/main/r.json",
            "retrieved_at": _now().isoformat(),
        }

    # ------------------------------------------------------------------
    # Channel 2 — full-text search (zakon.rada.gov.ua, browser UA)
    # ------------------------------------------------------------------

    def search(
        self,
        text: str,
        textl: str = "2",
        bool_mode: str = "and",
        types: tuple[str, ...] = (),
        status: str = "",
        max_results: int = 50,
        use_cache: bool = True,
    ) -> dict[str, Any]:
        """Full-text search. Handles both answer shapes Rada can return.

        With no requisite filter Rada may shortcut to a 302 carrying a handful of
        ids — a different, narrower "by subject" lookup, not a truncated listing.
        Passing ``types`` or ``status`` suppresses the shortcut and makes the
        answer deterministic.
        """
        query = " ".join(str(text or "").strip().split())
        if len(query) < 3:
            raise RadaOpenDataError("Rada requires at least 3 characters in a search query")

        params: dict[str, Any] = {
            "find": "3",
            "user": "a",
            "text": query,
            "textl": str(textl),
            "bool": bool_mode,
            "max": "100" if max_results > 50 else "50",
        }
        if types:
            # `typs` MUST be repeated, one value per parameter. A comma-joined
            # "1,21" is accepted with a 200 and then silently ignored: measured
            # 563 hits (постанови included) against 47 for repeated typs.
            params["typs"] = list(types)
        if status:
            params["stan"] = status
            params["stanl"] = "0"

        cache_key = self._search_cache_key(params)
        if use_cache:
            cached = self._read_search_cache(cache_key)
            if cached is not None:
                cached["from_cache"] = True
                return cached

        url = f"{ZAKON_HOST}/laws/main" if (types or status) else f"{ZAKON_HOST}/laws/find/a"
        response = self._request(
            url,
            BROWSER_USER_AGENT,
            params=params,
            allow_redirects=False,
        )

        if response.status_code in (301, 302, 303, 307, 308):
            payload = self._parse_redirect(query, response.headers.get("Location", ""))
        elif response.status_code == 200:
            payload = self._parse_results_page(query, _decode(response.content), max_results)
        elif response.status_code == 403:
            # 403 here means the wrong User-Agent profile, not throttling — retrying is pointless.
            raise RadaOpenDataError("zakon.rada refused the search (403): wrong User-Agent profile")
        else:
            raise RadaOpenDataError(f"search answered {response.status_code}")

        payload["from_cache"] = False
        if use_cache:
            self._write_search_cache(cache_key, payload)
        return payload

    def _parse_redirect(self, query: str, location: str) -> dict[str, Any]:
        marker = "/laws/main/"
        raw = location.split(marker, 1)[1] if marker in location else ""
        raw = raw.split("?", 1)[0].split("#", 1)[0]
        ids = [urllib.parse.unquote(part.strip()) for part in raw.split(",") if part.strip()]
        results = [
            {
                "law_id": law_id,
                "title": "",
                "status": "",
                "url": f"{ZAKON_HOST}/laws/show/{law_id}",
            }
            for law_id in ids
        ]
        return {
            "query": query,
            "format": "redirect",
            "found": len(results),
            "results": results,
            "token": "",
            "params_echo": "",
            "pages": 1,
            "retrieved_at": _now().isoformat(),
        }

    def _parse_results_page(self, query: str, html: str, max_results: int) -> dict[str, Any]:
        # The <h1> names the collection that was searched ("Всі документи — 294280"),
        # never the result — reading it as the count is a mistake already made once.
        # `params_echo` is likewise not proof that every filter applied: it lists the
        # text and status clauses but stays silent about `typs`, which does apply
        # (47 hits with it, 563 without). Judge the type filter by the results.
        count_match = _COUNT_RE.search(html)
        token_match = _TOKEN_RE.search(html)
        params_match = _PARAMS_RE.search(html)
        pages = [int(page) for page in _PAGES_RE.findall(html)]

        results: list[dict[str, Any]] = []
        for law_id, state, title_fragment in _ITEM_RE.findall(html):
            decoded_id = urllib.parse.unquote(law_id)
            results.append(
                {
                    "law_id": decoded_id,
                    "title": _strip_tags(title_fragment),
                    "status": state.strip(),
                    "url": f"{ZAKON_HOST}/laws/show/{decoded_id}",
                }
            )
            if len(results) >= max_results:
                break

        return {
            "query": query,
            "format": "page",
            "found": int(count_match.group(1)) if count_match else len(results),
            "results": results,
            "token": token_match.group(1) if token_match else "",
            "params_echo": _strip_tags(params_match.group(1)) if params_match else "",
            "pages": max(pages) if pages else 1,
            "retrieved_at": _now().isoformat(),
        }

    # ------------------------------------------------------------------
    # Plumbing
    # ------------------------------------------------------------------

    def _request(
        self,
        url: str,
        user_agent: str,
        method: str = "GET",
        params: dict[str, Any] | None = None,
        allow_redirects: bool = True,
    ) -> requests.Response:
        """One request with retries.

        Rada drops roughly one connection in fifteen at the TLS handshake, with no
        dependence on pacing — a retry, not a throttle, is the answer. HTTP codes
        are returned as they are: 403 and 302 carry meaning the caller must read.
        """
        last_error: Exception | None = None
        for attempt in range(self._attempts):
            try:
                return requests.request(
                    method,
                    url,
                    headers={"User-Agent": user_agent},
                    params=params,
                    timeout=self._request_timeout,
                    allow_redirects=allow_redirects,
                )
            except Exception as error:  # noqa: BLE001 — connection resets are the norm here
                last_error = error
                logger.debug("rada request attempt %d failed for %s: %s", attempt + 1, url, error)
                if attempt < self._attempts - 1:
                    self._sleep(4 * (attempt + 1))
        raise RadaOpenDataError(
            f"request to {url} failed after {self._attempts} attempts: {last_error}"
        )

    def _dump_is_fresh(self) -> bool:
        if not self._dump_path.exists():
            return False
        modified_at = dt.datetime.fromtimestamp(self._dump_path.stat().st_mtime, tz=timezone.utc)
        return _now() - modified_at < dt.timedelta(hours=self._dump_ttl_hours)

    @staticmethod
    def _parse_http_date(value: str) -> dt.datetime | None:
        if not value:
            return None
        try:
            from email.utils import parsedate_to_datetime

            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed

    def _search_cache_key(self, params: dict[str, Any]) -> str:
        payload = json.dumps(params, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]

    def _read_search_cache(self, key: str) -> dict[str, Any] | None:
        path = self._search_cache_dir / f"{key}.json"
        if not path.exists():
            return None
        modified_at = dt.datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        if _now() - modified_at >= dt.timedelta(hours=self._search_ttl_hours):
            return None
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return loaded if isinstance(loaded, dict) else None

    def _write_search_cache(self, key: str, payload: dict[str, Any]) -> None:
        path = self._search_cache_dir / f"{key}.json"
        self._write_atomic(path, json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    @staticmethod
    def _write_atomic(path: Path, payload: bytes) -> None:
        tmp_file = tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, suffix=".tmp", delete=False
        )
        try:
            with tmp_file as handle:
                handle.write(payload)
            os.replace(tmp_file.name, path)
        finally:
            tmp_path = Path(tmp_file.name)
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)
