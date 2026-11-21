"""Enrich verified SQLite catalog titles with optional MDBList metadata.

MDBList is queried only by an already verified IMDb ID. It never performs
identity matching and never overwrites TMDB metadata or canonical genres.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import niquests

from catalog_db import get_metadata_candidates, init_db, update_title_metadata

API_BASE = os.getenv("MDBLIST_API_BASE", "https://api.mdblist.com").rstrip("/")
IMDB_ID_RE = re.compile(r"^tt\d{7,10}$")
logger = logging.getLogger(__name__)


class MDBListEnrichmentError(RuntimeError):
    pass


class MDBListNotFound(MDBListEnrichmentError):
    pass


def _mdblist_get(path: str, params: dict[str, Any]) -> Any:
    api_key = os.getenv("MDBLIST_API_KEY", "").strip()
    if not api_key:
        raise MDBListEnrichmentError("MDBLIST_API_KEY is not configured")
    try:
        response = niquests.get(
            f"{API_BASE}{path}",
            params={**params, "apikey": api_key},
            timeout=20,
        )
    except Exception:
        # Request exceptions can contain the API-key URL. Never expose it.
        raise MDBListEnrichmentError("Could not reach MDBList") from None
    if response.status_code == 404:
        raise MDBListNotFound("MDBList has no record for this IMDb ID")
    if response.status_code >= 400:
        raise MDBListEnrichmentError(
            f"MDBList returned HTTP {response.status_code}"
        )
    try:
        return response.json()
    except Exception:
        raise MDBListEnrichmentError("MDBList returned invalid JSON") from None


def _unwrap_item(payload: Any) -> dict[str, Any] | None:
    if isinstance(payload, list):
        return next((item for item in payload if isinstance(item, dict)), None)
    if not isinstance(payload, dict):
        return None
    for key in ("data", "result", "item", "movie", "show"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            payload = nested
            break
        if isinstance(nested, list):
            return next((item for item in nested if isinstance(item, dict)), None)
    return payload if any(
        key in payload for key in ("title", "name", "ids", "imdb_id", "imdb")
    ) else None


def _as_text(value: Any) -> str | None:
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return None


def _as_image(value: Any) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        for key in ("url", "full", "large", "medium"):
            if isinstance(value.get(key), str) and value[key].strip():
                return value[key].strip()
    return None


def _as_year(item: dict[str, Any]) -> int | None:
    value = item.get("year") or item.get("release_year")
    if not value:
        for key in ("released", "release_date", "first_aired"):
            value = item.get(key)
            if value:
                break
    match = re.search(r"\b(?:19|20)\d{2}\b", str(value or ""))
    return int(match.group(0)) if match else None


def _rating_value(value: Any) -> float | None:
    if isinstance(value, dict):
        value = value.get("rating") or value.get("value") or value.get("score")
    if isinstance(value, str):
        match = re.search(r"\d+(?:\.\d+)?", value)
        value = match.group(0) if match else None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if 0 <= parsed <= 10 else None


def _imdb_rating(item: dict[str, Any]) -> float | None:
    direct = _rating_value(item.get("imdb_rating") or item.get("imdbrating"))
    if direct is not None:
        return direct
    ratings = item.get("ratings")
    if isinstance(ratings, dict):
        for key in ("imdb", "IMDb", "imdb_rating"):
            if key in ratings:
                direct = _rating_value(ratings[key])
                if direct is not None:
                    return direct
    elif isinstance(ratings, list):
        for rating in ratings:
            if not isinstance(rating, dict):
                continue
            source = str(
                rating.get("source") or rating.get("name") or rating.get("provider") or ""
            ).casefold()
            if "imdb" in source:
                direct = _rating_value(rating)
                if direct is not None:
                    return direct
    return None


def find_mdblist_metadata(
    record: dict[str, Any],
    *,
    fetch_fn: Callable[[str, dict[str, Any]], Any] = _mdblist_get,
) -> dict[str, Any]:
    """Fetch supplemental metadata for a title with a confirmed IMDb identity."""
    media_type = record.get("media_type")
    imdb_id = str(record.get("imdb_id") or "").strip()
    if media_type not in {"movie", "series"} or not IMDB_ID_RE.fullmatch(imdb_id):
        return {"status": "invalid"}

    endpoint_type = "show" if media_type == "series" else "movie"
    payload = fetch_fn(f"/imdb/{endpoint_type}/{imdb_id}/", {})
    item = _unwrap_item(payload)
    if item is None:
        return {"status": "no_match"}

    ids = item.get("ids") if isinstance(item.get("ids"), dict) else {}
    returned_id = (
        ids.get("imdb") or ids.get("imdbid") or ids.get("imdb_id")
        or item.get("imdb_id") or item.get("imdbid") or item.get("imdb")
    )
    if returned_id and str(returned_id).strip() != imdb_id:
        # Exact-ID endpoints should never silently return a different identity.
        raise MDBListEnrichmentError("MDBList returned a different IMDb identity")

    metadata: dict[str, Any] = {
        "status": "found",
        "title": _as_text(item.get("title") or item.get("name")),
        "year": _as_year(item),
        "description": _as_text(
            item.get("description") or item.get("overview") or item.get("plot")
        ),
        "runtime": item.get("runtime"),
        "score": item.get("score"),
        "ratings": item.get("ratings"),
        "imdb_rating": _imdb_rating(item),
        "streams": item.get("streams"),
        "watch_providers": item.get("watch_providers"),
        "language": item.get("language"),
        "spoken_language": item.get("spoken_language"),
        "country": item.get("country"),
        "status_label": item.get("status"),
        "trailer": item.get("trailer"),
        "poster_url": _as_image(item.get("poster") or item.get("poster_url")),
        "backdrop_url": _as_image(
            item.get("backdrop") or item.get("backdrop_url") or item.get("background")
        ),
        "genres": item.get("genres"),
        "production_companies": item.get("production_companies"),
        "certification": item.get("certification"),
        # MDBList's show response includes season summaries and sometimes the
        # latest/next episode, not a complete episode catalogue.
        "seasons": item.get("seasons"),
        "last_episode": item.get("last_episode"),
        "next_episode": item.get("next_episode"),
    }
    return {key: value for key, value in metadata.items() if value is not None}


def _is_cache_fresh(metadata: Any, cache_days: int | None = None) -> bool:
    """Return whether a stored MDBList result should suppress another API call.

    By default, successful lookups and explicit no-match results are permanent.
    A cache lifetime is applied only when the operator explicitly supplies
    --cache-days. Records without a timestamp remain reusable in permanent mode.
    """
    if not isinstance(metadata, dict) or metadata.get("status") not in {
        "found", "no_match"
    }:
        return False
    if cache_days is None:
        return True

    fetched_at = metadata.get("fetched_at")
    if not isinstance(fetched_at, str):
        return False
    try:
        fetched = datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
        if fetched.tzinfo is None:
            fetched = fetched.replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    return datetime.now(timezone.utc) - fetched < timedelta(days=max(1, cache_days))


def enrich_mdblist_metadata(
    *,
    limit: int = 500,
    force: bool = False,
    cache_days: int | None = None,
    fetch_fn: Callable[[str, dict[str, Any]], Any] = _mdblist_get,
) -> dict[str, Any]:
    """Enrich active, verified catalog entries; never performs title matching."""
    if not os.getenv("MDBLIST_API_KEY", "").strip() and fetch_fn is _mdblist_get:
        return {
            "scanned": 0, "enriched": 0, "no_match": 0,
            "skipped_cached": 0, "errors": 0, "disabled": True,
        }

    init_db()
    try:
        max_enrichments = max(1, min(50000, int(limit)))
    except (TypeError, ValueError):
        max_enrichments = 500
    # Scan the full verified catalogue so cached records at the beginning do
    # not hide older, not-yet-enriched titles beyond --limit.
    records = get_metadata_candidates(limit=50000, force=True)
    summary: dict[str, Any] = {
        "scanned": 0,
        "enriched": 0,
        "no_match": 0,
        "skipped_cached": 0,
        "errors": 0,
        "results": [],
    }
    for record in records:
        current = record.get("metadata", {}).get("mdblist")
        if not force and _is_cache_fresh(current, cache_days):
            summary["skipped_cached"] += 1
            continue
        if summary["scanned"] >= max_enrichments:
            break
        summary["scanned"] += 1
        try:
            result = find_mdblist_metadata(record, fetch_fn=fetch_fn)
            result["fetched_at"] = datetime.now(timezone.utc).isoformat()
            if result["status"] == "found":
                if update_title_metadata(
                    record["media_type"], record["imdb_id"], {"mdblist": result}
                ):
                    summary["enriched"] += 1
                    summary["results"].append({
                        "media_type": record["media_type"],
                        "imdb_id": record["imdb_id"],
                        "title": result.get("title") or record["title"],
                    })
                else:
                    summary["errors"] += 1
            elif result["status"] == "no_match":
                update_title_metadata(
                    record["media_type"], record["imdb_id"], {"mdblist": result}
                )
                summary["no_match"] += 1
            else:
                summary["errors"] += 1
        except MDBListNotFound:
            update_title_metadata(
                record["media_type"],
                record["imdb_id"],
                {"mdblist": {
                    "status": "no_match",
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                }},
            )
            summary["no_match"] += 1
        except MDBListEnrichmentError as exc:
            summary["errors"] += 1
            logger.warning(
                "MDBList metadata enrichment failed for %s %s: %s",
                record["media_type"], record["imdb_id"], str(exc),
            )
        except Exception as exc:
            summary["errors"] += 1
            logger.warning(
                "Unexpected MDBList metadata enrichment error for %s %s: %s",
                record["media_type"], record["imdb_id"], type(exc).__name__,
            )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Enrich verified SQLite catalog titles with MDBList metadata"
    )
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--cache-days",
        type=int,
        default=None,
        help="Temporarily treat cached results as stale after this many days; "
             "by default, found and no-match results are kept indefinitely",
    )
    args = parser.parse_args()
    print(json.dumps(
        enrich_mdblist_metadata(
            limit=args.limit, force=args.force, cache_days=args.cache_days
        ),
        ensure_ascii=False,
        indent=2,
    ))


if __name__ == "__main__":
    main()
