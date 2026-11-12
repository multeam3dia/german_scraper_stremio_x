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
    init_db,
    get_unresolved_titles,
    mark_unresolved_title_resolved,
    record_discovery,
)

logger = logging.getLogger(__name__)
TMDB_API_BASE = "https://api.themoviedb.org/3"
IMDB_ID_RE = re.compile(r"^tt\d{7,10}$")
MIN_CONFIDENCE = 0.94
MIN_MARGIN = 0.08
MIN_TITLE_SCORE_WITH_YEAR = 0.94


class TMDBConfigurationError(RuntimeError):
    """TMDB credentials are not configured."""


class TMDBAPIError(RuntimeError):
    """TMDB could not return a valid response."""


def _normalize(value: Any) -> str:
    value = unicodedata.normalize("NFKD", str(value or "").casefold())
    value = "".join(char for char in value if not unicodedata.combining(char))
    value = value.replace("&", " and ")
    return " ".join(re.findall(r"[a-z0-9]+", value))


def _coerce_year(value: Any) -> int | None:
    match = re.search(r"\b(?:19|20)\d{2}\b", str(value or ""))
    return int(match.group(0)) if match else None


def _year_from_result(item: dict[str, Any], media_type: str) -> int | None:
    date_key = "first_air_date" if media_type == "series" else "release_date"
    return _coerce_year(item.get(date_key))


def _title_variants(item: dict[str, Any], media_type: str) -> list[str]:
    keys = ("name", "original_name") if media_type == "series" else ("title", "original_title")
    return [str(item[key]) for key in keys if item.get(key)]


def _title_similarity(
    provider_title: str, item: dict[str, Any], media_type: str
) -> float:
    source = _normalize(provider_title)
    variants = [_normalize(value) for value in _title_variants(item, media_type)]
    variants = [value for value in variants if value]
    if not source or not variants:
        return 0.0
    return round(max(
        1.0 if source == value else difflib.SequenceMatcher(None, source, value).ratio()
        for value in variants
    ), 4)


def _candidate_score(
    provider_title: str, provider_year: int | None,
    item: dict[str, Any], media_type: str,
) -> float:
    title_score = _title_similarity(provider_title, item, media_type)
    if title_score <= 0:
        return 0.0
    provider_year = _coerce_year(provider_year)
    result_year = _year_from_result(item, media_type)
    if provider_year is None or result_year is None:
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
    """Return a conservative TMDB candidate; does not write to the catalog."""
    media_type = record.get("media_type")
    if media_type not in {"movie", "series"}:
        return {"status": "invalid", "reason": "Unsupported media type"}

    provider_title = str(record.get("provider_title") or "").strip()
    if not provider_title:
        return {"status": "no_match", "reason": "Provider title is empty"}
    provider_year = _coerce_year(record.get("year"))

    endpoint_type = "tv" if media_type == "series" else "movie"
    # Search by title only. Filtering at the API level can hide remakes or
    # nearby premiere years before the resolver has a chance to compare them.
    params: dict[str, Any] = {
        "query": provider_title,
        "include_adult": "false",
        "language": "de-DE",
    }

    try:
        response = search_fn(f"/search/{endpoint_type}", params)
    except (TMDBAPIError, TMDBConfigurationError) as exc:
        return {"status": "error", "reason": str(exc)}
    except Exception as exc:
        return {"status": "error", "reason": f"Search failed: {type(exc).__name__}"}

    results = response.get("results")
    if not isinstance(results, list) or not results:
        return {"status": "no_match", "reason": "TMDB returned no candidates"}

    exact_year: list[tuple[float, dict[str, Any], float, int | None]] = []
    near_year: list[tuple[float, dict[str, Any], float, int | None]] = []
    missing_year: list[tuple[float, dict[str, Any], float, int | None]] = []
    exact_title_without_year: list[tuple[float, dict[str, Any], float, int | None]] = []
    saw_title_candidate = False
    saw_year_mismatch = False

    for item in results:
        if not isinstance(item, dict):
            continue
        title_score = _title_similarity(provider_title, item, media_type)
        if title_score < MIN_TITLE_SCORE_WITH_YEAR:
            continue
        saw_title_candidate = True
        candidate_year = _year_from_result(item, media_type)
        score = _candidate_score(provider_title, provider_year, item, media_type)
        row = (score, item, title_score, candidate_year)

        if provider_year is None:
            # Without a provider year, only an exact normalized title can be
            # auto-matched. Similar titles need independent date evidence.
            if title_score == 1.0:
                exact_title_without_year.append(row)
        elif candidate_year is None:
            missing_year.append(row)
        elif candidate_year == provider_year:
            exact_year.append(row)
        elif abs(candidate_year - provider_year) == 1:
            # A one-year difference can be a regional premiere-date variation,
            # but is not strong enough evidence for automatic identity assignment.
            near_year.append(row)
        else:
            saw_year_mismatch = True

    # Prefer an exact year. Near-year or yearless results are retained for
    # manual review, never promoted automatically.
    if provider_year is None:
        candidates = exact_title_without_year
        review_reason = None
    elif exact_year:
        candidates = exact_year
        review_reason = None
    elif near_year:
        candidates = near_year
        review_reason = "Candidate year differs by one year; manual review required"
    elif missing_year:
        candidates = missing_year
        review_reason = "TMDB candidate has no release year; manual review required"
    else:
        candidates = []
        review_reason = None

    if not candidates:
        if provider_year is None and saw_title_candidate:
            reason = "Provider year is missing and no exact title candidate was found"
        elif saw_year_mismatch:
            reason = "No title candidate has a compatible release year"
        else:
            reason = "TMDB returned no sufficiently strong title candidate"
        return {"status": "no_match", "reason": reason}

    candidates.sort(key=lambda pair: pair[0], reverse=True)
    best_score, best, title_score, candidate_year = candidates[0]
    runner_up = candidates[1][0] if len(candidates) > 1 else 0.0
    margin = round(best_score - runner_up, 4)
    candidate = {
        "tmdb_id": best.get("id"),
        "candidate_title": (best.get("name") if media_type == "series" else best.get("title")) or "",
        "candidate_original_title": (
            best.get("original_name") if media_type == "series" else best.get("original_title")
        ) or "",
        "candidate_year": candidate_year,
        "candidate_year_delta": (
            abs(candidate_year - provider_year)
            if candidate_year is not None and provider_year is not None else None
        ),
        "title_score": title_score,
        "score": best_score,
        "margin": margin,
    }
    if review_reason:
        return {"status": "ambiguous", "reason": review_reason, **candidate}
    if best_score < MIN_CONFIDENCE:
        return {"status": "ambiguous", "reason": "Best candidate is below confidence threshold", **candidate}
    if margin < MIN_MARGIN:
        return {"status": "ambiguous", "reason": "Multiple candidates have similar scores", **candidate}
    if type(best.get("id")) is not int or best["id"] <= 0:
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
    init_db()
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
                    "candidate_year": result.get("candidate_year"),
                    "candidate_year_delta": result.get("candidate_year_delta"),
                    "title_score": result.get("title_score"),
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
