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
        "--index-only",
        action="store_true",
        help="Read and validate provider indexes without fetching new detail pages",
    )
    args = parser.parse_args()

    results = (
        crawl_all_catalog_providers(index_only=args.index_only)
        if args.provider == "all"
        else [crawl_named_provider(args.provider, index_only=args.index_only)]
    )
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0 if results and all(item.get("status") == "completed" for item in results) else 1


if __name__ == "__main__":
    sys.exit(main())
