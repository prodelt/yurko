"""Discovery over the official Verkhovna Rada open data document cards."""

from __future__ import annotations

import datetime as dt
import io
import logging
import os
import re
import tempfile
import time
import zipfile
from datetime import timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin, urlparse

import requests

from registries.rada_open_data import OPEN_DATA_USER_AGENT
from sources.transport import SourceTransport, is_settled_refusal

logger = logging.getLogger(__name__)

#: Хост машинного каналу Ради. Редирект у його межах — переїзд файлу; редирект
#: за його межі (насамперед на ``zakon.rada.gov.ua``) — те, чим Рада відповідає
#: на неприйнятий UA, і читати те, що там лежить, не можна: ``robots.txt``
#: публічного сайту — ``User-Agent: * Disallow: /``.
OPEN_DATA_HOST = "data.rada.gov.ua"

#: Стеля часу, який один виклик MCP має право заблокувати на завантаженні
#: каталогу. До рев'ю тікета 15 стелі не було зовсім: три спроби по 60 с плюс
#: відступи 4 і 8 с давали 192 с мовчання в одному виклику. Тайм-аут і кількість
#: спроб нижче підібрані так, щоб :meth:`OpenDataDiscovery.worst_case_seconds`
#: у цю стелю вкладався, і це перевіряється тестом, а не обіцянкою.
DOWNLOAD_BUDGET_SECONDS = 90.0

#: Тайм-аут однієї спроби. 25 с: три спроби з відступами 4 і 8 с — 87 с.
DEFAULT_REQUEST_TIMEOUT = 25

#: Член архіву, за яким видно, що завантажили каталог, а не половину файлу.
ARCHIVE_MEMBER = "doc.txt"


class OpenDataUnavailable(RuntimeError):
    """Каталог карток недоступний і придатної копії на диску немає."""


class OpenDataDiscovery:
    """Finds law IDs in the official open data document-card export.

    Каталог лежить на ``data.rada.gov.ua`` — машинному каналі, який відповідає
    лише на точний User-Agent ``OpenData``; будь-який інший дістає 302 на
    ``zakon.rada.gov.ua``, де ``robots.txt`` — ``Disallow: /``.

    Рада рве приблизно одне рукостискання з п'ятнадцяти, тому обрив
    повторюється; ім'я, якого немає, і закритий порт — ні (та сама межа, що в
    :func:`sources.transport.is_settled_refusal`, одна на випуск).

    Копія на диску переживає недоступність джерела, але ніколи не видається за
    свіжу: відповідь несе ``catalogue_freshness`` (``live`` / ``cache`` /
    ``stale``) і дату, якою каталог справді датований.
    """

    _source_url = "https://data.rada.gov.ua/ogd/zak/laws/data/csv/doc.zip"
    _ttl_hours = 24
    _min_query_chars = 2
    _max_redirects = 3
    _accepted_at_pattern = re.compile(r":(\d{8}):")
    _tokenizer = re.compile(r"[A-Za-zА-Яа-яІіЇїЄєҐґ0-9-]+")

    def __init__(
        self,
        cache_dir: str | Path,
        request_timeout: int = DEFAULT_REQUEST_TIMEOUT,
        attempts: int = 3,
        sleeper: Callable[[float], None] = time.sleep,
        session: Any | None = None,
    ) -> None:
        self._cache_dir = Path(cache_dir)
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._archive_path = self._cache_dir / "doc.zip"
        #: Мережа — лише через спільний транспорт: скинуте після простою
        #: пулове з'єднання інакше стає хибним «джерело недоступне»
        #: (живий збій 15.09.2026, :mod:`sources.transport`).
        self._transport = SourceTransport(session or requests.Session())
        self._request_timeout = request_timeout
        self._attempts = max(1, attempts)
        self._sleep = sleeper

    @staticmethod
    def worst_case_seconds(attempts: int, request_timeout: float) -> float:
        """Найгірший час завантаження: усі спроби до тайм-ауту плюс відступи."""
        backoff = sum(4 * (index + 1) for index in range(max(0, attempts - 1)))
        return max(1, attempts) * float(request_timeout) + backoff

    def search(self, query: str, max_results: int = 10) -> dict[str, Any]:
        normalized_query = " ".join(str(query or "").strip().lower().split())
        if len(normalized_query) < self._min_query_chars:
            freshness = "cache" if self._archive_is_fresh() else "stale"
            return self._payload(
                query,
                [],
                freshness=freshness if self._archive_path.exists() else "live",
            )

        archive, freshness = self._read_archive()
        rows = self._rank_rows(archive, normalized_query, max_results)
        return self._payload(query, rows[:max_results], freshness=freshness)

    def resolve_best(self, query: str) -> dict[str, Any] | None:
        normalized_query = " ".join(str(query or "").strip().lower().split())
        if len(normalized_query) < self._min_query_chars:
            return None

        payload = self.search(normalized_query, max_results=3)
        results = payload.get("results", [])
        if not results:
            return None

        best = dict(results[0])
        if not self._is_confident_match(best, normalized_query):
            return None
        return best

    def _payload(
        self,
        query: str,
        results: list[dict[str, Any]],
        freshness: str,
    ) -> dict[str, Any]:
        """Відповідь, у якій вік каталогу видно, а не здогадується.

        ``retrieved_at`` — коли відповідь склали; ``catalogue_fetched_at`` —
        коли востаннє вдалося завантажити сам каталог. До рев'ю тікета 15 було
        лише перше, і каталог місячної давнини йшов під сьогоднішньою датою.
        """
        payload: dict[str, Any] = {
            "query": str(query or "").strip(),
            "results": results,
            "found": len(results),
            "source": self._source_url,
            #: Лишається для сумісності; ``catalogue_freshness`` каже більше.
            "from_cache": freshness != "live",
            "catalogue_freshness": freshness,
            "catalogue_fetched_at": self._archive_fetched_at(),
            "retrieved_at": dt.datetime.now(timezone.utc).isoformat(),
        }
        if freshness == "stale":
            payload["stale_since"] = payload["catalogue_fetched_at"]
        payload["freshness_notice"] = self._catalogue_notice(freshness, payload)
        return payload

    @staticmethod
    def _catalogue_notice(freshness: str, payload: dict[str, Any]) -> str:
        """Речення про вік каталогу — те, що юрист читає, а не поле контракту."""
        if freshness != "stale":
            return ""
        fetched_at = str(payload.get("catalogue_fetched_at") or "")
        return (
            "Каталог карток застарілий: завантажити свіжий не вдалося, відповідь "
            f"зібрано з копії від {fetched_at}. Актів, ухвалених після цієї дати, "
            "у ній немає взагалі."
        )

    def _read_archive(self) -> tuple[bytes, str]:
        """Архів і стан каталогу: ``live`` / ``cache`` / ``stale``."""
        if self._archive_is_fresh():
            return self._archive_path.read_bytes(), "cache"

        try:
            content = self._download()
        except Exception as error:
            if self._archive_path.exists():
                # Застарілий каталог карток — відповідь; відмова — ні. Але
                # відповідь із названим віком: див. :meth:`_payload`.
                logger.info("doc.zip download failed, serving the stale copy: %s", error)
                return self._archive_path.read_bytes(), "stale"
            raise OpenDataUnavailable(str(error)) from error
        self._write_atomic(content)
        return content, "live"

    def _download(self) -> bytes:
        """Каталог із мережі: повтори, бюджет часу й перевірка перед заміною."""
        last_error: Exception | None = None
        deadline = time.monotonic() + DOWNLOAD_BUDGET_SECONDS
        for attempt in range(self._attempts):
            if attempt and time.monotonic() >= deadline:
                break
            try:
                content = self._fetch_once()
            except OpenDataUnavailable:
                # Редирект за межі машинного каналу — відповідь, а не збій:
                # повторювати нічого.
                raise
            except requests.RequestException as error:
                if is_settled_refusal(error):
                    # Імені немає або порт закритий: мережа відповіла остаточно.
                    raise OpenDataUnavailable(str(error)) from error
                last_error = error
                logger.debug("doc.zip attempt %d failed: %s", attempt + 1, error)
                if attempt < self._attempts - 1:
                    self._sleep(4 * (attempt + 1))
                continue
            self._reject_unusable_archive(content)
            return content
        raise OpenDataUnavailable(f"doc.zip failed after {self._attempts} attempts: {last_error}")

    def _fetch_once(self) -> bytes:
        """Один GET із ручним переходом за редиректами в межах машинного каналу."""
        url = self._source_url
        for _ in range(self._max_redirects + 1):
            response = self._transport.get(
                url,
                headers={"User-Agent": OPEN_DATA_USER_AGENT},
                timeout=self._request_timeout,
                allow_redirects=False,
            )
            if response.status_code not in (301, 302, 303, 307, 308):
                response.raise_for_status()
                return bytes(response.content)
            location = str(response.headers.get("Location", "") or "")
            target = urljoin(url, location)
            host = (urlparse(target).hostname or "").lower()
            if host != OPEN_DATA_HOST:
                raise OpenDataUnavailable(
                    f"data.rada redirected doc.zip to {target or '?'} — "
                    "the OpenData User-Agent was not accepted; reading the public site "
                    "is not an option (robots.txt: Disallow: /)"
                )
            url = target
        raise OpenDataUnavailable(f"doc.zip: more than {self._max_redirects} redirects")

    def _reject_unusable_archive(self, content: bytes) -> None:
        """Відмовити ДО ``os.replace``: заміна атомарна й тому незворотна.

        13,4 МБ, обірвані на половині (чи підмінені сторінкою помилки), стають
        «свіжим» каталогом, після чого TTL цілу добу каже «свіжо», а кожен
        виклик падає на розпакуванні. Дешева перевірка — відкрити архів і
        побачити в ньому потрібний член.
        """
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                archive.getinfo(ARCHIVE_MEMBER)
        except (zipfile.BadZipFile, KeyError, OSError) as error:
            raise OpenDataUnavailable(
                f"doc.zip is not a usable catalogue ({len(content)} bytes): {error}"
            ) from error

    def _archive_fetched_at(self) -> str:
        """Коли каталог на диску востаннє завантажувався; порожньо — його немає."""
        if not self._archive_path.exists():
            return ""
        return dt.datetime.fromtimestamp(
            self._archive_path.stat().st_mtime, tz=timezone.utc
        ).isoformat()

    def _archive_is_fresh(self) -> bool:
        if not self._archive_path.exists():
            return False
        modified_at = dt.datetime.fromtimestamp(
            self._archive_path.stat().st_mtime,
            tz=timezone.utc,
        )
        return dt.datetime.now(timezone.utc) - modified_at < dt.timedelta(hours=self._ttl_hours)

    def _write_atomic(self, payload: bytes) -> None:
        tmp_file = tempfile.NamedTemporaryFile(
            mode="wb",
            dir=self._archive_path.parent,
            suffix=".tmp",
            delete=False,
        )
        try:
            with tmp_file as handle:
                handle.write(payload)
            os.replace(tmp_file.name, self._archive_path)
        finally:
            tmp_path = Path(tmp_file.name)
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)

    def _rank_rows(
        self,
        archive: bytes,
        normalized_query: str,
        max_results: int,
    ) -> list[dict[str, Any]]:
        tokens = self._tokenize(normalized_query)
        scored: list[tuple[float, dict[str, Any]]] = []

        with zipfile.ZipFile(io.BytesIO(archive)) as zip_file:
            with zip_file.open("doc.txt") as raw_file:
                for raw_line in raw_file:
                    parsed = self._parse_line(raw_line)
                    if parsed is None:
                        continue
                    score = self._score(parsed, normalized_query, tokens)
                    if score <= 0:
                        continue
                    scored.append((score, parsed))

        scored.sort(key=lambda item: item[0], reverse=True)
        return [item[1] for item in scored[: max(max_results, 1)]]

    def _parse_line(self, raw_line: bytes) -> dict[str, Any] | None:
        try:
            line = raw_line.decode("windows-1251").strip()
        except UnicodeDecodeError:
            line = raw_line.decode("utf-8", errors="replace").strip()
        parts = line.split("\t")
        if len(parts) < 3:
            return None

        law_id = parts[1].strip()
        title = parts[2].strip()
        if not law_id or not title:
            return None

        accepted_at = self._accepted_at(parts[5] if len(parts) > 5 else "")
        updated_at = parts[8].strip() if len(parts) > 8 else ""
        url = f"https://zakon.rada.gov.ua/laws/show/{law_id}"
        return {
            "law_id": law_id,
            "title": title,
            "url": url,
            "print_url": f"{url}/print",
            "accepted_at": accepted_at,
            "updated_at": updated_at,
            "source": "rada_open_data_doc_cards",
        }

    def _accepted_at(self, raw_details: str) -> str:
        match = self._accepted_at_pattern.search(raw_details)
        return match.group(1) if match else ""

    def _tokenize(self, query: str) -> list[str]:
        return [token.lower() for token in self._tokenizer.findall(query)]

    def _score(
        self,
        row: dict[str, Any],
        normalized_query: str,
        tokens: list[str],
    ) -> float:
        law_id = str(row["law_id"]).lower()
        title = str(row["title"]).lower()
        if normalized_query == law_id:
            return 10_000.0
        if normalized_query in law_id:
            return 5_000.0
        if normalized_query in title:
            return 2_000.0 + len(normalized_query) / max(len(title), 1)
        matches = sum(1 for token in tokens if token in title or token in law_id)
        return float(matches)

    def _is_confident_match(self, row: dict[str, Any], normalized_query: str) -> bool:
        law_id = str(row["law_id"]).lower()
        title = str(row["title"]).lower()
        if normalized_query == law_id or normalized_query in law_id:
            return True

        tokens = self._tokenize(normalized_query)
        meaningful_tokens = [token for token in tokens if len(token) >= 4]
        if normalized_query in title and (
            len(normalized_query) >= 6 or len(meaningful_tokens) >= 2
        ):
            return True
        if not meaningful_tokens:
            return False
        return all(token in title for token in meaningful_tokens)
