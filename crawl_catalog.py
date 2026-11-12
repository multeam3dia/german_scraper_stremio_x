"""Run one or both catalog provider crawls from the command line.

Examples:
    python -m crawl_catalog all
    python -m crawl_catalog aniworld
    python -m crawl_catalog serienstream
"""
from __future__ import annotations

import argparse
import json
import sys

from providers.crawler import crawl_all_catalog_providers, crawl_named_provider


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a conservative catalog crawl")
    parser.add_argument(
        "provider",
        nargs="?",
        default="all",
        choices=("all", "aniworld", "serienstream"),
        help="Provider to crawl (default: all, sequentially)",
    )
    parser.add_argument(
        "--max-details",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Maximum detail pages per provider in this run "
            "(default: CATALOG_CRAWL_MAX_DETAILS_PER_RUN or 200)"
        ),
    )
    parser.add_argument(
        "--index-only",
        action="store_true",
        help="Read and validate provider indexes without fetching new detail pages",
    )
    args = parser.parse_args()
    if args.max_details is not None and args.max_details < 1:
        parser.error("--max-details must be at least 1")

    results = (
        crawl_all_catalog_providers(
            index_only=args.index_only, max_details=args.max_details
        )
        if args.provider == "all"
        else [
            crawl_named_provider(
                args.provider,
                index_only=args.index_only,
                max_details=args.max_details,
            )
        ]
    )
    print(json.dumps(results, ensure_ascii=False, indent=2))
    successful = all(
        item.get("status") == "completed"
        or (item.get("detail_limit_reached") and not item.get("blocked"))
        for item in results
    )
    return 0 if results and successful else 1


if __name__ == "__main__":
    sys.exit(main())
