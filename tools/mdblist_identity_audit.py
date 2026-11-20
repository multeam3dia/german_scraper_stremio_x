"""Compare MDBList API coverage against user-verified AniWorld identities.

Run on the deployed app/container (or locally) with MDBLIST_API_KEY set:
    python -m tools.mdblist_identity_audit --limit 34

The audit is read-only. It does not modify the catalog or the production resolver.
It performs title searches for both shows and movies, plus exact IMDb-ID lookups
for both media types. Responses are reduced to safe metadata summaries; the API
key is never written to the report or logs.

The free MDBList tier currently allows 1,000 requests/day. A full run of the
34-entry fixture uses at most 142 GET requests (Persona 3 has four IMDb IDs).
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import time
import unicodedata
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

API_BASE = os.getenv("MDBLIST_API_BASE", "https://api.mdblist.com").rstrip("/")
MEDIA_TYPES = ("show", "movie")
IMDB_RE = re.compile(r"^tt\d{7,10}$")
FIXTURE = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "aniworld_identity_ground_truth.json"


class MDBListAuditError(RuntimeError):
    pass


def normalize(value: Any) -> str:
    value = unicodedata.normalize("NFKD", str(value or "").casefold())
    value = "".join(char for char in value if not unicodedata.combining(char))
    return " ".join(re.findall(r"[^\W_]+", value, flags=re.UNICODE))


def similarity(left: str, right: str) -> float:
    a, b = normalize(left), normalize(right)
    if not a or not b:
        return 0.0
    if a == b or a.replace(" ", "") == b.replace(" ", ""):
        return 1.0
    return round(difflib.SequenceMatcher(None, a, b).ratio(), 4)


def _field_paths(value: Any, prefix: str = "", depth: int = 0) -> set[str]:
    """Collect response field names without retaining personal/account data."""
    paths: set[str] = set()
    if depth >= 8:
        return paths
    if isinstance(value, dict):
        for key, child in value.items():
            key = str(key)
            path = f"{prefix}.{key}" if prefix else key
            paths.add(path)
            paths.update(_field_paths(child, path, depth + 1))
    elif isinstance(value, list):
        for child in value[:2]:
            paths.update(_field_paths(child, prefix + "[]", depth + 1))
    return paths


def _candidate_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("search", "results", "items", "movies", "shows", "data"):
        rows = payload.get(key)
        if isinstance(rows, list):
            return [item for item in rows if isinstance(item, dict)]
    # Some API versions return a single media object.
    if any(key in payload for key in ("title", "name", "ids", "imdb_id", "imdb")):
        return [payload]
    return []


def _imdb_id(item: dict[str, Any]) -> str | None:
    ids = item.get("ids") if isinstance(item.get("ids"), dict) else {}
    # MDBList search uses ids.imdbid; media detail lookups use ids.imdb.
    # Accept both documented shapes so search hits are not silently discarded.
    value = (
        ids.get("imdb") or ids.get("imdbid") or ids.get("imdb_id")
        or item.get("imdb_id") or item.get("imdbid") or item.get("imdb")
    )
    value = str(value or "").strip()
    return value if IMDB_RE.fullmatch(value) else None


def _summary(item: dict[str, Any], expected_title: str) -> dict[str, Any]:
    title = item.get("title") or item.get("name") or item.get("original_title") or item.get("original_name")
    year = item.get("year") or item.get("release_year") or item.get("released") or item.get("release_date") or item.get("first_aired")
    if isinstance(year, str):
        year_match = re.search(r"\b(?:19|20)\d{2}\b", year)
        year = year_match.group(0) if year_match else year
    return {
        "title": str(title) if title else None,
        "year": year,
        "imdb_id": _imdb_id(item),
        "title_similarity": similarity(expected_title, str(title or "")),
        "available_fields": sorted(_field_paths(item)),
    }


def _get_json(path: str, params: dict[str, Any], api_key: str) -> tuple[Any, dict[str, str]]:
    query = dict(params)
    query["apikey"] = api_key
    url = f"{API_BASE}{path}?{urlencode(query)}"
    request = Request(url, headers={
        "Accept": "application/json",
        "User-Agent": "GermanScraperStremioX-MDBListAudit/1.0",
    })
    try:
        with urlopen(request, timeout=25) as response:
            payload = json.loads(response.read().decode("utf-8"))
            headers = {key.lower(): value for key, value in response.headers.items()}
    except HTTPError as exc:
        retry_after = exc.headers.get("Retry-After", "") if exc.headers else ""
        raise MDBListAuditError(
            f"MDBList HTTP {exc.code}" + (f" (Retry-After {retry_after}s)" if retry_after else "")
        ) from None
    except (URLError, TimeoutError, OSError):
        raise MDBListAuditError("Could not reach MDBList") from None
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise MDBListAuditError("MDBList returned invalid JSON") from None
    return payload, headers


def _load_fixture() -> dict[str, Any]:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    if not isinstance(fixture.get("entries"), list):
        raise MDBListAuditError("Ground-truth fixture has no entries array")
    return fixture


def run_audit(*, limit: int | None = None, pause: float = 0.15) -> dict[str, Any]:
    api_key = os.getenv("MDBLIST_API_KEY", "").strip()
    if not api_key:
        raise MDBListAuditError("MDBLIST_API_KEY is not configured")
    fixture = _load_fixture()
    entries = fixture["entries"][:limit] if limit else fixture["entries"]
    report: dict[str, Any] = {
        "audit_version": 1,
        "provider": fixture.get("provider"),
        "fixture_source": fixture.get("source"),
        "entries_scanned": len(entries),
        "requests": 0,
        "rate_limit": {},
        "results": [],
        "errors": [],
    }
    latest_headers: dict[str, str] = {}

    def request(path: str, params: dict[str, Any]) -> Any:
        payload, headers = _get_json(path, params, api_key)
        report["requests"] += 1
        for name in ("x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset"):
            if name in headers:
                latest_headers[name] = headers[name]
        report["rate_limit"] = dict(latest_headers)
        if pause:
            time.sleep(pause)
        return payload

    for entry in entries:
        title = str(entry.get("title") or "")
        ids = [value for value in entry.get("imdb_ids", []) if IMDB_RE.fullmatch(str(value))]
        result: dict[str, Any] = {
            "title": title,
            "expected_imdb_ids": ids,
            "note": entry.get("note"),
            "search": {},
            "exact_lookup": {},
        }
        for media_type in MEDIA_TYPES:
            try:
                payload = request(f"/search/{media_type}", {"query": title, "limit": 20})
                rows = _candidate_rows(payload)
                result["search"][media_type] = {
                    "candidate_count": len(rows),
                    "expected_id_hits": sorted({
                        found for row in rows
                        if (found := _imdb_id(row)) in ids
                    }),
                    "candidates": sorted(
                        (_summary(row, title) for row in rows),
                        key=lambda row: (row["title_similarity"], bool(row["imdb_id"])),
                        reverse=True,
                    )[:10],
                    "response_fields": sorted(_field_paths(payload)),
                }
            except MDBListAuditError as exc:
                result["search"][media_type] = {"error": str(exc)}
                report["errors"].append({"title": title, "operation": f"search/{media_type}", "error": str(exc)})
                if "HTTP 429" in str(exc):
                    report["results"].append(result)
                    report["aborted_rate_limit"] = True
                    report["rate_limit"] = dict(latest_headers)
                    return report

        for imdb_id in ids:
            result["exact_lookup"][imdb_id] = {}
            for media_type in MEDIA_TYPES:
                try:
                    payload = request(f"/imdb/{media_type}/{quote(str(imdb_id), safe='')}/", {})
                    rows = _candidate_rows(payload)
                    summaries = [_summary(row, title) for row in rows]
                    result["exact_lookup"][imdb_id][media_type] = {
                        "returned": bool(rows),
                        "returned_ids": sorted({row["imdb_id"] for row in summaries if row["imdb_id"]}),
                        "candidates": summaries[:5],
                        "response_fields": sorted(_field_paths(payload)),
                    }
                except MDBListAuditError as exc:
                    result["exact_lookup"][imdb_id][media_type] = {"error": str(exc)}
                    report["errors"].append({
                        "title": title, "operation": f"imdb/{media_type}/{imdb_id}", "error": str(exc)
                    })
                    if "HTTP 429" in str(exc):
                        report["results"].append(result)
                        report["aborted_rate_limit"] = True
                        report["rate_limit"] = dict(latest_headers)
                        return report
        report["results"].append(result)

    report["summary"] = summarize(report["results"])
    return report


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    expected_ids = 0
    exact_lookup_hits = 0
    search_hits = 0
    show_search_hits = 0
    movie_search_hits = 0
    for result in results:
        ids = result.get("expected_imdb_ids", [])
        expected_ids += len(ids)
        for imdb_id in ids:
            lookups = result.get("exact_lookup", {}).get(imdb_id, {})
            if any(
                imdb_id in (lookups.get(kind, {}).get("returned_ids") or [])
                for kind in MEDIA_TYPES
            ):
                exact_lookup_hits += 1
        for kind, counter in (("show", "show_search_hits"), ("movie", "movie_search_hits")):
            hits = set(result.get("search", {}).get(kind, {}).get("expected_id_hits", []))
            if hits:
                if kind == "show":
                    show_search_hits += len(hits)
                else:
                    movie_search_hits += len(hits)
                search_hits += len(hits)
    return {
        "expected_imdb_id_count": expected_ids,
        "exact_imdb_lookup_hits": exact_lookup_hits,
        "title_search_expected_id_hits": search_hits,
        "show_search_expected_id_hits": show_search_hits,
        "movie_search_expected_id_hits": movie_search_hits,
        "note": "A successful exact-ID lookup proves ID resolution, not title-search ranking or episode/credits coverage.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only MDBList identity/metadata coverage audit")
    parser.add_argument("--limit", type=int, default=None, help="Audit only the first N fixture entries")
    parser.add_argument("--pause", type=float, default=0.15, help="Delay between API calls in seconds")
    parser.add_argument("--output", default="data/mdblist_identity_audit.json")
    args = parser.parse_args()
    try:
        report = run_audit(limit=args.limit, pause=max(0.0, args.pause))
    except MDBListAuditError as exc:
        raise SystemExit(str(exc)) from None
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "output": str(output),
        "requests": report["requests"],
        "entries_scanned": report["entries_scanned"],
        "summary": report.get("summary"),
        "errors": len(report["errors"]),
        "aborted_rate_limit": report.get("aborted_rate_limit", False),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
