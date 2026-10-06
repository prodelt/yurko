"""Lexical search over the hot local cache: a SQLite FTS5 index, stdlib only.

Why lexical and not vector: the shipped Postgres path has no Ukrainian stemmer
either — ``db/schema.sql`` builds the ``ukrainian`` configuration on top of
``simple`` + ``unaccent`` and says so ("no stemming"). A local FTS5 index is
therefore morphologically *equal* to production, not a regression, and it costs
zero new dependencies: this CPython already ships sqlite 3.49.1 with
ENABLE_FTS5. The vector alternative was measured and rejected elsewhere.

What this module refuses to do: pretend. The index covers only documents that
were already fetched into ``cache/texts`` — a fraction of the registry. Every
answer therefore carries the covered law ids and a scope note, and the three
states (no index / empty index / unusable query) are separate typed refusals
rather than an empty list, because an empty list reads as "searched everything,
found nothing" (Constitution III).
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from search.search_engine import SearchEngine
from core.source_cache import atomic_replace, cache_path

#: File name of the index inside the source cache directory.
INDEX_FILENAME = "local_index.sqlite3"

#: Bumped whenever the table layout changes; a stale version is reported as an
#: unusable index instead of being queried, because a silently wrong schema
#: would surface as "nothing found" rather than as an error.
SCHEMA_VERSION = 1

#: Manual route offered with every refusal. It has to be concrete: a refusal
#: that does not tell the lawyer where to go by hand is not an honest refusal.
MANUAL_PATH = (
    "індекс локального кешу недоступний: перебудуйте його (local_index.build_index "
    "над cache/texts), або прочитайте документ за ідентифікатором через get_article, "
    "або шукайте вручну на https://zakon.rada.gov.ua"
)

_TOKENIZER = re.compile(r"[^\W_]+", re.UNICODE)

#: Reused, not copied, from the in-memory scorer: two stop lists that drift
#: apart would make the same query behave differently on the two paths.
_STOP_WORDS = SearchEngine._stop_words

#: Prefix rewriting exists because there is no stemmer, not because it is
#: "better for quality". A query token is cut back to a stem-shaped prefix so
#: that «відкликані» can reach «Відкликання» and «ліцензії» can reach «ліцензій».
#: On FTS5 the untrimmed «"відкликані"* AND "ліцензії"*» matches the revocation
#: article ZERO times; the trimmed form matches it first.
_MIN_PREFIX_CHARS = 5
_STEM_TRIM_CHARS = 3

#: A heading hit is worth more than a body hit: «Стаття 22. Відкликання
#: ліцензій» is the article the lawyer asked for, while the same words deep in
#: another article's body are usually a cross-reference.
_BM25_HEADING_WEIGHT = 5.0
_BM25_BODY_WEIGHT = 1.0

_SCHEMA = """
CREATE TABLE meta(
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE docs(
    rowid       INTEGER PRIMARY KEY,
    law_id      TEXT NOT NULL,
    law_title   TEXT NOT NULL,
    article_num TEXT NOT NULL,
    heading     TEXT NOT NULL,
    body        TEXT NOT NULL
);
CREATE INDEX docs_law_id ON docs(law_id);
CREATE VIRTUAL TABLE docs_fts USING fts5(
    heading,
    body,
    content=docs,
    content_rowid=rowid,
    tokenize='unicode61 remove_diacritics 2'
);
"""


@dataclass(frozen=True)
class LocalHit:
    """One article (or text fragment) matched in the hot cache."""

    law_id: str
    law_title: str
    #: An article number ("22", "179-1") or, for laws whose structure does not
    #: parse (КМУ постанови with пункти), a fragment key "frag-0001". The caller
    #: must not print the second kind as an article number.
    article_num: str
    heading: str
    snippet: str
    #: Raw bm25 output: lower is better and the value is negative. Kept raw so
    #: nobody mistakes it for the density score of ``search_engine`` — the two
    #: scales are not comparable and must never be sorted together.
    bm25: float


@dataclass(frozen=True)
class LocalSearchResult:
    """A search that actually ran, with the boundary of what it covered."""

    query: str
    fts_query: str
    hits: tuple[LocalHit, ...]
    covered_law_ids: tuple[str, ...]
    indexed_documents: int
    scope_note: str


@dataclass(frozen=True)
class IndexUnavailable:
    """Typed refusal: the search did not run, and here is the manual route.

    Deliberately not a ``LocalSearchResult`` with zero hits — the caller must be
    unable to render "нічого не знайдено" for a search that never happened.
    """

    #: ``not_built`` | ``empty`` | ``schema_mismatch`` | ``unusable_query``
    reason: str
    index_path: str
    manual_path: str = MANUAL_PATH
    detail: str = ""


@dataclass(frozen=True)
class BuildReport:
    """What a rebuild actually put into the index."""

    index_path: str
    indexed_documents: int
    covered_law_ids: tuple[str, ...]
    skipped_law_ids: tuple[str, ...] = field(default_factory=tuple)
    index_bytes: int = 0


def default_index_path() -> Path:
    """Where the index lives when the caller does not say.

    Resolved through ``source_cache`` rather than computed here: the cache
    directory is the owner's choice (YURKO_CACHE_DIR), and a second opinion
    about it would put the index somewhere the rest of the product does not
    look — or, worse, inside a case folder.
    """
    return Path(cache_path(INDEX_FILENAME))


def rewrite_query(query: str) -> str:
    """Turn a natural query into an FTS5 AND-expression of stem prefixes.

    Every term is quoted before ``*`` is appended: unquoted ``-``, ``.`` or a
    bare ``NOT`` are FTS5 syntax, and a syntax error here would surface to the
    lawyer as "nothing found". Terms are joined with AND, never OR: an OR
    fan-out returns documents that match one word of a two-word question, which
    is exactly the irrelevant output principle III calls worse than empty.
    """
    terms: list[str] = []
    for raw in _TOKENIZER.findall(query.lower()):
        if raw in _STOP_WORDS:
            continue
        if len(raw) >= _MIN_PREFIX_CHARS:
            stem = raw[: max(_MIN_PREFIX_CHARS, len(raw) - _STEM_TRIM_CHARS)]
            terms.append(f'"{stem}"*')
        else:
            # Too short to trim: a 2-3 character prefix would match half the
            # corpus, so the token is required verbatim instead.
            terms.append(f'"{raw}"')
    return " AND ".join(terms)


def build_index(
    texts_dir: str | Path,
    *,
    index_path: str | Path | None = None,
) -> BuildReport:
    """Rebuild the FTS5 index from the cached law texts. Safe to repeat.

    The index is written to a temporary file next to its destination and moved
    into place in one step, so a crash mid-build leaves the previous index
    intact rather than a half-written one that would answer wrongly.
    """
    source_dir = Path(texts_dir)
    final_path = Path(index_path) if index_path is not None else default_index_path()
    final_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = final_path.with_name(f"{final_path.name}.tmp-{os.getpid()}")
    if tmp_path.exists():
        tmp_path.unlink()

    engine = SearchEngine()
    covered: list[str] = []
    skipped: list[str] = []
    indexed = 0

    try:
        connection = sqlite3.connect(tmp_path)
        try:
            # No journal: the build writes to a throwaway file, and a leftover
            # ``-journal`` sidecar would survive the rename as orphaned garbage.
            connection.execute("PRAGMA journal_mode=OFF")
            connection.executescript(_SCHEMA)
            for path in sorted(source_dir.glob("*.json")):
                indexed += _index_one(connection, engine, path, covered, skipped)
            connection.execute("INSERT INTO docs_fts(docs_fts) VALUES('rebuild')")
            connection.executemany(
                "INSERT INTO meta(key, value) VALUES (?, ?)",
                [
                    ("schema_version", str(SCHEMA_VERSION)),
                    ("built_at", datetime.now(timezone.utc).isoformat()),
                    ("source_dir", str(source_dir)),
                    ("document_count", str(indexed)),
                ],
            )
            connection.commit()
        finally:
            # Closed before the rename, not after: Windows refuses to replace a
            # file that is still open, so a live connection would break the swap.
            connection.close()
    except BaseException:
        # A failed build must leave nothing behind. The unlink above only clears
        # a leftover from this same process, so without this a crashed run would
        # park a half-written file next to the live index for good.
        tmp_path.unlink(missing_ok=True)
        raise

    # Same directory as the destination, which is what source_cache requires:
    # across volumes os.replace degrades into a copy and stops being atomic.
    atomic_replace(tmp_path, final_path)
    return BuildReport(
        index_path=str(final_path),
        indexed_documents=indexed,
        covered_law_ids=tuple(covered),
        skipped_law_ids=tuple(skipped),
        index_bytes=final_path.stat().st_size,
    )


def search_local(
    query: str,
    *,
    index_path: str | Path | None = None,
    limit: int = 10,
) -> LocalSearchResult | IndexUnavailable:
    """Search the hot cache, or refuse in a way the caller cannot mistake."""
    path = Path(index_path) if index_path is not None else default_index_path()
    if not path.exists():
        return IndexUnavailable(
            reason="not_built",
            index_path=str(path),
            detail="індекс локального кешу ще не побудовано",
        )

    fts_query = rewrite_query(query)
    connection = sqlite3.connect(path)
    try:
        connection.row_factory = sqlite3.Row
        version = _meta_value(connection, "schema_version")
        if version != str(SCHEMA_VERSION):
            return IndexUnavailable(
                reason="schema_mismatch",
                index_path=str(path),
                detail=f"індекс має версію схеми {version!r}, очікується {SCHEMA_VERSION}",
            )
        indexed = int(connection.execute("SELECT count(*) FROM docs").fetchone()[0])
        if indexed == 0:
            return IndexUnavailable(
                reason="empty",
                index_path=str(path),
                detail="індекс побудовано, але він порожній: у гарячому кеші немає текстів",
            )
        if not fts_query:
            return IndexUnavailable(
                reason="unusable_query",
                index_path=str(path),
                detail="у запиті немає слів, придатних для пошуку (лишились самі службові)",
            )
        covered = tuple(
            str(row[0]) for row in connection.execute("SELECT DISTINCT law_id FROM docs ORDER BY 1")
        )
        try:
            rows = connection.execute(
                "SELECT d.law_id, d.law_title, d.article_num, d.heading, "
                "       snippet(docs_fts, 1, '', '', ' … ', 14) AS excerpt, "
                "       bm25(docs_fts, ?, ?) AS score "
                "FROM docs_fts JOIN docs d ON d.rowid = docs_fts.rowid "
                "WHERE docs_fts MATCH ? ORDER BY score LIMIT ?",
                (_BM25_HEADING_WEIGHT, _BM25_BODY_WEIGHT, fts_query, max(int(limit), 1)),
            ).fetchall()
        except sqlite3.OperationalError as error:
            # Reached only if rewrite_query let something through: an FTS5
            # syntax error must not be rendered as "nothing found".
            return IndexUnavailable(
                reason="unusable_query",
                index_path=str(path),
                detail=f"FTS5 відхилив запит {fts_query!r}: {error}",
            )
    finally:
        connection.close()

    hits = tuple(
        LocalHit(
            law_id=str(row["law_id"]),
            law_title=str(row["law_title"]),
            article_num=str(row["article_num"]),
            heading=str(row["heading"]),
            snippet=str(row["excerpt"]),
            bm25=float(row["score"]),
        )
        for row in rows
    )
    return LocalSearchResult(
        query=query,
        fts_query=fts_query,
        hits=hits,
        covered_law_ids=covered,
        indexed_documents=indexed,
        scope_note=(
            f"Шукали лише в гарячому локальному кеші: {indexed} фрагментів "
            f"з {len(covered)} документів. Це не пошук по всьому праву України — "
            "документи поза кешем не переглядались."
        ),
    )


def _index_one(
    connection: sqlite3.Connection,
    engine: SearchEngine,
    path: Path,
    covered: list[str],
    skipped: list[str],
) -> int:
    """Insert one cached law and report how many units it contributed."""
    entry = _load_entry(path)
    if entry is None:
        skipped.append(path.stem)
        return 0
    law_id, law_title, text = entry
    sections = _sections_for(engine, text)
    if not sections:
        skipped.append(law_id)
        return 0
    connection.executemany(
        "INSERT INTO docs(law_id, law_title, article_num, heading, body) VALUES (?, ?, ?, ?, ?)",
        [
            (law_id, law_title, number, _heading_of(body, number), body)
            for number, body in sections.items()
        ],
    )
    covered.append(law_id)
    return len(sections)


def _load_entry(path: Path) -> tuple[str, str, str] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    text = str(payload.get("text") or "")
    if not text.strip():
        return None
    law_id = str(payload.get("law_id") or path.stem)
    return law_id, str(payload.get("title") or law_id), text


def _sections_for(engine: SearchEngine, text: str) -> dict[str, str]:
    """Split a law into indexable units, re-parsing rather than trusting cache.

    ``entry["articles"]`` in ``cache/texts`` was produced by the old parser,
    which collapsed «Стаття 179-1» onto the key «179». Re-parsing here means a
    rebuilt index is correct without waiting for every cached file to be
    re-fetched. Laws with no «статті» at all (КМУ постанови, which use пункти)
    fall back to sequential fragments so they are covered rather than dropped.
    """
    articles = engine.parse_articles(text)
    return articles if articles else engine.chunk_text(text)


#: Lines that carry only the article number, in the shapes Rada's print layout
#: wraps it into: "Стаття 179", then "-", "1", "." on lines of their own.
_HEADER_NOISE = re.compile(r"^(?:[Сс]таття|\d+|[-–.)]+)$")
_ARTICLE_PREFIX = re.compile(r"^[Сс]таття\s+\d+(?:\s*[-–]\s*\d+)?\s*[.)]?\s*")


def _heading_of(body: str, article_num: str) -> str:
    """Rebuild "Стаття N. Назва" from a body whose header may be wrapped.

    Worth the effort because the heading column is weighted five times the body
    in bm25: if the wrapped "-\n1\n." lines leaked in as the heading, the very
    articles this task is about would rank on punctuation.
    """
    label = (
        f"Фрагмент {article_num}" if article_num.startswith("frag-") else f"Стаття {article_num}"
    )
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped or _HEADER_NOISE.match(stripped):
            continue
        name = _ARTICLE_PREFIX.sub("", stripped).strip()
        if name:
            return f"{label}. {name}"[:300]
    return label


def _meta_value(connection: sqlite3.Connection, key: str) -> str:
    try:
        row = connection.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    except sqlite3.DatabaseError:
        return ""
    return str(row[0]) if row else ""
