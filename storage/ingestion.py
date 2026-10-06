"""Ingestion pipeline: fetch law texts into the Postgres repository.

Reuses the existing ZakonRadaAdapter (fetch) and SearchEngine (article
parsing). Text + articles are persisted as a redaction; embeddings are
generated only when a real embedder is configured (MVP-1+).
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

from search.embeddings import NullEmbedder
from registries.law_registry import LawRegistry
from storage.repository import PostgresRepository, persistence_verdict
from search.search_engine import SearchEngine

logger = logging.getLogger("ukraine-laws.ingestion")

# Gemini's embedding models cap input at ~2048 tokens; Ukrainian Cyrillic packs
# more tokens per character, so we truncate article bodies before embedding to
# stay safely under the limit (the leading text is the most discriminative).
EMBED_MAX_CHARS = 6000

# A real «стаття» never approaches this size; anything above is a parser
# artifact (e.g. the last article of a code swallowing the «Прикінцеві
# положення» tail) that wrecks FTS ranking and overflows the embedding window.
MAX_ARTICLE_CHARS = 12000
SPLIT_TARGET_CHARS = 3000

# On the metered Gemini free tier (~100 embeddings/day measured) the corpus
# fills over weeks — embed the laws lawyers query most first. Override with
# EMBED_PRIORITY_LAWS (comma-separated law_ids; empty string disables).
DEFAULT_PRIORITY_LAWS = "435-15,436-15,922-19,1178-2022-п,2755-17"


def _embed_priority_laws() -> list[str]:
    raw = os.getenv("EMBED_PRIORITY_LAWS", DEFAULT_PRIORITY_LAWS)
    return [law_id.strip() for law_id in raw.split(",") if law_id.strip()]


@dataclass
class IngestResult:
    law_id: str
    ok: bool
    created: bool = False
    version_id: int | None = None
    char_count: int = 0
    articles: int = 0
    embedded: int = 0
    error: str | None = None


@dataclass
class IngestSummary:
    total: int = 0
    ok: int = 0
    created: int = 0
    skipped: int = 0
    failed: int = 0
    results: list[IngestResult] = field(default_factory=list)


class Ingestor:
    """Drives fetch → parse → persist (→ embed) for laws."""

    def __init__(
        self,
        repo: PostgresRepository,
        adapter: Any,
        search: SearchEngine,
        registry: LawRegistry,
        embedder: Any | None = None,
    ) -> None:
        self._repo = repo
        self._adapter = adapter
        self._search = search
        self._registry = registry
        self._embedder = embedder or NullEmbedder()

    def ingest_law(self, law_id: str, law_info: dict[str, Any] | None = None) -> IngestResult:
        law_info = law_info or self._registry.get_law(law_id)
        if law_info is None:
            return IngestResult(law_id=law_id, ok=False, error="unknown law_id")

        try:
            payload = self._adapter.fetch_law(law_id, law_info)
        except Exception as error:  # network / source failure
            logger.warning("ingest fetch failed for %s: %s", law_id, error, exc_info=True)
            return IngestResult(law_id=law_id, ok=False, error=str(error))

        text = str(payload.get("text") or "")
        if not text:
            return IngestResult(law_id=law_id, ok=False, error="empty text")

        # The gate runs BEFORE the first write: a document that may not join the
        # working set must leave no laws row, no version and no articles behind.
        # That is how the 2456-17 stub got in — the write happened first and the
        # failed parse was noticed afterwards (ticket 15, defects 2a/2b).
        articles = self._search.parse_articles(text)
        store, reason, title = persistence_verdict(
            {
                "law_id": law_id,
                **payload,
                "text": text,
                "registry_pinned": not bool(law_info.get("dynamic")),
            },
            articles,
        )
        if not store:
            logger.info("not persisting %s: %s", law_id, reason)
            return IngestResult(law_id=law_id, ok=False, error=f"not persisted: {reason}")

        self._repo.upsert_law(
            law_id,
            {
                "title": title,
                "url": payload.get("url") or law_info.get("url"),
                "print_url": law_info.get("print_url"),
                "category": law_info.get("category"),
                "volatile": self._registry.is_volatile(law_id),
                "cache_ttl_days": law_info.get("cache_ttl_days", 30),
                "accepted_at": law_info.get("accepted_at"),
                "source": payload.get("adapter") or "zakon_rada",
            },
        )

        version_id, created = self._repo.upsert_version(law_id, payload)
        articles_count = 0
        embedded = 0
        if created:
            # No chunk_text fallback any more: a document whose text yields zero
            # «статті» is either a bylaw (not stored at all) or a truncated fetch,
            # and both are refused by the gate above before reaching here.
            articles = self._split_oversized(articles)
            article_ids = self._repo.upsert_articles(version_id, law_id, articles)
            articles_count = len(article_ids)
            embedded = self._maybe_embed(article_ids, list(articles.values()))

        logger.info(
            "ingested %s: created=%s articles=%d chars=%d",
            law_id,
            created,
            articles_count,
            len(text),
        )
        return IngestResult(
            law_id=law_id,
            ok=True,
            created=created,
            version_id=version_id,
            char_count=len(text),
            articles=articles_count,
            embedded=embedded,
        )

    def ingest_all(self, law_ids: list[str] | None = None) -> IngestSummary:
        if law_ids is None:
            law_ids = [law["id"] for law in self._registry.list_laws()]

        summary = IngestSummary(total=len(law_ids))
        for law_id in law_ids:
            result = self.ingest_law(law_id)
            summary.results.append(result)
            if not result.ok:
                summary.failed += 1
            else:
                summary.ok += 1
                if result.created:
                    summary.created += 1
                else:
                    summary.skipped += 1
        return summary

    def _maybe_embed(self, article_ids: list[int], bodies: list[str]) -> int:
        """Embed article bodies if a real embedder is configured (MVP-1+)."""
        if getattr(self._embedder, "dim", 0) <= 0 or not article_ids:
            return 0
        try:
            vectors = self._embedder.embed([body[:EMBED_MAX_CHARS] for body in bodies])
        except Exception as error:  # pragma: no cover - optional path
            logger.warning("embedding failed: %s", error)
            return 0
        rows = [
            (article_id, vector)
            for article_id, vector in zip(article_ids, vectors, strict=True)
            if vector
        ]
        if not rows:
            return 0
        # Persist embeddings (MVP-1 wires repo.upsert_embeddings; guarded here).
        upsert = getattr(self._repo, "upsert_embeddings", None)
        if upsert is None:
            return 0
        upsert(rows, self._embedder.model)
        return len(rows)

    def _split_oversized(self, articles: dict[str, str]) -> dict[str, str]:
        """Split parser-artifact giant bodies into chunked rows.

        The first chunk keeps the original article_num (so citations still
        resolve); the rest get ``{num}#2``, ``{num}#3``… keys, which cannot
        collide with real numbers («625-1» style).
        """
        out: dict[str, str] = {}
        for article_num, body in articles.items():
            if len(body) <= MAX_ARTICLE_CHARS:
                out[article_num] = body
                continue
            chunks = self._search.chunk_text(body, target_chars=SPLIT_TARGET_CHARS)
            if not chunks:
                out[article_num] = body
                continue
            for index, chunk_body in enumerate(chunks.values(), start=1):
                key = article_num if index == 1 else f"{article_num}#{index}"
                out[key] = chunk_body
        return out

    def backfill_split_articles(self, min_chars: int = MAX_ARTICLE_CHARS) -> int:
        """Re-chunk already-ingested oversized articles (no network, idempotent).

        Each giant row is atomically replaced by its chunks; the stale embedding
        (if any) is cascade-deleted and the chunks are picked up by the next
        embeddings backfill. Returns the number of giant rows replaced.
        """
        replaced = 0
        for (
            article_id,
            version_id,
            law_id,
            article_num,
            body,
            ordinal,
        ) in self._repo.oversized_articles(min_chars):
            chunks = self._split_oversized({article_num: body})
            if set(chunks.keys()) == {article_num}:
                continue
            self._repo.replace_article_with_chunks(article_id, version_id, law_id, ordinal, chunks)
            replaced += 1
            logger.info(
                "split %s ст.%s (%d chars) into %d chunks",
                law_id,
                article_num,
                len(body),
                len(chunks),
            )
        logger.info("backfill_split_articles complete: %d giant articles split", replaced)
        return replaced

    def backfill_articles(self) -> int:
        """Chunk ``full_text`` into fragments for current redactions with no articles.

        Operates purely on already-ingested DB rows (no network), so laws whose
        structure ``parse_articles`` could not detect (КМУ постанови with «пункти»)
        become FTS/vector-searchable. Idempotent: only touches 0-article versions.
        Returns the number of fragment rows created.
        """
        pending = self._repo.versions_missing_articles()
        created = 0
        for version_id, law_id, full_text in pending:
            chunks = self._search.chunk_text(full_text)
            if not chunks:
                continue
            ids = self._repo.upsert_articles(version_id, law_id, chunks)
            created += len(ids)
            logger.info("backfilled %d fragments for %s", len(ids), law_id)
        logger.info(
            "backfill_articles complete: %d fragments across %d laws", created, len(pending)
        )
        return created

    def backfill_embeddings(
        self,
        batch: int = 32,
        max_articles: int | None = None,
        max_failures: int = 6,
        retry_pause: float = 30.0,
    ) -> int:
        """Embed current-redaction articles that have no vector yet (resumable).

        Each call only processes the remaining gap, so embedding generation
        survives free-tier rate limits and CI timeouts across multiple runs.
        Crucially it does NOT give up on the first rate-limited batch: it waits
        out transient 429s (``retry_pause`` between tries, re-fetching the same
        gap) and only stops after ``max_failures`` consecutive failures — so one
        run drains as much of the backlog as the quota window allows. Returns the
        number of embeddings written this call.
        """
        if getattr(self._embedder, "dim", 0) <= 0:
            logger.warning("backfill_embeddings: NullEmbedder configured; nothing to do")
            return 0
        upsert = getattr(self._repo, "upsert_embeddings", None)
        get_pending = getattr(self._repo, "articles_missing_embeddings", None)
        if upsert is None or get_pending is None:
            return 0

        priority_laws = _embed_priority_laws()
        total = 0
        failures = 0
        while True:
            limit = batch
            if max_articles is not None:
                limit = min(batch, max_articles - total)
                if limit <= 0:
                    break
            try:
                pending = get_pending(limit=limit, priority_laws=priority_laws)
            except TypeError:  # repo fake/older impl without priority support
                pending = get_pending(limit=limit)
            if not pending:
                break
            article_ids = [article_id for article_id, _ in pending]
            bodies = [body[:EMBED_MAX_CHARS] for _, body in pending]
            try:
                vectors = self._embedder.embed(bodies)
            except Exception as error:  # network / rate-limit path
                failures += 1
                logger.warning(
                    "backfill_embeddings batch failed (%d/%d), pausing %.0fs: %s",
                    failures,
                    max_failures,
                    retry_pause,
                    error,
                )
                if failures >= max_failures:
                    logger.warning(
                        "backfill_embeddings: giving up this run after %d failures; "
                        "%d written, remainder resumes next run",
                        failures,
                        total,
                    )
                    break
                time.sleep(retry_pause)
                continue
            rows = [
                (article_id, vec)
                for article_id, vec in zip(article_ids, vectors, strict=True)
                if vec
            ]
            if not rows:
                failures += 1
                if failures >= max_failures:
                    break
                time.sleep(retry_pause)
                continue
            upsert(rows, self._embedder.model)
            total += len(rows)
            failures = 0
            logger.info("embedded %d articles (running total %d)", len(rows), total)
        logger.info("backfill_embeddings complete: %d embeddings written", total)
        return total
