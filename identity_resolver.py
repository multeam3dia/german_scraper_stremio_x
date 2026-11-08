"""Confidence-aware provider-title to IMDb identity resolution via TMDB.

The resolver is intentionally conservative: it never promotes a title based on
fuzzy title similarity alone. A candidate must have a strong title match, a
compatible release year, a valid IMDb ID from TMDB external IDs, and a clear
margin over the runner-up.
"""
from __future__ import annotations

import argparse
import difflib
import json
import logging
import os
import re
import unicodedata
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from catalog_db import (
    get_unresolved_titles,
    mark_unresolved_title_resolved,
    record_discovery,
)

logger = logging.getLogger(__name__)
TMDB_API_BASE = "https://api.themoviedb.org/3"
IMDB_ID_RE = re.compile(r"^tt\d{7,10}$")
MIN_CONFIDENCE = 0.94
MIN_MARGIN = 0.08


class TMDBConfigurationError(RuntimeError):
    """TMDB credentials are not configured."""


class TMDBAPIError(RuntimeError):
    """TMDB could not return a valid response."""


def _normalize(value: Any) -> str:
    value = unicodedata.normalize("NFKD", str(value or ""))
    value = "".join(char for char in value if not unicodedata.combining(char))
    value = value.casefold().replace("&", " and ")
    return " ".join(re.findall(r"[a-z0-9]+", value))


def _tmdb_get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Call TMDB using either a v4 read token or a v3 API key."""
    token = os.getenv("TMDB_READ_ACCESS_TOKEN", "").strip()
    api_key = os.getenv("TMDB_API_KEY", "").strip()
    if not token and not api_key:
        raise TMDBConfigurationError(
            "Set TMDB_READ_ACCESS_TOKEN or TMDB_API_KEY to enable identity matching"
        )

    query = dict(params or {})
    headers = {"Accept": "application/json", "User-Agent": "GermanScraperStremioX/1.0"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    else:
        query["api_key"] = api_key
    url = f"{TMDB_API_BASE}{path}"
    if query:
        url = f"{url}?{urlencode(query)}"

    request = Request(url, headers=headers)
    try:
        with urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise TMDBAPIError(f"TMDB returned HTTP {exc.code}") from None
    except (URLError, TimeoutError, OSError):
        raise TMDBAPIError("Could not reach TMDB") from None
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise TMDBAPIError("TMDB returned invalid JSON") from None
    if not isinstance(payload, dict):
        raise TMDBAPIError("TMDB returned an unexpected response")
    return payload


def _year_from_result(item: dict[str, Any], media_type: str) -> int | None:
    date_key = "first_air_date" if media_type == "series" else "release_date"
    value = item.get(date_key)
    if isinstance(value, str) and re.match(r"^\d{4}", value):
        return int(value[:4])
    return None


def _title_variants(item: dict[str, Any], media_type: str) -> list[str]:
    keys = ("name", "original_name") if media_type == "series" else ("title", "original_title")
    return [str(item[key]) for key in keys if item.get(key)]


def _candidate_score(
    provider_title: str, provider_year: int | None,
    item: dict[str, Any], media_type: str,
) -> float:
    source = _normalize(provider_title)
    variants = [_normalize(value) for value in _title_variants(item, media_type)]
    variants = [value for value in variants if value]
    if not source or not variants:
        return 0.0
    title_score = max(
        1.0 if source == value else difflib.SequenceMatcher(None, source, value).ratio()
        for value in variants
    )
    if provider_year is None:
        year_score = 0.5
    else:
        result_year = _year_from_result(item, media_type)
        if result_year is None:
            year_score = 0.5
        elif result_year == provider_year:
            year_score = 1.0
        else:
            year_score = max(0.0, 1.0 - abs(result_year - provider_year) / 5.0)
    return round((title_score * 0.88) + (year_score * 0.12), 4)


def find_tmdb_match(
    record: dict[str, Any],
    *,
    search_fn: Callable[..., dict[str, Any]] = _tmdb_get,
) -> dict[str, Any]:
    """Return a scored candidate; does not write to the catalog."""
    media_type = record.get("media_type")
    if media_type not in {"movie", "series"}:
        return {"status": "invalid", "reason": "Unsupported media type"}

    endpoint_type = "tv" if media_type == "series" else "movie"
    params: dict[str, Any] = {
        "query": record.get("provider_title", ""),
        "include_adult": "false",
        "language": "de-DE",
    }
    if record.get("year"):
        params["first_air_date_year" if media_type == "series" else "year"] = record["year"]

    try:
        response = search_fn(f"/search/{endpoint_type}", params)
    except (TMDBAPIError, TMDBConfigurationError) as exc:
        return {"status": "error", "reason": str(exc)}
    except Exception as exc:
        return {"status": "error", "reason": f"Search failed: {type(exc).__name__}"}

    results = response.get("results")
    if not isinstance(results, list) or not results:
        return {"status": "no_match", "reason": "TMDB returned no candidates"}

    scored = [
        ( _candidate_score(record["provider_title"], record.get("year"), item, media_type), item)
        for item in results if isinstance(item, dict)
    ]
    scored.sort(key=lambda pair: pair[0], reverse=True)
    if not scored:
        return {"status": "no_match", "reason": "TMDB returned no usable candidates"}

    best_score, best = scored[0]
    runner_up = scored[1][0] if len(scored) > 1 else 0.0
    margin = round(best_score - runner_up, 4)
    candidate = {
        "tmdb_id": best.get("id"),
        "candidate_title": (best.get("name") if media_type == "series" else best.get("title")) or "",
        "candidate_original_title": (
            best.get("original_name") if media_type == "series" else best.get("original_title")
        ) or "",
        "candidate_year": _year_from_result(best, media_type),
        "score": best_score,
        "margin": margin,
    }
    if best_score < MIN_CONFIDENCE:
        return {"status": "ambiguous", "reason": "Best candidate is below confidence threshold", **candidate}
    if margin < MIN_MARGIN:
        return {"status": "ambiguous", "reason": "Multiple candidates have similar scores", **candidate}
    if not isinstance(best.get("id"), int):
        return {"status": "ambiguous", "reason": "TMDB candidate has no numeric ID", **candidate}

    try:
        external = search_fn(f"/{endpoint_type}/{best['id']}/external_ids", {})
    except Exception as exc:
        return {"status": "error", "reason": f"External ID lookup failed: {type(exc).__name__}", **candidate}
    imdb_id = str(external.get("imdb_id") or "").strip()
    if not IMDB_ID_RE.fullmatch(imdb_id):
        return {"status": "ambiguous", "reason": "TMDB candidate has no valid IMDb ID", **candidate}

    return {"status": "matched", "imdb_id": imdb_id, **candidate}


def resolve_unresolved_titles(
    *,
    provider: str | None = None,
    limit: int = 500,
    apply: bool = False,
    search_fn: Callable[..., dict[str, Any]] = _tmdb_get,
) -> dict[str, Any]:
    """Evaluate queued titles and optionally persist only high-confidence matches."""
    records = get_unresolved_titles(provider=provider, limit=limit)
    summary: dict[str, Any] = {
        "scanned": len(records),
        "matched": 0,
        "applied": 0,
        "ambiguous": 0,
        "no_match": 0,
        "errors": 0,
        "dry_run": not apply,
        "results": [],
    }
    for record in records:
        result = find_tmdb_match(record, search_fn=search_fn)
        result_row = {
            "provider": record["provider"],
            "title": record["provider_title"],
            "url": record["provider_url"],
            **result,
        }
        summary["results"].append(result_row)
        status = result["status"]
        if status == "matched":
            summary["matched"] += 1
            if apply:
                metadata = dict(record.get("metadata") or {})
                metadata["identity_resolution"] = {
                    "source": "tmdb",
                    "tmdb_id": result["tmdb_id"],
                    "score": result["score"],
                    "margin": result["margin"],
                    "candidate_title": result["candidate_title"],
                }
                record_discovery(
                    record["media_type"],
                    result["imdb_id"],
                    record["provider"],
                    record["provider_title"],
                    provider_title=record["provider_title"],
                    provider_url=record["provider_url"],
                    year=record.get("year") or result.get("candidate_year"),
                    metadata=metadata,
                )
                mark_unresolved_title_resolved(
                    record["provider"], record["provider_url"], result["imdb_id"]
                )
                summary["applied"] += 1
        elif status == "ambiguous":
            summary["ambiguous"] += 1
        elif status == "no_match":
            summary["no_match"] += 1
        else:
            summary["errors"] += 1
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Resolve provider titles to IMDb IDs via TMDB")
    parser.add_argument("--provider", choices=("aniworld", "serienstream"))
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument(
        "--apply", action="store_true",
        help="Persist high-confidence matches (default: report only)",
    )
    args = parser.parse_args()
    summary = resolve_unresolved_titles(
        provider=args.provider, limit=args.limit, apply=args.apply
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
