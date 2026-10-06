#!/usr/bin/env python3
"""CLI to ingest Ukrainian laws into the Postgres backend.

Usage:
    DATABASE_URL=postgresql://... python scripts/ingest.py --all
    DATABASE_URL=postgresql://... python scripts/ingest.py --law 922-19 --law 435-15
    DATABASE_URL=postgresql://... python scripts/ingest.py --all --ensure-schema

    # Resumable maintenance (operate on already-ingested DB rows, no re-fetch):
    DATABASE_URL=postgresql://... python scripts/ingest.py --backfill-articles
    GOOGLE_API_KEY=... DATABASE_URL=postgresql://... python scripts/ingest.py --embed-missing

Embeddings are generated only if an embedder is configured (GOOGLE_API_KEY or
EMBEDDING_PROVIDER); by default ingestion is text + Ukrainian FTS only (0 UAH).
``--embed-missing`` is idempotent and resumable — re-run it until 0 remain to
finish embedding generation across free-tier rate limits.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Allow running as a plain script (repo uses a flat module layout).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from search.embeddings import make_embedder  # noqa: E402
from storage.ingestion import Ingestor  # noqa: E402
from registries.law_registry import LawRegistry  # noqa: E402
from storage.repository import PostgresRepository  # noqa: E402
from search.search_engine import SearchEngine  # noqa: E402
from registries.source_adapters import ZakonRadaAdapter  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Ingest Ukrainian laws into Postgres.")
    parser.add_argument("--all", action="store_true", help="Ingest every law in the registry.")
    parser.add_argument("--law", action="append", default=[], help="Specific law_id (repeatable).")
    parser.add_argument(
        "--ensure-schema", action="store_true", help="Apply db/schema.sql before ingesting."
    )
    parser.add_argument(
        "--backfill-articles",
        action="store_true",
        help="Chunk full_text into fragments for already-ingested laws that have no articles.",
    )
    parser.add_argument(
        "--split-giants",
        action="store_true",
        help="Re-chunk oversized parser-artifact articles already in the DB (no network).",
    )
    parser.add_argument(
        "--embed-missing",
        action="store_true",
        help="Embed current-redaction articles that have no vector yet (resumable; needs key).",
    )
    args = parser.parse_args()

    dsn = os.getenv("DATABASE_URL", "").strip()
    if not dsn:
        parser.error("DATABASE_URL is required")
    if not (
        args.all or args.law or args.backfill_articles or args.split_giants or args.embed_missing
    ):
        parser.error(
            "pass --all, one or more --law <id>, --backfill-articles, "
            "--split-giants, or --embed-missing"
        )

    base_dir = Path(__file__).resolve().parent.parent
    registry = LawRegistry(base_dir / "cache" / "laws.json")
    search = SearchEngine()
    adapter = ZakonRadaAdapter(registry, search)
    embedder = make_embedder()
    repo = PostgresRepository(dsn, embedder=embedder)
    ingestor = Ingestor(repo, adapter, search, registry, embedder)
    exit_code = 0

    try:
        if args.ensure_schema:
            repo.ensure_schema()
            print("schema applied")

        if args.all or args.law:
            law_ids = None if args.all else list(args.law)
            summary = ingestor.ingest_all(law_ids)
            print(
                f"\nIngest complete: total={summary.total} ok={summary.ok} "
                f"created={summary.created} skipped={summary.skipped} failed={summary.failed} "
                f"(embedder={embedder.model})"
            )
            for result in summary.results:
                if not result.ok:
                    print(f"  FAILED {result.law_id}: {result.error}")
            if summary.failed and not summary.ok:
                exit_code = 1

        if args.backfill_articles:
            created = ingestor.backfill_articles()
            print(f"\nArticle backfill complete: {created} fragments created")

        if args.split_giants:
            replaced = ingestor.backfill_split_articles()
            print(f"\nGiant-article split complete: {replaced} oversized articles re-chunked")

        if args.embed_missing:
            written = ingestor.backfill_embeddings()
            stats = repo.stats()
            print(
                f"\nEmbedding backfill: {written} written this run; "
                f"{stats.get('pending_embeddings', 0)} still pending "
                f"({stats.get('embeddings', 0)} total, embedder={embedder.model})"
            )

        return exit_code
    finally:
        repo.close()


if __name__ == "__main__":
    raise SystemExit(main())
