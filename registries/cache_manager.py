"""Cache layer for law texts with atomic file writes."""

from __future__ import annotations

import collections
import datetime as dt
import json
import os
import tempfile
import threading
from datetime import timezone
from pathlib import Path
from typing import Any

from core.contracts import content_hash
from registries.law_registry import LawRegistry
from search.search_engine import SearchEngine

_MEM_CACHE_MAX = 15


class CacheManager:
    """Maintains in-memory + file cache and exposes cache metrics."""

    def __init__(self, cache_dir: str | Path, registry: LawRegistry) -> None:
        self._cache_dir = Path(cache_dir)
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._registry = registry
        self._mem_cache: collections.OrderedDict[str, dict[str, Any]] = collections.OrderedDict()
        self._mem_cache_lock = threading.Lock()
        self._law_locks: dict[str, threading.Lock] = {}
        self._locks_lock = threading.Lock()
        self._metrics_lock = threading.Lock()
        self._metrics = {"hits": 0, "misses": 0, "errors": 0, "writes": 0}

    def get_or_fetch(
        self,
        law_id: str,
        scraper: Any,
        search: SearchEngine,
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        if not force_refresh:
            entry = self._load_from_memory(law_id)
            if entry:
                self._record_metric("hits")
                return {**entry, "from_cache": True}

        law_lock = self._get_law_lock(law_id)
        with law_lock:
            if not force_refresh:
                memory_entry = self._load_from_memory(law_id)
                if memory_entry:
                    self._record_metric("hits")
                    return {**memory_entry, "from_cache": True}

                file_entry = self._load_from_file(law_id, fresh_only=True)
                if file_entry:
                    self._mem_put(law_id, file_entry)
                    self._record_metric("hits")
                    return {**file_entry, "from_cache": True}

            return self._fetch_and_cache(law_id, scraper, search)

    def _fetch_and_cache(self, law_id: str, scraper: Any, search: SearchEngine) -> dict[str, Any]:
        law_info = self._registry.get_law(law_id)
        if law_info is None:
            raise ValueError(f"Unknown law_id: {law_id}")

        try:
            fetched = scraper.fetch_law(law_id, law_info)
        except Exception as error:
            stale_entry = self.get_cached_entry(law_id, allow_stale=True)
            self._record_metric("errors")
            if stale_entry:
                return {
                    **stale_entry,
                    "from_cache": True,
                    "stale": True,
                    "error": str(error),
                }
            raise

        text = fetched.get("text", "")
        entry: dict[str, Any] = {
            "law_id": law_id,
            "title": fetched.get("title", law_info.get("title", law_id)),
            "url": fetched.get("url", law_info.get("url", "")),
            "text": text,
            "source": fetched.get("source", "scraped"),
            "adapter": fetched.get("adapter", "unknown"),
            "source_policy": fetched.get("source_policy", "restricted"),
            "content_hash": fetched.get("content_hash") or content_hash(text),
            "articles": search.parse_articles(text),
            "scraped_at": dt.datetime.now(timezone.utc).isoformat(),
            "char_count": len(text),
        }
        if fetched.get("amendment_date"):
            entry["amendment_date"] = fetched["amendment_date"]

        # Carried, not interpreted: the routing layer classifies the document and
        # extracts its real name, and the persistence gate in repository.py reads
        # both. This entry is rebuilt from a fixed key set, so without an explicit
        # pass-through the two fields would be dropped between fetch and storage.
        for seam_key in ("doc_kind", "real_title"):
            if seam_key in fetched:
                entry[seam_key] = fetched[seam_key]

        # A hand-curated registry entry outranks the "bylaws are never stored"
        # rule; a document conjured by `_dynamic_law` does not.
        entry["registry_pinned"] = not bool(law_info.get("dynamic"))

        self._write_atomic(self._cache_path(law_id), entry)
        self._mem_put(law_id, dict(entry))
        self._record_metric("misses")
        self._record_metric("writes")
        return {**entry, "from_cache": False}

    def get_cached_entry(self, law_id: str, allow_stale: bool = False) -> dict[str, Any] | None:
        memory_entry = self._mem_get(law_id)
        if memory_entry and (allow_stale or self._is_fresh(law_id, memory_entry)):
            return memory_entry

        file_entry = self._load_from_file(law_id, fresh_only=not allow_stale)
        return dict(file_entry) if file_entry else None

    def get_status(self, law_id: str) -> dict[str, Any]:
        entry = self.get_cached_entry(law_id, allow_stale=True)
        if not entry:
            return {"cached": False, "fresh": False, "char_count": 0, "source": None}

        return {
            "cached": True,
            "fresh": self._is_fresh(law_id, entry),
            "char_count": int(entry.get("char_count", 0)),
            "source": entry.get("source"),
            "cached_at": entry.get("scraped_at"),
        }

    def get_metrics(self) -> dict[str, Any]:
        with self._metrics_lock:
            metrics: dict[str, Any] = dict(self._metrics)
        metrics["backend"] = "file"
        metrics["mem_cache_entries"] = len(self._mem_cache)
        metrics["cached_laws"] = self.count_cached_laws()
        return metrics

    def count_cached_laws(self) -> int:
        return sum(1 for path in self._cache_dir.glob("*.json") if path.is_file())

    def _mem_get(self, law_id: str) -> dict[str, Any] | None:
        """Return a copy of the cached entry and mark it as recently used."""
        with self._mem_cache_lock:
            entry = self._mem_cache.get(law_id)
            if entry is None:
                return None
            self._mem_cache.move_to_end(law_id)
            return dict(entry)

    def _mem_put(self, law_id: str, entry: dict[str, Any]) -> None:
        """Insert entry into LRU mem cache; evict the oldest if over limit."""
        with self._mem_cache_lock:
            self._mem_cache[law_id] = entry
            self._mem_cache.move_to_end(law_id)
            if len(self._mem_cache) > _MEM_CACHE_MAX:
                self._mem_cache.popitem(last=False)

    def _get_law_lock(self, law_id: str) -> threading.Lock:
        with self._locks_lock:
            if law_id not in self._law_locks:
                self._law_locks[law_id] = threading.Lock()
            return self._law_locks[law_id]

    def _load_from_memory(self, law_id: str) -> dict[str, Any] | None:
        entry = self._mem_get(law_id)
        if not entry:
            return None
        return entry if self._is_fresh(law_id, entry) else None

    def _load_from_file(self, law_id: str, fresh_only: bool) -> dict[str, Any] | None:
        path = self._cache_path(law_id)
        if not path.exists():
            return None

        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

        if not isinstance(loaded, dict):
            return None
        data: dict[str, Any] = loaded

        if fresh_only and not self._is_fresh(law_id, data):
            return None
        return data

    def _is_fresh(self, law_id: str, entry: dict[str, Any]) -> bool:
        scraped_at = str(entry.get("scraped_at", "")).strip()
        if not scraped_at:
            return False

        try:
            parsed = dt.datetime.fromisoformat(scraped_at)
        except ValueError:
            return False

        ttl_days = self._registry.get_ttl_days(law_id)
        # Ensure both datetimes are tz-aware for comparison
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        age = dt.datetime.now(timezone.utc) - parsed
        return age.days < ttl_days

    def _cache_path(self, law_id: str) -> Path:
        safe_name = law_id.replace("/", "_").replace("\\", "_")
        return self._cache_dir / f"{safe_name}.json"

    def _write_atomic(self, target_path: Path, payload: dict[str, Any]) -> None:
        tmp_file = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=target_path.parent,
            suffix=".tmp",
            delete=False,
        )
        try:
            with tmp_file as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
            os.replace(tmp_file.name, target_path)
        finally:
            tmp_path = Path(tmp_file.name)
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)

    def _record_metric(self, metric_name: str) -> None:
        with self._metrics_lock:
            self._metrics[metric_name] = self._metrics.get(metric_name, 0) + 1
