"""Read-only MDBList identity probe for an identity-resolver JSON report.

Reads the resolver report and queries MDBList by the already selected TMDB ID.
It never writes to catalog or tracking databases and never changes resolver data.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import quote

import niquests

API_BASE = "https://api.mdblist.com"
TMDB_ID_RE = re.compile(r"^[1-9]\d*$")


def _media_provider_type(item: dict, override: str) -> str | None:
    if override in {"movie", "show"}:
        return override
    media_type = item.get("media_type")
    if media_type == "movie":
        return "movie"
    if media_type in {"series", "show"}:
        return "show"
    url = str(item.get("url") or "")
    if "/anime/stream/" in url or "/serie/stream/" in url:
        return "show"
    if "/film/stream/" in url or "/movie/stream/" in url:
        return "movie"
    return None


def _lookup(api_key: str, media_type: str, tmdb_id: int) -> tuple[int, dict]:
    # MDBList media lookup: GET /{media_provider}/{media_type}/{media_id}/
    url = f"{API_BASE}/tmdb/{media_type}/{tmdb_id}/"
    response = niquests.get(url, params={"apikey": api_key}, timeout=20)
    try:
        payload = response.json()
    except Exception:
        payload = {}
    if response.status_code >= 400:
        detail = payload.get("error") if isinstance(payload, dict) else None
        raise RuntimeError(f"HTTP {response.status_code}" + (f": {detail}" if detail else ""))
    if not isinstance(payload, dict):
        raise RuntimeError("Unexpected MDBList response (not a JSON object)")
    return response.status_code, payload


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only MDBList lookup for missing IMDb IDs in an identity resolver report"
    )
    parser.add_argument(
        "--report", default="/app/data/identity-resolver-report.json",
        help="Path to identity-resolver-report.json",
    )
    parser.add_argument(
        "--media-type", choices=("auto", "movie", "show"), default="auto",
        help="Override media type; auto detects it from the report row/provider URL",
    )
    parser.add_argument("--limit", type=int, default=0, help="Maximum rows; 0 means all")
    parser.add_argument("--delay", type=float, default=0.25, help="Seconds between API requests")
    args = parser.parse_args()

    api_key = os.getenv("MDBLIST_API_KEY", "").strip()
    if not api_key:
        print("ERROR: MDBLIST_API_KEY is not set in the container environment.", file=sys.stderr)
        return 2

    try:
        report = json.loads(Path(args.report).read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"ERROR: Could not read report: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    rows = report.get("results") if isinstance(report, dict) else None
    if not isinstance(rows, list):
        print("ERROR: Report does not contain a results array.", file=sys.stderr)
        return 2

    targets = [row for row in rows if isinstance(row, dict) and row.get("status") == "missing_imdb"]
    if args.limit > 0:
        targets = targets[:args.limit]

    output = []
    for index, item in enumerate(targets):
        raw_tmdb_id = str(item.get("tmdb_id") or "").strip()
        media_type = _media_provider_type(item, args.media_type)
        row = {
            "title": item.get("title"),
            "provider": item.get("provider"),
            "tmdb_id": raw_tmdb_id or None,
            "media_type": media_type,
            "resolver_status": item.get("status"),
        }
        if not TMDB_ID_RE.fullmatch(raw_tmdb_id):
            row.update({"lookup_status": "skipped", "reason": "Missing or invalid TMDB ID"})
        elif media_type is None:
            row.update({"lookup_status": "skipped", "reason": "Could not infer media type; use --media-type"})
        else:
            try:
                _, payload = _lookup(api_key, media_type, int(raw_tmdb_id))
                ids = payload.get("ids") if isinstance(payload.get("ids"), dict) else {}
                imdb_id = str(ids.get("imdb") or payload.get("imdb_id") or "").strip()
                row.update({
                    "lookup_status": "found" if imdb_id else "no_imdb_id",
                    "imdb_id": imdb_id or None,
                    "returned_title": payload.get("title") or payload.get("name"),
                    "returned_year": payload.get("year"),
                    "returned_type": payload.get("type") or payload.get("mediatype"),
                    "returned_ids": ids,
                    "response_keys": sorted(payload.keys()),
                })
                if imdb_id and not re.fullmatch(r"tt\d{7,10}", imdb_id):
                    row["lookup_status"] = "invalid_imdb_id"
            except Exception as exc:
                row.update({
                    "lookup_status": "error",
                    "reason": str(exc),
                })
        output.append(row)
        print(json.dumps(row, ensure_ascii=False))
        if index + 1 < len(targets) and args.delay > 0:
            time.sleep(args.delay)

    counts = {}
    for row in output:
        status = row["lookup_status"]
        counts[status] = counts.get(status, 0) + 1
    print("\nSUMMARY")
    print(json.dumps({
        "checked": len(output),
        "counts": counts,
        "read_only": True,
        "database_writes": 0,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
