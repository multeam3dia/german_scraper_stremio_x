"""Enrich verified provider catalog entries with canonical TMDB metadata.

Identity matching is performed separately. This module only looks up metadata
for IMDb IDs that are already present in the verified SQLite catalog.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
from typing import Any, Callable

from catalog_db import (
    get_metadata_candidates,
    init_db,
    update_title_metadata,
)
from identity_resolver import (
    TMDBAPIError,
    TMDBConfigurationError,
    _tmdb_get,
)

logger = logging.getLogger(__name__)
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p"


def _year_from_item(item: dict[str, Any], media_type: str) -> int | None:
    date_key = "first_air_date" if media_type == "series" else "release_date"
    value = item.get(date_key)
    if isinstance(value, str) and re.match(r"^\d{4}", value):
        return int(value[:4])
    return None


def _image_url(path: Any, size: str) -> str | None:
    if not isinstance(path, str) or not path.startswith("/"):
        return None
    return f"{TMDB_IMAGE_BASE}/{size}{path}"


def _fetch_genre_map(
    media_type: str,
    *,
    search_fn: Callable[..., dict[str, Any]],
) -> dict[int, str]:
    endpoint = "/genre/tv/list" if media_type == "series" else "/genre/movie/list"
    payload = search_fn(endpoint, {"language": "de-DE"})
    genres = payload.get("genres")
    if not isinstance(genres, list):
        raise TMDBAPIError("TMDB returned an unexpected genre response")
    result: dict[int, str] = {}
    for item in genres:
        if not isinstance(item, dict):
            continue
        try:
            genre_id = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        name = str(item.get("name") or "").strip()
        if name:
            result[genre_id] = name
    return result


def find_tmdb_metadata(
    record: dict[str, Any],
    genre_map: dict[int, str],
    *,
    search_fn: Callable[..., dict[str, Any]] = _tmdb_get,
) -> dict[str, Any]:
    """Look up localized TMDB metadata using an already-verified IMDb ID."""
    media_type = record.get("media_type")
    if media_type not in {"movie", "series"}:
        return {"status": "invalid"}
    imdb_id = str(record.get("imdb_id") or "").strip()
    if not re.fullmatch(r"tt\d{7,10}", imdb_id):
        return {"status": "invalid"}

    payload = search_fn(
        f"/find/{imdb_id}",
        {"external_source": "imdb_id", "language": "de-DE"},
    )
    result_key = "tv_results" if media_type == "series" else "movie_results"
    results = payload.get(result_key)
    if not isinstance(results, list) or not results:
        return {"status": "no_match"}

    item = next(
        (
            candidate for candidate in results
            if isinstance(candidate, dict) and isinstance(candidate.get("id"), int)
        ),
        None,
    )
    if item is None:
        return {"status": "no_match"}

    raw_genres = item.get("genre_ids") or []
    genres = []
    for genre_id in raw_genres:
        try:
            name = genre_map.get(int(genre_id))
        except (TypeError, ValueError):
            name = None
        if name and name not in genres:
            genres.append(name)

    title = (
        item.get("name") if media_type == "series" else item.get("title")
    ) or record.get("title")
    original_title = (
        item.get("original_name") if media_type == "series"
        else item.get("original_title")
    )
    tmdb_metadata = {
        "tmdb_id": item["id"],
        "title": str(title) if title else None,
        "original_title": str(original_title) if original_title else None,
        "year": _year_from_item(item, media_type),
        "poster_url": _image_url(item.get("poster_path"), "w500"),
        "backdrop_url": _image_url(item.get("backdrop_path"), "w1280"),
        "overview": str(item.get("overview")) if item.get("overview") else None,
        "vote_average": item.get("vote_average"),
        "original_language": item.get("original_language"),
    }
    return {
        "status": "matched",
        "metadata": tmdb_metadata,
        "genres": genres,
    }


def enrich_catalog_metadata(
    *,
    limit: int = 500,
    force: bool = False,
    search_fn: Callable[..., dict[str, Any]] = _tmdb_get,
) -> dict[str, Any]:
    """Enrich active catalog titles; never performs identity matching."""
    init_db()
    records = get_metadata_candidates(limit=limit, force=force)
    genre_maps: dict[str, dict[int, str]] = {}
    summary: dict[str, Any] = {
        "scanned": len(records),
        "enriched": 0,
        "no_match": 0,
        "errors": 0,
        "results": [],
    }

    for record in records:
        media_type = record["media_type"]
        imdb_id = record["imdb_id"]
        try:
            if media_type not in genre_maps:
                genre_maps[media_type] = _fetch_genre_map(
                    media_type, search_fn=search_fn
                )
            result = find_tmdb_metadata(
                record, genre_maps[media_type], search_fn=search_fn
            )
            if result["status"] == "matched":
                updated = update_title_metadata(
                    media_type,
                    imdb_id,
                    {"tmdb": result["metadata"]},
                    genres=result["genres"],
                )
                if updated:
                    summary["enriched"] += 1
                    summary["results"].append({
                        "media_type": media_type,
                        "imdb_id": imdb_id,
                        "title": result["metadata"].get("title") or record["title"],
                        "genres": result["genres"],
                    })
                else:
                    summary["errors"] += 1
            elif result["status"] == "no_match":
                summary["no_match"] += 1
            else:
                summary["errors"] += 1
        except (TMDBAPIError, TMDBConfigurationError) as exc:
            summary["errors"] += 1
            logger.warning(
                "TMDB metadata enrichment failed for %s %s: %s",
                media_type, imdb_id, str(exc),
            )
        except Exception as exc:
            summary["errors"] += 1
            logger.warning(
                "Unexpected metadata enrichment error for %s %s: %s",
                media_type, imdb_id, type(exc).__name__,
            )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Enrich verified SQLite catalog titles with TMDB metadata"
    )
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument(
        "--force", action="store_true",
        help="Refresh titles that already have TMDB metadata",
    )
    args = parser.parse_args()
    summary = enrich_catalog_metadata(limit=args.limit, force=args.force)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
