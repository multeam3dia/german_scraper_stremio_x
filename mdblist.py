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
        list_name = (
            "MDBLIST_MOVIE_LIST_ID" if media_type == "movie"
            else "MDBLIST_SHOW_LIST_ID"
        )
        result = _add_item(api_key, _list_id(list_name), media_type, imdb_id)
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

    required = ("MDBLIST_API_KEY",)
    list_name = (
        "MDBLIST_MOVIE_LIST_ID" if media_type == "movie"
        else "MDBLIST_SHOW_LIST_ID"
    )
    if not all(os.getenv(name, "").strip() for name in (*required, list_name)):
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
