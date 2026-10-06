#!/usr/bin/env python3
"""Hybrid-search quality eval: golden queries → expected articles.

Runs every query in evals/golden.json through HybridSearch against the live
Postgres corpus and reports recall@k + MRR. A query passes when any expected
article appears in the top-k (an expectation without article_num passes on
any hit from that law).

Usage:
    DATABASE_URL=postgresql://... python scripts/eval_search.py
    DATABASE_URL=... python scripts/eval_search.py --min-recall 0.7
    DATABASE_URL=... python scripts/eval_search.py --report-only

Exit code is non-zero when recall@k falls below --min-recall (unless
--report-only), so it can gate CI.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from search.embeddings import make_embedder  # noqa: E402
from search.hybrid_search import HybridSearch  # noqa: E402
from storage.repository import PostgresRepository  # noqa: E402

GOLDEN_PATH = Path(__file__).resolve().parent.parent / "evals" / "golden.json"


def _matches(hit: dict, expected: dict) -> bool:
    if hit.get("law_id") != expected.get("law_id"):
        return False
    expected_num = expected.get("article_num")
    if expected_num is None:
        return True
    # Giant articles are stored chunked as «num», «num#2»…; any chunk counts.
    hit_num = str(hit.get("article_num") or "")
    return hit_num == expected_num or hit_num.startswith(f"{expected_num}#")


def main() -> int:
    parser = argparse.ArgumentParser(description="Hybrid-search quality eval.")
    parser.add_argument("--min-recall", type=float, default=0.6)
    parser.add_argument("--report-only", action="store_true")
    parser.add_argument("--golden", type=Path, default=GOLDEN_PATH)
    args = parser.parse_args()

    dsn = os.getenv("DATABASE_URL", "").strip()
    if not dsn:
        parser.error("DATABASE_URL is required")

    golden = json.loads(args.golden.read_text(encoding="utf-8"))
    k = int(golden.get("k", 5))
    queries = golden["queries"]

    embedder = make_embedder()
    repo = PostgresRepository(dsn, embedder=embedder)
    search = HybridSearch(repo, embedder)

    passed = 0
    reciprocal_ranks: list[float] = []
    print(f"Hybrid-search eval: {len(queries)} queries, k={k}, embedder={embedder.model}\n")
    try:
        for item in queries:
            results = search.search_articles(item["query"], item.get("law_id"), k)
            rank = next(
                (
                    index
                    for index, hit in enumerate(results, start=1)
                    if any(_matches(hit, expected) for expected in item["expected"])
                ),
                None,
            )
            ok = rank is not None
            passed += ok
            reciprocal_ranks.append(1.0 / rank if rank else 0.0)
            top = ", ".join(f"{hit.get('law_id')}:{hit.get('article_num')}" for hit in results[:3])
            print(
                f"  {'PASS' if ok else 'FAIL'} {item['id']:28} "
                f"rank={rank or '-':<3} top: {top or '(no results)'}"
            )
    finally:
        repo.close()

    recall = passed / len(queries) if queries else 0.0
    mrr = sum(reciprocal_ranks) / len(reciprocal_ranks) if reciprocal_ranks else 0.0
    print(f"\nrecall@{k} = {recall:.2f} ({passed}/{len(queries)})   MRR = {mrr:.2f}")

    if not args.report_only and recall < args.min_recall:
        print(f"FAIL: recall@{k} {recall:.2f} < required {args.min_recall:.2f}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
