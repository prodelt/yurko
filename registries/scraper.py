"""Network fetcher for Ukrainian law texts."""

from __future__ import annotations

import concurrent.futures
import datetime as dt
import logging
import re
import threading
from datetime import timezone
from html.parser import HTMLParser
from typing import Any

import requests

from sources.stub_detection import detect_stub
from sources.transport import SourceTransport

logger = logging.getLogger("ukraine-laws")


class BackoffSentinel:
    """Exponential retry sentinel for unstable sources."""

    def __init__(self) -> None:
        self._delays_seconds = [60, 300, 1800, 7200, 86400]
        self._state: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def is_blocked(self, law_id: str) -> bool:
        with self._lock:
            entry = self._state.get(law_id)
            if not entry:
                return False
            retry_after = entry.get("retry_after")
            return (
                isinstance(retry_after, dt.datetime) and dt.datetime.now(timezone.utc) < retry_after
            )

    def mark_failed(self, law_id: str) -> None:
        with self._lock:
            entry = self._state.get(law_id, {})
            attempt = int(entry.get("attempt", 0)) + 1
            delay = self._delays_seconds[min(attempt - 1, len(self._delays_seconds) - 1)]
            self._state[law_id] = {
                "attempt": attempt,
                "retry_after": dt.datetime.now(timezone.utc) + dt.timedelta(seconds=delay),
            }

    def mark_success(self, law_id: str) -> None:
        with self._lock:
            self._state.pop(law_id, None)

    def status(self, law_id: str) -> dict[str, Any]:
        with self._lock:
            entry = self._state.get(law_id)
            if not entry:
                return {"blocked": False, "attempt": 0, "retry_after": None}
            retry_after = entry.get("retry_after")
            return {
                "blocked": isinstance(retry_after, dt.datetime)
                and dt.datetime.now(timezone.utc) < retry_after,
                "attempt": int(entry.get("attempt", 0)),
                "retry_after": retry_after.isoformat() if retry_after else None,
            }


class _HtmlTextExtractor(HTMLParser):
    """Extracts visible text from HTML."""

    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self._skip_depth += 1
        if tag in {"p", "div", "li", "h1", "h2", "h3", "h4"}:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        cleaned = data.strip()
        if cleaned:
            self._chunks.append(cleaned)

    def as_text(self) -> str:
        joined = "\n".join(self._chunks)
        return re.sub(r"\n{3,}", "\n\n", joined).strip()


_playwright_semaphore = threading.Semaphore(1)  # at most 1 Chrome process at a time


class Scraper:
    """Fetches law text with print-url first and Playwright fallback."""

    def __init__(
        self,
        request_timeout: int = 15,
        playwright_timeout: int = 35,
        session: Any | None = None,
    ) -> None:
        #: Мережа — лише через спільний транспорт: скинуте після простою
        #: пулове з'єднання інакше стає хибним «джерело недоступне»
        #: (живий збій 15.09.2026, :mod:`sources.transport`).
        self._transport = SourceTransport(session or requests.Session())
        self._request_timeout = request_timeout
        self._playwright_timeout = playwright_timeout
        self.backoff = BackoffSentinel()

    def fetch_law(self, law_id: str, law_info: dict[str, Any]) -> dict[str, Any]:
        if self.backoff.is_blocked(law_id):
            status = self.backoff.status(law_id)
            raise RuntimeError(f"Law {law_id} is in backoff until {status.get('retry_after')}")

        url = str(law_info.get("url", "")).strip()
        print_url = self._build_print_url(law_id, law_info)
        title = str(law_info.get("title", law_id))
        amendment_date = self.check_amendment_date(law_id, law_info)

        text = self._try_print_url(print_url)
        if text:
            self.backoff.mark_success(law_id)
            return self._build_result(
                law_id, title, url, text, source="print_url", amendment_date=amendment_date
            )

        text = self._try_playwright(url)
        if text:
            self.backoff.mark_success(law_id)
            return self._build_result(
                law_id, title, url, text, source="playwright", amendment_date=amendment_date
            )

        self.backoff.mark_failed(law_id)
        raise RuntimeError(f"Unable to fetch law text for {law_id}")

    def check_amendment_date(self, law_id: str, law_info: dict[str, Any]) -> str | None:
        """Return 'YYYY-MM-DD' of last amendment from zakon.rada.gov.ua, or None."""
        url = str(law_info.get("url", "")).strip()
        if not url:
            url = f"https://zakon.rada.gov.ua/laws/show/{law_id}"
        url = url.removesuffix("/print")

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36"
            )
        }
        try:
            resp = self._transport.get(url, headers=headers, timeout=10)
            resp.raise_for_status()
        except Exception as exc:
            logger.debug("Amendment check failed for %s: %s", law_id, exc)
            return None

        # Force UTF-8 decode — zakon.rada.gov.ua may send windows-1251 declared as utf-8
        try:
            text = resp.content.decode("utf-8")
        except UnicodeDecodeError:
            text = resp.content.decode("windows-1251", errors="replace")

        match = re.search(r"[Рр]едакція від\s+(\d{2})\.(\d{2})\.(\d{4})", text)
        if match:
            d, m, y = match.group(1), match.group(2), match.group(3)
            return f"{y}-{m}-{d}"
        return None

    def _build_print_url(self, law_id: str, law_info: dict[str, Any]) -> str:
        print_url = str(law_info.get("print_url", "")).strip()
        if print_url:
            return print_url

        base_url = str(law_info.get("url", "")).strip()
        if not base_url:
            base_url = f"https://zakon.rada.gov.ua/laws/show/{law_id}"
        return base_url if base_url.endswith("/print") else f"{base_url}/print"

    def _build_result(
        self,
        law_id: str,
        title: str,
        url: str,
        text: str,
        source: str,
        amendment_date: str | None = None,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "law_id": law_id,
            "title": title,
            "url": url,
            "text": text,
            "source": source,
        }
        if amendment_date:
            result["amendment_date"] = amendment_date
        return result

    def _try_print_url(self, print_url: str) -> str | None:
        if not print_url:
            return None

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36"
            )
        }
        for attempt in range(3):
            try:
                response = self._transport.get(
                    print_url, headers=headers, timeout=self._request_timeout, allow_redirects=True
                )
                response.raise_for_status()
                break
            except Exception as error:
                logger.debug(
                    "print_url attempt %d failed for %s: %s", attempt + 1, print_url, error
                )
                if attempt == 2:
                    return None
                import time

                time.sleep(3 * (attempt + 1))
        else:
            return None

        # Force UTF-8 decode — zakon.rada.gov.ua sometimes sends windows-1251
        try:
            html = response.content.decode("utf-8")
        except UnicodeDecodeError:
            html = response.content.decode("windows-1251", errors="replace")

        extractor = _HtmlTextExtractor()
        extractor.feed(html)
        text = extractor.as_text()
        # Not just "is it long enough": an error page, a challenge page and a
        # bare table of contents are all formally successful 200s, and all three
        # are more dangerous than an outright failure because they look healthy
        # (T014, principle III). The 500-char floor is now the last of the
        # checks, not the only one.
        verdict = detect_stub(text)
        if verdict is not None:
            logger.warning(
                "print_url returned a stub for %s: %s", print_url, verdict.as_detected_by()
            )
            return None
        return text

    def _try_playwright(self, url: str) -> str | None:
        if not url:
            return None
        try:
            from playwright.async_api import async_playwright
        except Exception:
            return None

        async def _scrape() -> str:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=True)
                try:
                    page = await browser.new_page()
                    await page.goto(url, wait_until="networkidle", timeout=30000)
                    body: str = await page.inner_text("body")
                    return body.strip()
                finally:
                    await browser.close()

        def _run() -> str:
            import asyncio

            loop = asyncio.new_event_loop()
            try:
                asyncio.set_event_loop(loop)
                return loop.run_until_complete(_scrape())
            finally:
                loop.close()

        acquired = _playwright_semaphore.acquire(timeout=self._playwright_timeout)
        if not acquired:
            logger.warning("Playwright semaphore timed out — skipping browser fallback")
            return None
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(_run)
                try:
                    text = future.result(timeout=self._playwright_timeout)
                except Exception as exc:
                    logger.warning("Playwright fetch failed: %s", exc)
                    return None
        finally:
            _playwright_semaphore.release()
        verdict = detect_stub(text)
        if verdict is not None:
            logger.warning("Playwright returned a stub: %s", verdict.as_detected_by())
            return None
        return text
