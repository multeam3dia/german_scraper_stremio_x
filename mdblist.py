"""Background exporter for titles with a confirmed scraper stream."""
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from typing import Dict, List

import niquests

API_BASE = "https://api.mdblist.com"
IMDB_ID_RE = re.compile(r"^tt\d{7,10}$")
logger = logging.getLogger(__name__)

_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mdblist")
_LOCK = Lock()
_PENDING = set()
_SYNCED = set()
_LIST_ID_CACHE = {}


class MDBListConfigurationError(RuntimeError):
    pass


class MDBListAPIError(RuntimeError):
    pass


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise MDBListConfigurationError(f"Missing required setting: {name}")
    return value


def _resolve_list_id(api_key: str, media_type: str) -> int:
    """Resolve a configured numeric list ID or exact user-list name."""
    id_env = "MDBLIST_MOVIE_LIST_ID" if media_type == "movie" else "MDBLIST_SHOW_LIST_ID"
    name_env = "MDBLIST_MOVIE_LIST_NAME" if media_type == "movie" else "MDBLIST_SHOW_LIST_NAME"
    configured_id = os.getenv(id_env, "").strip()
    if configured_id:
        try:
            parsed = int(configured_id)
        except ValueError as exc:
            raise MDBListConfigurationError(f"{id_env} must be a positive integer") from exc
        if parsed <= 0:
            raise MDBListConfigurationError(f"{id_env} must be a positive integer")
        return parsed

    list_name = _required_env(name_env)
    cache_key = (media_type, list_name)
    with _LOCK:
        cached = _LIST_ID_CACHE.get(cache_key)
    if cached:
        return cached

    try:
        response = niquests.get(
            f"{API_BASE}/lists/user",
            params={"apikey": api_key},
            timeout=15,
        )
    except Exception as exc:
        raise MDBListAPIError("Could not retrieve MDBList user lists") from exc
    if response.status_code >= 400:
        raise MDBListAPIError(f"MDBList returned HTTP {response.status_code}")
    try:
        lists = response.json()
    except Exception as exc:
        raise MDBListAPIError("MDBList returned invalid list JSON") from exc
    if isinstance(lists, dict):
        lists = lists.get("lists")
    if not isinstance(lists, list):
        raise MDBListAPIError("MDBList returned an unexpected list response")

    # The /lists/user response does not consistently expose a mediatype
    # field for every list. The configured movie/show list names are distinct,
    # so resolve by exact name first and use mediatype only to disambiguate
    # duplicate names when MDBList provides it.
    matches = [
        item for item in lists
        if isinstance(item, dict)
        and str(item.get("name", "")).strip() == list_name
    ]
    if len(matches) > 1:
        expected_type = "movie" if media_type == "movie" else "show"
        typed_matches = [
            item for item in matches
            if str(item.get("mediatype", "")).strip().lower() == expected_type
        ]
        if typed_matches:
            matches = typed_matches
    if len(matches) != 1:
        raise MDBListConfigurationError(
            f"Expected exactly one list named {list_name!r}; found {len(matches)}"
        )
    try:
        list_id = int(matches[0]["id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise MDBListAPIError("MDBList list response did not contain a valid ID") from exc
    if list_id <= 0:
        raise MDBListAPIError("MDBList returned an invalid list ID")
    with _LOCK:
        _LIST_ID_CACHE[cache_key] = list_id
    return list_id


def _add_item(api_key: str, list_id: int, media_type: str, imdb_id: str) -> Dict:
    payload = {
        "movies" if media_type == "movie" else "shows": [{"imdb": imdb_id}]
    }
    try:
        response = niquests.post(
            f"{API_BASE}/lists/{list_id}/items/add",
            params={"apikey": api_key},
            json=payload,
            timeout=20,
        )
    except Exception as exc:
        # Request exceptions can contain the API-key URL. Never log or re-raise it.
        raise MDBListAPIError("Could not reach the MDBList API") from exc

    if response.status_code >= 400:
        raise MDBListAPIError(f"MDBList returned HTTP {response.status_code}")

    try:
        data = response.json()
    except Exception as exc:
        raise MDBListAPIError("MDBList returned invalid JSON") from exc

    if not isinstance(data, dict):
        raise MDBListAPIError("MDBList returned an unexpected response")
    return data


def _sync_worker(media_type: str, imdb_id: str) -> None:
    key = (media_type, imdb_id)
    try:
        api_key = _required_env("MDBLIST_API_KEY")
        list_id = _resolve_list_id(api_key, media_type)
        result = _add_item(api_key, list_id, media_type, imdb_id)
        not_found = result.get("not_found") or {}
        not_found_count = sum(
            value for value in not_found.values() if isinstance(value, int)
        ) if isinstance(not_found, dict) else 0
        if not_found_count:
            logger.warning(
                "MDBList did not recognize %s ID %s; it was not marked as synced",
                media_type, imdb_id,
            )
            return
        with _LOCK:
            _SYNCED.add(key)
        logger.info("MDBList catalog updated for %s %s", media_type, imdb_id)
    except MDBListConfigurationError as exc:
        logger.warning("MDBList export is disabled or misconfigured: %s", exc)
    except MDBListAPIError as exc:
        logger.warning("MDBList export failed for %s %s: %s", media_type, imdb_id, exc)
    except Exception as exc:
        logger.warning(
            "Unexpected MDBList export error for %s %s: %s",
            media_type, imdb_id, type(exc).__name__,
        )
    finally:
        with _LOCK:
            _PENDING.discard(key)


def queue_verified_title(media_type: str, imdb_id: str) -> bool:
    """Queue a title only after a scraper request returned at least one stream.

    Returns False when the optional integration is not configured or the ID is
    invalid. Duplicate/in-flight items are ignored. API work runs off the
    Stremio response path.
    """
    if media_type not in {"movie", "series"}:
        return False
    imdb_id = str(imdb_id or "").strip()
    if not IMDB_ID_RE.fullmatch(imdb_id):
        return False

    list_id_name = "MDBLIST_MOVIE_LIST_ID" if media_type == "movie" else "MDBLIST_SHOW_LIST_ID"
    list_name_name = "MDBLIST_MOVIE_LIST_NAME" if media_type == "movie" else "MDBLIST_SHOW_LIST_NAME"
    if not os.getenv("MDBLIST_API_KEY", "").strip():
        return False
    if not (os.getenv(list_id_name, "").strip() or os.getenv(list_name_name, "").strip()):
        return False

    key = (media_type, imdb_id)
    with _LOCK:
        if key in _SYNCED or key in _PENDING:
            return False
        _PENDING.add(key)

    try:
        _EXECUTOR.submit(_sync_worker, media_type, imdb_id)
    except Exception:
        with _LOCK:
            _PENDING.discard(key)
        logger.warning("Could not queue MDBList export for %s %s", media_type, imdb_id)
        return False
    return True


# --- Stremio catalog reader -------------------------------------------------

import time

_CATALOG_CACHE = {}
_CATALOG_CACHE_TTL = 300
_CATALOG_PAGE_SIZE = 100


def _extract_list_items(payload):
    """Accept the list-shaped responses used by MDBList API versions."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("items", "results", "movies", "shows"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    raise MDBListAPIError("MDBList returned an unexpected catalog response")


def _item_to_stremio_meta(item: Dict, media_type: str):
    if not isinstance(item, dict):
        return None
    ids = item.get("ids") if isinstance(item.get("ids"), dict) else {}
    imdb_id = (
        ids.get("imdb")
        or item.get("imdb_id")
        or item.get("imdbid")
        or item.get("imdb")
    )
    if not isinstance(imdb_id, str) or not IMDB_ID_RE.fullmatch(imdb_id.strip()):
        return None
    imdb_id = imdb_id.strip()

    title = item.get("title") or item.get("name")
    if not title:
        return None

    poster = item.get("poster") or item.get("poster_url")
    if isinstance(poster, dict):
        poster = poster.get("url") or poster.get("full")
    background = item.get("backdrop") or item.get("backdrop_url") or item.get("background")
    if isinstance(background, dict):
        background = background.get("url") or background.get("full")

    description = item.get("description") or item.get("overview") or item.get("plot")
    year = item.get("year")
    if not year:
        released = item.get("released") or item.get("release_date") or item.get("first_aired")
        if isinstance(released, str) and len(released) >= 4 and released[:4].isdigit():
            year = released[:4]

    genres = item.get("genres") or []
    if not isinstance(genres, list):
        genres = []
    genres = [
        str(g.get("name") if isinstance(g, dict) else g)
        for g in genres if g
    ]

    ratings = item.get("ratings") if isinstance(item.get("ratings"), dict) else {}
    imdb_rating = item.get("imdb_rating") or item.get("imdbrating")
    imdb_data = ratings.get("imdb") if isinstance(ratings.get("imdb"), dict) else {}
    imdb_rating = imdb_rating or imdb_data.get("rating")

    meta = {
        "id": imdb_id,
        "type": media_type,
        "name": str(title),
    }
    if poster:
        meta["poster"] = poster
    if background:
        meta["background"] = background
    if description:
        meta["description"] = str(description)
    if year:
        meta["releaseInfo"] = str(year)[:4]
    if genres:
        meta["genres"] = genres
    try:
        if imdb_rating is not None:
            meta["imdbRating"] = str(round(float(imdb_rating), 1))
    except (TypeError, ValueError):
        pass
    return meta


def get_catalog_metas(media_type: str, skip: int = 0, search: str = "") -> List[Dict]:
    """Read one page from the configured MDBList static list for Stremio."""
    if media_type not in {"movie", "series"}:
        return []
    api_key = _required_env("MDBLIST_API_KEY")
    list_id = _resolve_list_id(api_key, media_type)
    offset = max(0, int(skip))
    search = (search or "").strip()
    cache_key = (media_type, list_id, offset, search.casefold())
    now = time.monotonic()
    with _LOCK:
        cached = _CATALOG_CACHE.get(cache_key)
        if cached and now - cached[0] < _CATALOG_CACHE_TTL:
            return list(cached[1])

    try:
        response = niquests.get(
            f"{API_BASE}/lists/{list_id}/items/",
            params={
                "apikey": api_key,
                "limit": _CATALOG_PAGE_SIZE,
                "offset": offset,
                "append_to_response": "genres,poster,description,ratings",
            },
            timeout=20,
        )
    except Exception as exc:
        raise MDBListAPIError("Could not retrieve MDBList catalog items") from exc
    if response.status_code >= 400:
        raise MDBListAPIError(f"MDBList returned HTTP {response.status_code}")
    try:
        payload = response.json()
    except Exception as exc:
        raise MDBListAPIError("MDBList returned invalid catalog JSON") from exc

    items = _extract_list_items(payload)
    metas = []
    rejected = 0
    for item in items:
        meta = _item_to_stremio_meta(item, media_type)
        if not meta:
            rejected += 1
            continue
        if search and search.casefold() not in meta["name"].casefold():
            continue
        metas.append(meta)

    logger.info(
        "MDBList catalog fetched: type=%s list_id=%s items=%d metas=%d rejected=%d response_type=%s",
        media_type, list_id, len(items), len(metas), rejected, type(payload).__name__,
    )

    with _LOCK:
        _CATALOG_CACHE[cache_key] = (now, metas)
    return metas
