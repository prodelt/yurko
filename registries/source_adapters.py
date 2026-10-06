"""Source adapter contracts and implementations."""

from __future__ import annotations

import datetime as dt
import logging
from datetime import timezone
from typing import Any, Protocol

import requests

from core.contracts import (
    SourceAdapterError,
    SourceDocument,
    SourceHealth,
    SourceMetadata,
    SourcePolicy,
)
from registries.law_registry import LawRegistry
from registries.scraper import Scraper
from search.search_engine import SearchEngine

logger = logging.getLogger("ukraine-laws")


class SourceAdapter(Protocol):
    """Common contract for official source adapters."""

    name: str
    source_policy: SourcePolicy

    def resolve(self, query: str) -> dict[str, Any] | None: ...

    def fetch(self, law_id: str, law_info: dict[str, Any]) -> SourceDocument: ...

    def fetch_law(self, law_id: str, law_info: dict[str, Any]) -> dict[str, Any]: ...

    def search(self, law_id: str, query: str, max_results: int = 5) -> list[dict[str, Any]]: ...

    def metadata(self, law_id: str) -> SourceMetadata: ...

    def health(self) -> SourceHealth: ...


class ZakonRadaAdapter:
    """Adapter for zakon.rada.gov.ua using the existing scraper implementation."""

    name = "zakon_rada"
    source_policy = SourcePolicy.HTML_PRINT

    def __init__(
        self,
        registry: LawRegistry,
        search_engine: SearchEngine,
        scraper: Any | None = None,
    ) -> None:
        self._scraper = scraper or Scraper()
        self._registry = registry
        self._search_engine = search_engine
        self.backoff = self._scraper.backoff

    def resolve(self, query: str) -> dict[str, Any] | None:
        return self._registry.resolve(query)

    def fetch(self, law_id: str, law_info: dict[str, Any]) -> SourceDocument:
        try:
            fetched = self._scraper.fetch_law(law_id, law_info)
            document = SourceDocument(
                law_id=str(fetched.get("law_id") or law_id),
                title=str(fetched.get("title") or law_info.get("title") or law_id),
                url=str(fetched.get("url") or law_info.get("url") or ""),
                text=str(fetched.get("text") or ""),
                source=str(fetched.get("source") or "scraped"),
                source_policy=self.source_policy,
                adapter=self.name,
                amendment_date=fetched.get("amendment_date"),
                retrieved_at=dt.datetime.now(timezone.utc).isoformat(),
                content_hash=fetched.get("content_hash"),
            )
        except Exception as error:
            raise SourceAdapterError(f"{self.name} failed to fetch {law_id}") from error
        return document.with_hash()

    def fetch_law(self, law_id: str, law_info: dict[str, Any]) -> dict[str, Any]:
        return self.fetch(law_id, law_info).as_cache_payload()

    def search(self, law_id: str, query: str, max_results: int = 5) -> list[dict[str, Any]]:
        law = self._registry.get_law(law_id)
        if law is None:
            return []
        document = self.fetch(law_id, law)
        articles = self._search_engine.parse_articles(document.text)
        matched = self._search_engine.match_query(document.text, query, articles=articles)
        if matched is None:
            return []
        return [
            {
                "law_id": law_id,
                "title": document.title,
                "snippet": matched["snippet"],
                "url": document.url,
                "adapter": self.name,
                "source_policy": self.source_policy.value,
            }
        ][:max_results]

    def metadata(self, law_id: str) -> SourceMetadata:
        law = self._registry.get_law(law_id) or {}
        return SourceMetadata(
            law_id=law_id,
            adapter=self.name,
            source_policy=self.source_policy,
            url=str(law.get("url", "")),
            title=str(law.get("title", law_id)),
            backoff=self.backoff.status(law_id),
        )

    def _check_connectivity(self) -> bool:
        """Probe zakon.rada.gov.ua with a minimal GET request."""
        try:
            response = requests.head(
                "https://zakon.rada.gov.ua/",
                timeout=5,
                allow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0 ukraine-laws-mcp/2"},
            )
            return 200 <= response.status_code < 400
        except Exception as error:
            logger.debug("zakon.rada.gov.ua connectivity check failed: %s", error)
            return False

    def health(self) -> SourceHealth:
        """Report adapter health: distinguish 'source accessible' from 'data available'."""
        # Check if source is actually reachable (not just cached data)
        is_accessible = self._check_connectivity()

        return SourceHealth(
            adapter=self.name,
            ok=is_accessible,
            source_policy=self.source_policy,
            details={
                "registry_laws": len(self._registry.list_laws()),
                "source_accessible": is_accessible,
                "backoff_status": self.backoff.status("main"),
            },
        )
