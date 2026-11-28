"""Command line entry point for catalog crawls.

Example: python -m providers --provider aniworld
"""
import argparse
import json

from .crawler import crawl_named_provider


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a provider catalog crawl")
    parser.add_argument(
        "--provider",
        required=True,
        choices=("aniworld", "serienstream"),
        help="Provider to crawl",
    )
    args = parser.parse_args()
    print(json.dumps(crawl_named_provider(args.provider), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
