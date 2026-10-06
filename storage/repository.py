"""Postgres-backed storage for laws, redactions and articles.

``PostgresRepository`` persists law texts into Supabase/Postgres (with pgvector
+ Ukrainian full-text search). ``RepositoryCacheAdapter`` presents the exact
same surface as ``CacheManager`` (``get_or_fetch`` / ``get_cached_entry`` /
``get_status`` / ``get_metrics``) so the MCP tools in server.py stay untouched,
and it transparently falls through to the file-cache path on any DB miss/error.

Enabled behind the ``USE_PG_BACKEND`` feature flag (default off).
"""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path
from typing import Any, Callable, Protocol, TypeVar

from core.contracts import content_hash
from registries.law_registry import is_placeholder_title
from search.search_engine import SearchEngine

logger = logging.getLogger("ukraine-laws.repository")

T = TypeVar("T")

_SCHEMA_PATH = Path(__file__).resolve().parents[1] / "db" / "schema.sql"


def _parse_date(value: Any) -> dt.date | None:
    """Parse an ISO date/datetime string into a date, tolerating noise."""
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return dt.date.fromisoformat(text[:10])
    except ValueError:
        return None


# --- persistence gate --------------------------------------------------------
# The working set holds only what it may safely answer from: a real Закон or
# Кодекс, under its own name, parsed into articles. Everything else is answered
# live and forgotten (CONTEXT.md — «Робочий набір», «Обрубок»).
#
# Two fields are produced by the routing layer and only READ here:
#   doc_kind   — "law" | "code" | "constitution" | "bylaw" | "unknown"
#   real_title — the document's own name, "" when it could not be extracted
# While routing is still landing these keys may be absent. An ABSENT doc_kind
# means "unknown" and never blocks (blocking on it would stop today's prod from
# caching anything); an absent real_title falls back to the entry title, which
# is rejected when it is the registry placeholder.

NON_PERSISTABLE_DOC_KINDS = frozenset({"bylaw"})


def is_registry_pinned(entry: dict[str, Any]) -> bool:
    """True for a document the registry curates by hand.

    The "bylaws are read live, never stored" rule is about the 285 960 acts in
    Rada's catalogue that nobody has vouched for — not about the handful someone
    put in ``cache/laws.json`` on purpose. Eleven of the twenty-seven entries
    there are bylaws, and they are the product's subject matter: publiс
    procurement specifics (1178-2022-п, 93 articles), pharmacovigilance orders,
    prescription rules. Refusing to store those would quietly narrow the product
    to what the rule was never aimed at.
    """
    return bool(entry.get("registry_pinned"))


def resolve_document_title(entry: dict[str, Any]) -> str:
    """The document's real name, or "" when only a placeholder is available.

    ``real_title`` wins when the routing layer supplied the key — including when
    it supplied it empty, which is that layer saying "this is a stub, not a
    document" and must not be papered over with the registry title.
    """
    law_id = str(entry.get("law_id") or "")
    source = entry.get("real_title") if "real_title" in entry else entry.get("title")
    candidate = str(source or "").strip()
    return "" if is_placeholder_title(candidate, law_id) else candidate


def persistence_verdict(entry: dict[str, Any], articles: dict[str, str]) -> tuple[bool, str, str]:
    """Decide whether a fetched document may be written to the working set.

    Returns ``(store, reason, title)``. ``reason`` is empty when ``store`` is
    True and otherwise names the rule that refused. ``title`` is the real title
    to persist — never the ``_dynamic_law`` placeholder.
    """
    if not str(entry.get("text") or "").strip():
        return False, "empty text", ""

    doc_kind = str(entry.get("doc_kind") or "unknown").strip().lower()
    if doc_kind in NON_PERSISTABLE_DOC_KINDS and not is_registry_pinned(entry):
        return False, f"doc_kind={doc_kind}: read live, never stored", ""

    title = resolve_document_title(entry)
    if not title:
        return False, "no real title (placeholder is a hypothesis, not a document)", ""

    if not articles:
        return False, "zero articles parsed from non-empty text (truncated fetch)", title

    return True, "", title


def _freshness(retrieved_at: Any, ttl_days: Any) -> tuple[bool, int | None]:
    """Compare a row's age against its TTL. Missing timestamp counts as stale."""
    if retrieved_at is None:
        return False, None
    stamp = retrieved_at
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=dt.timezone.utc)
    age_days = (dt.datetime.now(dt.timezone.utc) - stamp).days
    return age_days < int(ttl_days or 30), age_days


class LawRepository(Protocol):
    """Storage contract (mirrors the SourceAdapter protocol style)."""

    def get_current(self, law_id: str, allow_stale: bool = True) -> dict[str, Any] | None: ...

    def get_status(self, law_id: str) -> dict[str, Any]: ...

    def upsert_law(self, law_id: str, meta: dict[str, Any]) -> None: ...

    def upsert_version(self, law_id: str, payload: dict[str, Any]) -> tuple[int, bool]: ...

    def upsert_articles(
        self, version_id: int, law_id: str, articles: dict[str, str]
    ) -> list[int]: ...

    def fts_search(
        self, query: str, law_id: str | None, limit: int, match_any: bool = False
    ) -> list[dict[str, Any]]: ...


class PostgresRepository:
    """psycopg3 + connection-pool implementation backed by Postgres/pgvector."""

    def __init__(
        self, dsn: str, embedder: Any | None = None, min_size: int = 1, max_size: int = 4
    ) -> None:
        from psycopg_pool import ConnectionPool

        self._embedder = embedder
        # prepare_threshold=None disables server-side prepared statements, which
        # are incompatible with Supabase Transaction pooler (PgBouncer, port 6543).
        self._pool = ConnectionPool(
            dsn,
            min_size=min_size,
            max_size=max_size,
            open=True,
            kwargs={"prepare_threshold": None},
        )
        self._bytes_fetched = 0  # Track egress for monitoring

    def ensure_schema(self) -> None:
        """Apply db/schema.sql (idempotent). Used by ingestion and tests."""
        sql = _SCHEMA_PATH.read_text(encoding="utf-8")
        with self._pool.connection() as conn:
            conn.execute(sql)  # type: ignore[arg-type]

    # --- writes -----------------------------------------------------------
    def upsert_law(self, law_id: str, meta: dict[str, Any]) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO laws (law_id, title, url, print_url, category,
                                  volatile, cache_ttl_days, accepted_at, source,
                                  updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (law_id) DO UPDATE SET
                    title = EXCLUDED.title,
                    url = EXCLUDED.url,
                    print_url = EXCLUDED.print_url,
                    category = EXCLUDED.category,
                    volatile = EXCLUDED.volatile,
                    cache_ttl_days = EXCLUDED.cache_ttl_days,
                    accepted_at = COALESCE(EXCLUDED.accepted_at, laws.accepted_at),
                    updated_at = now()
                """,
                (
                    law_id,
                    str(meta.get("title") or law_id),
                    str(meta.get("url") or ""),
                    meta.get("print_url"),
                    meta.get("category"),
                    bool(meta.get("volatile", False)),
                    int(meta.get("cache_ttl_days", 30)),
                    _parse_date(meta.get("accepted_at")),
                    str(meta.get("source") or "zakon_rada"),
                ),
            )

    def upsert_version(self, law_id: str, payload: dict[str, Any]) -> tuple[int, bool]:
        """Insert a redaction, deduping by content_hash and tracking status.

        Returns (version_id, created). When the text is new, the previous
        'current' redaction is marked 'superseded' (effective_to = new
        amendment date) and the new row becomes 'current'.
        """
        text = str(payload.get("text") or "")
        digest = str(payload.get("content_hash") or content_hash(text))
        amendment = _parse_date(payload.get("amendment_date"))

        with self._pool.connection() as conn:
            with conn.transaction():
                existing = conn.execute(
                    "SELECT id FROM law_versions WHERE law_id = %s AND content_hash = %s",
                    (law_id, digest),
                ).fetchone()
                if existing:
                    return int(existing[0]), False

                conn.execute(
                    """
                    UPDATE law_versions
                    SET status = 'superseded',
                        effective_to = COALESCE(%s, CURRENT_DATE)
                    WHERE law_id = %s AND status = 'current'
                    """,
                    (amendment, law_id),
                )
                row = conn.execute(
                    """
                    INSERT INTO law_versions (law_id, amendment_date, effective_from,
                        status, content_hash, full_text, char_count, source,
                        source_policy, adapter)
                    VALUES (%s, %s, %s, 'current', %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        law_id,
                        amendment,
                        amendment,
                        digest,
                        text,
                        len(text),
                        payload.get("source"),
                        payload.get("source_policy"),
                        str(payload.get("adapter") or "zakon_rada"),
                    ),
                ).fetchone()
                assert row is not None
                return int(row[0]), True

    def upsert_articles(self, version_id: int, law_id: str, articles: dict[str, str]) -> list[int]:
        ids: list[int] = []
        with self._pool.connection() as conn:
            for ordinal, (article_num, body) in enumerate(articles.items()):
                row = conn.execute(
                    """
                    INSERT INTO articles (version_id, law_id, article_num, ordinal, body)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (version_id, article_num) DO UPDATE SET
                        body = EXCLUDED.body, ordinal = EXCLUDED.ordinal
                    RETURNING id
                    """,
                    (version_id, law_id, article_num, ordinal, body),
                ).fetchone()
                if row is not None:
                    ids.append(int(row[0]))
        return ids

    def store_cache_entry(
        self, law_id: str, entry: dict[str, Any], search: SearchEngine
    ) -> tuple[int, bool]:
        """Persist a CacheManager-shaped entry (used by lazy write-through).

        Guarded by :func:`persistence_verdict`. A refusal is NOT an error: the
        caller keeps the fetched result and answers from it, the document simply
        does not join the working set. Returns ``(0, False)`` when refused —
        no ``laws`` row, no ``law_versions`` row, nothing half-written.
        """
        articles = entry.get("articles") or search.parse_articles(str(entry.get("text") or ""))
        store, reason, title = persistence_verdict({"law_id": law_id, **entry}, articles)
        if not store:
            logger.info("not persisting %s: %s", law_id, reason)
            return 0, False

        self.upsert_law(
            law_id,
            {
                "title": title,
                "url": entry.get("url"),
                "cache_ttl_days": entry.get("cache_ttl_days", 30),
                "source": entry.get("adapter", "zakon_rada"),
            },
        )
        version_id, created = self.upsert_version(law_id, entry)
        if created:
            self.upsert_articles(version_id, law_id, articles)
        return version_id, created

    # --- reads ------------------------------------------------------------
    def get_current(self, law_id: str, allow_stale: bool = True) -> dict[str, Any] | None:
        """Return the current redaction shaped like a CacheManager entry.

        FRESHNESS (ticket 15, cause 1): the row's age is compared against the
        law's ``cache_ttl_days`` and the verdict travels with the entry —
        ``fresh``/``age_days``/``ttl_days``, plus ``stale=True`` once the TTL is
        spent. Reading used to skip this check entirely, which is how 35 laws sat
        frozen on 2026-06-06 for three months while being served as current.
        With ``allow_stale=False`` an expired row is not returned at all.

        OPTIMIZATION (ticket 14, egress blowout): full_text is NOT selected here. It
        duplicates content already held in articles, and fetching both doubled the
        traffic of every call. Articles are the source of truth.
        FALLBACK: when the parse produced no articles but char_count says the version
        has text, a SECOND query fetches full_text. Keeping it separate is the point —
        the rare broken version pays for itself, the common case does not.
        """
        with self._pool.connection() as conn:
            version = conn.execute(
                """
                SELECT v.id, v.content_hash, v.amendment_date,
                       v.source, v.source_policy, v.adapter, v.char_count,
                       v.retrieved_at, l.title, l.url, l.cache_ttl_days
                FROM law_versions v
                JOIN laws l ON l.law_id = v.law_id
                WHERE v.law_id = %s AND v.status = 'current'
                """,
                (law_id,),
            ).fetchone()
            if version is None:
                return None

            (
                vid,
                chash,
                amendment,
                source,
                source_policy,
                adapter,
                char_count,
                retrieved_at,
                title,
                url,
                ttl_days,
            ) = version

            fresh, age_days = _freshness(retrieved_at, ttl_days)
            if not fresh and not allow_stale:
                return None

            article_rows = conn.execute(
                "SELECT article_num, body FROM articles WHERE version_id = %s " "ORDER BY ordinal",
                (vid,),
            ).fetchall()

            # Fallback is a SEPARATE query, issued only when the parse actually
            # failed. Selecting full_text up front would re-fetch what articles
            # already hold and undo the point of this method — that is the
            # regression ticket 14 exists to remove.
            fallback_text: str | None = None
            if not article_rows and char_count:
                row = conn.execute(
                    "SELECT full_text FROM law_versions WHERE id = %s",
                    (vid,),
                ).fetchone()
                fallback_text = str(row[0]) if row and row[0] else None

        articles = {str(num): str(body) for num, body in article_rows}

        # Text source: articles if available, fallback to full_text if parse failed
        if articles:
            # Use articles as source of truth (reconstructed, but preserves structure)
            text_parts = [str(body) for _, body in article_rows]
            text = "\n".join(text_parts)
            egress_source = "articles"
        elif fallback_text:
            # Fallback: parse failed, return full_text (e.g., 2456-17 with 0 articles)
            text = fallback_text
            egress_source = "full_text_fallback"
        else:
            text = ""
            egress_source = "empty"

        entry: dict[str, Any] = {
            "law_id": law_id,
            "title": str(title or law_id),
            "url": str(url or ""),
            "text": text,
            "source": source,
            "adapter": adapter or "zakon_rada",
            "source_policy": source_policy or "html_print",
            "content_hash": chash,
            "articles": articles,
            "scraped_at": retrieved_at.isoformat() if retrieved_at else None,
            "char_count": int(char_count or 0),
            "fresh": fresh,
            "age_days": age_days,
            "ttl_days": int(ttl_days or 30),
        }
        if not fresh:
            entry["stale"] = True
        if amendment is not None:
            entry["amendment_date"] = amendment.isoformat()

        # Track egress: only count text that was actually fetched from Postgres
        # (not reconstructed, which would double-count articles)
        if egress_source == "full_text_fallback":
            bytes_fetched = len(text.encode("utf-8"))
        else:
            # articles case: count only the bodies (not the reconstructed text)
            bytes_fetched = sum(len(v.encode("utf-8")) for v in articles.values())
        self._bytes_fetched += bytes_fetched

        return entry

    def get_articles_by_number(
        self, law_id: str, article_nums: list[str] | None = None
    ) -> dict[str, Any] | None:
        """Fetch specific articles without loading entire law (< 100 bytes each vs 8 МБ).

        Used by get_article, get_multiple_articles. With article_nums, selects only
        those; without, returns all (but still avoids full_text duplication).
        """
        with self._pool.connection() as conn:
            version = conn.execute(
                """
                SELECT v.id, v.content_hash, v.amendment_date,
                       v.source, v.source_policy, v.adapter, v.char_count,
                       v.retrieved_at, l.title, l.url
                FROM law_versions v
                JOIN laws l ON l.law_id = v.law_id
                WHERE v.law_id = %s AND v.status = 'current'
                """,
                (law_id,),
            ).fetchone()
            if version is None:
                return None

            (
                vid,
                chash,
                amendment,
                source,
                source_policy,
                adapter,
                char_count,
                retrieved_at,
                title,
                url,
            ) = version

            # Fetch requested articles only
            if article_nums:
                article_rows = conn.execute(
                    "SELECT article_num, body FROM articles "
                    "WHERE version_id = %s AND article_num = ANY(%s) "
                    "ORDER BY ordinal",
                    (vid, article_nums),
                ).fetchall()
            else:
                article_rows = conn.execute(
                    "SELECT article_num, body FROM articles WHERE version_id = %s "
                    "ORDER BY ordinal",
                    (vid,),
                ).fetchall()

        articles = {str(num): str(body) for num, body in article_rows}
        text_parts = [str(body) for _, body in article_rows]
        text = "\n".join(text_parts)

        entry: dict[str, Any] = {
            "law_id": law_id,
            "title": str(title or law_id),
            "url": str(url or ""),
            "text": text,
            "source": source,
            "adapter": adapter or "zakon_rada",
            "source_policy": source_policy or "html_print",
            "content_hash": chash,
            "articles": articles,
            "scraped_at": retrieved_at.isoformat() if retrieved_at else None,
            "char_count": int(char_count or 0),
        }
        if amendment is not None:
            entry["amendment_date"] = amendment.isoformat()

        # Track egress: only requested articles
        bytes_fetched = sum(len(v.encode("utf-8")) for v in articles.values())
        self._bytes_fetched += bytes_fetched

        return entry

    def get_current_limited(self, law_id: str, limit_chars: int) -> dict[str, Any] | None:
        """Fetch current redaction with SQL-level LIMIT on articles (for query_law optimization).

        Fetches articles sequentially until reaching limit_chars. Articles are fetched
        from the start, so this is suitable for paginated results.
        """
        with self._pool.connection() as conn:
            version = conn.execute(
                """
                SELECT v.id, v.content_hash, v.amendment_date,
                       v.source, v.source_policy, v.adapter, v.char_count,
                       v.retrieved_at, l.title, l.url
                FROM law_versions v
                JOIN laws l ON l.law_id = v.law_id
                WHERE v.law_id = %s AND v.status = 'current'
                """,
                (law_id,),
            ).fetchone()
            if version is None:
                return None

            (
                vid,
                chash,
                amendment,
                source,
                source_policy,
                adapter,
                char_count,
                retrieved_at,
                title,
                url,
            ) = version

            # Fetch articles in order, accumulating until we hit the char limit
            # This avoids fetching articles we won't use (e.g., huge last articles)
            article_rows = conn.execute(
                """
                SELECT article_num, body FROM articles
                WHERE version_id = %s
                ORDER BY ordinal
                """,
                (vid,),
            ).fetchall()

        # Accumulate text until we hit the limit
        accumulated_text = ""
        accumulated_articles = {}
        total_chars = 0

        for num, body in article_rows:
            body_str = str(body)
            body_chars = len(body_str)
            if total_chars + body_chars > limit_chars and accumulated_text:
                # We've hit the limit; stop accumulating
                break
            accumulated_text += body_str + "\n"
            accumulated_articles[str(num)] = body_str
            total_chars += body_chars + 1  # +1 for newline

        if not accumulated_text and article_rows:
            # Even the first article exceeds limit; include it anyway
            accumulated_text = str(article_rows[0][1])
            accumulated_articles[str(article_rows[0][0])] = str(article_rows[0][1])

        entry: dict[str, Any] = {
            "law_id": law_id,
            "title": str(title or law_id),
            "url": str(url or ""),
            "text": accumulated_text.rstrip("\n"),
            "source": source,
            "adapter": adapter or "zakon_rada",
            "source_policy": source_policy or "html_print",
            "content_hash": chash,
            "articles": accumulated_articles,
            "scraped_at": retrieved_at.isoformat() if retrieved_at else None,
            "char_count": int(char_count or 0),
        }
        if amendment is not None:
            entry["amendment_date"] = amendment.isoformat()

        # Track egress: only accumulated articles
        bytes_fetched = sum(len(v.encode("utf-8")) for v in accumulated_articles.values())
        self._bytes_fetched += bytes_fetched

        return entry

    def get_status(self, law_id: str) -> dict[str, Any]:
        with self._pool.connection() as conn:
            row = conn.execute(
                """
                SELECT v.char_count, v.source, v.retrieved_at, l.cache_ttl_days
                FROM law_versions v
                JOIN laws l ON l.law_id = v.law_id
                WHERE v.law_id = %s AND v.status = 'current'
                """,
                (law_id,),
            ).fetchone()
        if row is None:
            return {"cached": False, "fresh": False, "char_count": 0, "source": None}

        char_count, source, retrieved_at, ttl_days = row
        fresh, age_days = _freshness(retrieved_at, ttl_days)
        return {
            "cached": True,
            "fresh": fresh,
            "age_days": age_days,
            "char_count": int(char_count or 0),
            "source": source,
            "cached_at": retrieved_at.isoformat() if retrieved_at else None,
        }

    def fts_search(
        self, query: str, law_id: str | None, limit: int, match_any: bool = False
    ) -> list[dict[str, Any]]:
        """Ukrainian full-text search over current-redaction articles.

        ``plainto_tsquery`` ANDs every lexeme, so one extra query word kills
        recall (e.g. «інфляційні втрати ... прострочення» misses ЦКУ ст.625,
        which has no «втрати»). ``match_any=True`` relaxes the same query to
        OR-semantics — used as a recall fallback when the AND pass is empty.
        """
        tsquery = "plainto_tsquery('ukrainian', %s)"
        if match_any:
            tsquery = "replace(plainto_tsquery('ukrainian', %s)::text, ' & ', ' | ')::tsquery"
        # Placeholders are bound positionally in the order they appear in the
        # SQL: rank query, optional law_id filter, match query, limit.
        law_filter = "AND a.law_id = %s" if law_id else ""
        params: list[Any] = [query]
        if law_id:
            params.append(law_id)
        params.extend([query, limit])
        # Normalization flag 1 divides the rank by 1+log(doc length) so a giant
        # body cannot dominate purely by repeating query lexemes many times.
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"""
                SELECT a.law_id, l.title, l.url, a.article_num, a.body,
                       ts_rank_cd(a.tsv, {tsquery}, 1) AS rank
                FROM articles a
                JOIN law_versions v ON v.id = a.version_id
                JOIN laws l ON l.law_id = a.law_id
                WHERE v.status = 'current'
                  {law_filter}
                  AND a.tsv @@ {tsquery}
                ORDER BY rank DESC
                LIMIT %s
                """,
                params,
            ).fetchall()
        return [
            {
                "law_id": law_id_,
                "title": title,
                "url": url,
                "article_num": article_num,
                "snippet": body,
                "score": float(rank),
            }
            for law_id_, title, url, article_num, body, rank in rows
        ]

    @staticmethod
    def _to_vector_literal(vector: list[float]) -> str:
        return "[" + ",".join(str(float(value)) for value in vector) + "]"

    def upsert_embeddings(self, rows: list[tuple[int, list[float]]], model: str) -> None:
        with self._pool.connection() as conn:
            for article_id, vector in rows:
                if not vector:
                    continue
                conn.execute(
                    """
                    INSERT INTO article_embeddings (article_id, embedding, model)
                    VALUES (%s, %s::vector, %s)
                    ON CONFLICT (article_id) DO UPDATE SET
                        embedding = EXCLUDED.embedding,
                        model = EXCLUDED.model,
                        created_at = now()
                    """,
                    (article_id, self._to_vector_literal(vector), model),
                )

    def articles_missing_embeddings(
        self,
        limit: int = 200,
        law_id: str | None = None,
        priority_laws: list[str] | None = None,
    ) -> list[tuple[int, str]]:
        """Current-redaction articles that have no embedding yet (resumable backfill).

        ``priority_laws`` jump the queue: on a metered free tier the corpus fills
        over weeks, so the laws lawyers query most should gain the vector channel
        first instead of waiting behind alphabetical law_id order.
        """
        law_filter = "AND a.law_id = %s" if law_id else ""
        order_by = "a.law_id, a.ordinal"
        params: list[Any] = []
        if law_id:
            params.append(law_id)
        if priority_laws:
            order_by = f"(a.law_id = ANY(%s)) DESC, {order_by}"
            params.append(list(priority_laws))
        params.append(limit)
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"""
                SELECT a.id, a.body
                FROM articles a
                JOIN law_versions v ON v.id = a.version_id
                LEFT JOIN article_embeddings e ON e.article_id = a.id
                WHERE v.status = 'current'
                  AND e.article_id IS NULL
                  AND btrim(a.body) <> ''
                  {law_filter}
                ORDER BY {order_by}
                LIMIT %s
                """,
                params,
            ).fetchall()
        return [(int(article_id), str(body or "")) for article_id, body in rows]

    def oversized_articles(
        self, min_chars: int = 12000
    ) -> list[tuple[int, int, str, str, str, int]]:
        """Current-redaction articles whose body exceeds ``min_chars``.

        These are parser artifacts — e.g. the last «стаття» of a code swallowing
        the whole «Прикінцеві положення» tail (ЦКУ ст.1308 → 41k chars) — and
        they wreck FTS ranking. Returned for chunked re-insertion.
        """
        with self._pool.connection() as conn:
            rows = conn.execute(
                """
                SELECT a.id, a.version_id, a.law_id, a.article_num, a.body, a.ordinal
                FROM articles a
                JOIN law_versions v ON v.id = a.version_id
                WHERE v.status = 'current' AND length(a.body) > %s
                ORDER BY a.law_id, a.ordinal
                """,
                (min_chars,),
            ).fetchall()
        return [
            (int(aid), int(vid), str(law_id), str(num), str(body or ""), int(ordinal))
            for aid, vid, law_id, num, body, ordinal in rows
        ]

    def replace_article_with_chunks(
        self,
        article_id: int,
        version_id: int,
        law_id: str,
        ordinal: int,
        chunks: dict[str, str],
    ) -> list[int]:
        """Atomically replace one oversized article row with its chunk rows.

        The old row's embedding (if any) is removed via ON DELETE CASCADE; the
        new chunk rows are picked up by the resumable embeddings backfill.
        """
        ids: list[int] = []
        with self._pool.connection() as conn:
            with conn.transaction():
                conn.execute("DELETE FROM articles WHERE id = %s", (article_id,))
                for article_num, body in chunks.items():
                    row = conn.execute(
                        """
                        INSERT INTO articles (version_id, law_id, article_num, ordinal, body)
                        VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT (version_id, article_num) DO UPDATE SET
                            body = EXCLUDED.body, ordinal = EXCLUDED.ordinal
                        RETURNING id
                        """,
                        (version_id, law_id, article_num, ordinal, body),
                    ).fetchone()
                    if row is not None:
                        ids.append(int(row[0]))
        return ids

    def versions_missing_articles(self) -> list[tuple[int, str, str]]:
        """Current redactions whose text was stored but parsed into zero articles."""
        with self._pool.connection() as conn:
            rows = conn.execute("""
                SELECT v.id, v.law_id, v.full_text
                FROM law_versions v
                WHERE v.status = 'current'
                  AND NOT EXISTS (SELECT 1 FROM articles a WHERE a.version_id = v.id)
                ORDER BY v.law_id
                """).fetchall()
        return [(int(vid), str(law_id), str(full_text or "")) for vid, law_id, full_text in rows]

    def vector_search(
        self, qvec: list[float], law_id: str | None, limit: int
    ) -> list[dict[str, Any]]:
        """Semantic search over current-redaction article embeddings (cosine)."""
        vec = self._to_vector_literal(qvec)
        law_filter = "AND a.law_id = %s" if law_id else ""
        params: list[Any] = [vec]
        if law_id:
            params.append(law_id)
        params.extend([vec, limit])
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"""
                SELECT a.law_id, l.title, l.url, a.article_num, a.body,
                       1 - (e.embedding <=> %s::vector) AS score
                FROM article_embeddings e
                JOIN articles a ON a.id = e.article_id
                JOIN law_versions v ON v.id = a.version_id
                JOIN laws l ON l.law_id = a.law_id
                WHERE v.status = 'current'
                  {law_filter}
                ORDER BY e.embedding <=> %s::vector ASC
                LIMIT %s
                """,
                params,
            ).fetchall()
        return [
            {
                "law_id": law_id_,
                "title": title,
                "url": url,
                "article_num": article_num,
                "snippet": body,
                "score": float(score),
            }
            for law_id_, title, url, article_num, body, score in rows
        ]

    def stats(self) -> dict[str, Any]:
        with self._pool.connection() as conn:
            laws = conn.execute("SELECT count(*) FROM laws").fetchone()
            current = conn.execute(
                "SELECT count(*) FROM law_versions WHERE status = 'current'"
            ).fetchone()
            arts = conn.execute("SELECT count(*) FROM articles").fetchone()
            embeddings = conn.execute("SELECT count(*) FROM article_embeddings").fetchone()
            pending = conn.execute("""
                SELECT count(*)
                FROM articles a
                JOIN law_versions v ON v.id = a.version_id
                LEFT JOIN article_embeddings e ON e.article_id = a.id
                WHERE v.status = 'current' AND e.article_id IS NULL
                """).fetchone()
        return {
            "laws": int(laws[0]) if laws else 0,
            "current_versions": int(current[0]) if current else 0,
            "articles": int(arts[0]) if arts else 0,
            "embeddings": int(embeddings[0]) if embeddings else 0,
            "pending_embeddings": int(pending[0]) if pending else 0,
        }

    def get_current_with_size(self, law_id: str) -> tuple[dict[str, Any] | None, int]:
        """Return current redaction + the byte size of the SELECT result.

        Used for egress monitoring — returns (entry, bytes_fetched) where bytes_fetched
        is the approximate size of data fetched from Postgres (for tracking quota).
        """
        entry = self.get_current(law_id)
        if entry is None:
            return None, 0
        # Approximate byte size of text + articles content
        text_bytes = len(entry.get("text", "").encode("utf-8"))
        articles_bytes = sum(
            len(k.encode("utf-8")) + len(v.encode("utf-8"))
            for k, v in entry.get("articles", {}).items()
        )
        return entry, text_bytes + articles_bytes

    def get_and_reset_bytes_fetched(self) -> int:
        """Return bytes fetched since last call, and reset counter (for egress monitoring)."""
        current = self._bytes_fetched
        self._bytes_fetched = 0
        return current

    def close(self) -> None:
        self._pool.close()


class RepositoryCacheAdapter:
    """Drop-in replacement for CacheManager backed by a LawRepository.

    Reads hit the repository first; on a miss or any DB error it falls through
    to the wrapped CacheManager (network fetch + file cache), then writes the
    fetched text back into the repository (best-effort) so future reads are
    served from Postgres.
    """

    def __init__(self, repo: PostgresRepository, fallback_cache: Any) -> None:
        self._repo = repo
        self._cache = fallback_cache

    def get_articles_by_number(
        self, law_id: str, article_nums: list[str] | None = None
    ) -> dict[str, Any] | None:
        """Optimized fetch of specific articles without loading entire law."""
        return self._repo.get_articles_by_number(law_id, article_nums)

    def get_current_limited(self, law_id: str, limit_chars: int) -> dict[str, Any] | None:
        """Optimized fetch of current redaction with SQL-level article LIMIT."""
        return self._repo.get_current_limited(law_id, limit_chars)

    def get_or_fetch(
        self, law_id: str, scraper: Any, search: SearchEngine, force_refresh: bool = False
    ) -> dict[str, Any]:
        """Serve from Postgres while fresh; once the TTL is spent, go refetch.

        A stale row is not thrown away — when the live source cannot be reached
        it is still better than nothing — but it is handed back marked
        ``stale=True`` rather than passed off as current (ticket 15, cause 1).
        """
        stale_entry: dict[str, Any] | None = None
        if not force_refresh:
            entry = self._safe(lambda: self._repo.get_current(law_id))
            if entry and not entry.get("stale"):
                return {**entry, "from_cache": True}
            stale_entry = entry

        try:
            result: dict[str, Any] = self._cache.get_or_fetch(
                law_id, scraper, search, force_refresh
            )
        except Exception as error:
            if stale_entry is None:
                raise
            logger.warning("refresh of stale %s failed, serving stale copy: %s", law_id, error)
            return {**stale_entry, "from_cache": True, "stale": True, "error": str(error)}

        if not result.get("text"):
            if stale_entry is not None:
                return {**stale_entry, "from_cache": True, "stale": True}
            return result

        self._safe(lambda: self._repo.store_cache_entry(law_id, result, search))
        return result

    def get_cached_entry(self, law_id: str, allow_stale: bool = False) -> dict[str, Any] | None:
        entry = self._safe(lambda: self._repo.get_current(law_id, allow_stale=allow_stale))
        if entry:
            return entry
        fallback: dict[str, Any] | None = self._cache.get_cached_entry(
            law_id, allow_stale=allow_stale
        )
        return fallback

    def get_status(self, law_id: str) -> dict[str, Any]:
        status = self._safe(lambda: self._repo.get_status(law_id))
        if status and status.get("cached"):
            return status
        fallback: dict[str, Any] = self._cache.get_status(law_id)
        return fallback

    def get_metrics(self) -> dict[str, Any]:
        metrics: dict[str, Any] = self._cache.get_metrics()
        metrics["backend"] = "postgres"
        pg_stats = self._safe(lambda: self._repo.stats())
        if pg_stats:
            metrics["pg"] = pg_stats
        return metrics

    def get_and_reset_bytes_fetched(self) -> int:
        """Get bytes fetched from Postgres since last call and reset counter."""
        return self._repo.get_and_reset_bytes_fetched()

    @staticmethod
    def _safe(func: Callable[[], T]) -> T | None:
        try:
            return func()
        except Exception as error:  # pragma: no cover - defensive fall-through
            logger.warning("Postgres backend error, falling back to file cache: %s", error)
            return None
