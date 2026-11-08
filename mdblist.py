"""Small, opt-in MDBList static-list exporter for the Stremio add-on.

The exporter deliberately accepts only IMDb IDs that have already been matched
by the scraper. It does not infer availability from Cinemeta/TMDB metadata.
"""
import os
import re
from typing import Dict, Iterable, List

import niquests

API_BASE = "https://api.mdblist.com"
IMDB_ID_RE = re.compile(r"^tt\d{7,10}$")


class MDBListConfigurationError(RuntimeError):
    pass


class MDBListAPIError(RuntimeError):
    pass


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise MDBListConfigurationError(f"Missing required setting: {name}")
    return value


def _list_id(name: str) -> int:
    value = _required_env(name)
    try:
        parsed = int(value)
    except ValueError as exc:
        raise MDBListConfigurationError(f"{name} must be a positive integer") from exc
    if parsed <= 0:
        raise MDBListConfigurationError(f"{name} must be a positive integer")
    return parsed


def _normalize_ids(values: Iterable[str], label: str) -> List[str]:
    result = []
    seen = set()
    for value in values:
        imdb_id = str(value or "").strip()
        if not IMDB_ID_RE.fullmatch(imdb_id):
            raise ValueError(f"Invalid IMDb ID in {label}: {imdb_id!r}")
        if imdb_id not in seen:
            seen.add(imdb_id)
            result.append(imdb_id)
    if len(result) > 50:
        raise ValueError(f"At most 50 {label} IDs may be synced per request")
    return result


def _add_items(api_key: str, list_id: int, media_type: str, imdb_ids: List[str]) -> Dict:
    if not imdb_ids:
        return {"added": {}, "existing": {}, "not_found": {}}

    payload = {
        "movies" if media_type == "movie" else "shows": [
            {"imdb": imdb_id} for imdb_id in imdb_ids
        ]
    }
    try:
        response = niquests.post(
            f"{API_BASE}/lists/{list_id}/items/add",
            params={"apikey": api_key},
            json=payload,
            timeout=20,
        )
    except Exception as exc:
        # Do not propagate request exceptions: they can include the API-key URL.
        raise MDBListAPIError("Could not reach the MDBList API") from exc

    if response.status_code >= 400:
        # Avoid returning the raw body or request URL, either of which may contain
        # sensitive request details.
        raise MDBListAPIError(f"MDBList returned HTTP {response.status_code}")

    try:
        data = response.json()
    except Exception as exc:
        raise MDBListAPIError("MDBList returned an invalid JSON response") from exc

    if not isinstance(data, dict):
        raise MDBListAPIError("MDBList returned an unexpected response")
    return data


def sync_imdb_ids(movie_ids: Iterable[str], show_ids: Iterable[str]) -> Dict:
    """Add already-verified movie/show IMDb IDs to their configured static lists."""
    movies = _normalize_ids(movie_ids, "movie")
    shows = _normalize_ids(show_ids, "show")
    if not movies and not shows:
        raise ValueError("Provide at least one movie or show IMDb ID")

    api_key = _required_env("MDBLIST_API_KEY")
    result = {}
    if movies:
        result["movies"] = _add_items(
            api_key, _list_id("MDBLIST_MOVIE_LIST_ID"), "movie", movies
        )
    if shows:
        result["shows"] = _add_items(
            api_key, _list_id("MDBLIST_SHOW_LIST_ID"), "show", shows
        )
    return {
        "submitted": {"movies": len(movies), "shows": len(shows)},
        "results": result,
    }
