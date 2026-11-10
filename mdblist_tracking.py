"""MDBList playback scrobbling and cross-device playback progress retrieval.

Uses the existing MDBLIST_API_KEY setting. No separate OAuth registration is
required for this single-user/self-hosted integration. Player events are sent
only on start, pause and stop; there is deliberately no heartbeat.
"""
from __future__ import annotations

import logging
import math
import os
import re
import sqlite3
import time
from pathlib import Path
from threading import Lock
from typing import Any, Mapping, Optional

import niquests

from playback_events import PlaybackEvent

API_BASE = "https://api.mdblist.com"
APP_NAME = "german-scraper-x"
APP_VERSION = "1.4.0"
IMDB_ID_RE = re.compile(r"^tt\d{7,10}$")
logger = logging.getLogger(__name__)
_DB_LOCK = Lock()

SUPPORTED_IDS = {"imdb", "tmdb", "trakt", "mdblist", "kitsu", "tvdb"}


class MDBListTrackingError(RuntimeError):
    """A safe-to-log MDBList tracking error."""


def _db_path() -> Path:
    catalog_path = Path(os.getenv("CATALOG_DB_PATH", "/app/data/catalog.sqlite3"))
    return catalog_path.parent / "mdblist_tracking.sqlite3"


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path), timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout = 30000")
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS mdblist_event_state (
            media_type TEXT NOT NULL,
            video_id TEXT NOT NULL,
            last_action TEXT NOT NULL,
            last_event_at INTEGER NOT NULL,
            last_progress REAL,
            PRIMARY KEY(media_type, video_id)
        )
        """
    )
    return db


def init_db() -> None:
    """Create persistent event-deduplication state."""
    db = _connect()
    db.close()


def _api_key() -> Optional[str]:
    value = os.getenv("MDBLIST_API_KEY", "").strip()
    return value or None


def _rate_limit_info(headers: Any) -> dict[str, Optional[int]]:
    result: dict[str, Optional[int]] = {}
    for source, target in (
        ("X-RateLimit-Limit", "limit"),
        ("X-RateLimit-Remaining", "remaining"),
        ("X-RateLimit-Reset", "reset_at"),
        ("Retry-After", "retry_after"),
    ):
        raw = None
        try:
            raw = headers.get(source)
        except Exception:
            raw = None
        try:
            result[target] = int(raw) if raw is not None else None
        except (TypeError, ValueError):
            result[target] = None
    return result


def _api_request(
    method: str,
    path: str,
    *,
    payload: Optional[dict[str, Any]] = None,
) -> tuple[int, Any, Any]:
    api_key = _api_key()
    if not api_key:
        raise MDBListTrackingError("MDBLIST_API_KEY is not configured")

    headers = {
        "User-Agent": f"{APP_NAME}/{APP_VERSION}",
        "Accept": "application/json",
    }
    kwargs: dict[str, Any] = {
        "params": {"apikey": api_key},
        "headers": headers,
        "timeout": 15,
    }
    try:
        if method.upper() == "GET":
            response = niquests.get(f"{API_BASE}{path}", **kwargs)
        else:
            headers["Content-Type"] = "application/json"
            kwargs["json"] = payload or {}
            response = niquests.post(f"{API_BASE}{path}", **kwargs)
    except Exception as exc:
        # Do not expose the API-key query string in logs or returned errors.
        raise MDBListTrackingError("Could not reach the MDBList API") from exc

    try:
        data = response.json()
    except Exception:
        data = {}
    return int(response.status_code), data, response.headers


def get_mdblist_status() -> dict[str, Any]:
    """Verify the configured API key and report quota headers without user data."""
    if not _api_key():
        return {"configured": False, "connected": False, "status": "not_configured"}
    try:
        status, _data, headers = _api_request("GET", "/user")
    except MDBListTrackingError:
        return {
            "configured": True,
            "connected": False,
            "status": "error",
            "reason": "api_unreachable",
        }

    result: dict[str, Any] = {
        "configured": True,
        "connected": status < 400,
        "http_status": status,
        "rate_limit": _rate_limit_info(headers),
    }
    result["status"] = "connected" if status < 400 else "error"
    if status == 401 or status == 403:
        result["reason"] = "invalid_api_key_or_forbidden"
    elif status == 429:
        result["reason"] = "rate_limited"
    elif status >= 400:
        result["reason"] = "api_error"
    return result


def _playback_items(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if not isinstance(data, dict):
        return []
    for key in ("items", "playback", "results", "data"):
        value = data.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]

    # Be tolerant of a split movie/episode response shape.
    combined: list[dict[str, Any]] = []
    for key in ("movies", "episodes", "shows"):
        value = data.get(key)
        if isinstance(value, list):
            combined.extend(
                {**item, "_media_group": key}
                for item in value
                if isinstance(item, dict)
            )
    return combined


def get_mdblist_playback() -> dict[str, Any]:
    """Retrieve paused scrobble sessions for a Continue Watching view."""
    if not _api_key():
        return {
            "status": "not_configured",
            "configured": False,
            "count": 0,
            "items": [],
        }
    try:
        status, data, headers = _api_request("GET", "/sync/playback")
    except MDBListTrackingError:
        return {"status": "error", "reason": "api_unreachable", "items": []}

    if status >= 400:
        return {
            "status": "rate_limited" if status == 429 else "error",
            "http_status": status,
            "rate_limit": _rate_limit_info(headers),
            "items": [],
        }

    items = _playback_items(data)
    return {
        "status": "ok",
        "count": len(items),
        "items": items,
        "rate_limit": _rate_limit_info(headers),
        # Keep the original response available during initial API-shape testing.
        "response": data if not items and isinstance(data, dict) else None,
    }


def _build_payload(event: PlaybackEvent, progress: float) -> Optional[dict[str, Any]]:
    if event.media_type not in {"movie", "series"}:
        return None

    ids = {
        str(key).lower(): str(value).strip()
        for key, value in event.ids.items()
        if str(key).lower() in SUPPORTED_IDS
        and value is not None
        and str(value).strip()
    }
    imdb_id = ids.get("imdb")
    if imdb_id and not IMDB_ID_RE.fullmatch(imdb_id):
        ids.pop("imdb", None)
    if not ids:
        return None

    if event.media_type == "movie":
        item: dict[str, Any] = {"movie": {"ids": ids}}
    else:
        try:
            season = int(event.season)
            episode = int(event.episode)
        except (TypeError, ValueError):
            return None
        if season < 0 or episode < 1:
            return None
        item = {
            "show": {
                "ids": ids,
                "season": {
                    "number": season,
                    "episode": {"number": episode},
                },
            }
        }
    item["progress"] = progress
    return item


def _event_is_duplicate(media_type: str, media_key: str, action: str, now: int) -> bool:
    db = _connect()
    try:
        row = db.execute(
            """
            SELECT last_action, last_event_at FROM mdblist_event_state
            WHERE media_type = ? AND video_id = ?
            """,
            (media_type, media_key),
        ).fetchone()
    finally:
        db.close()
    if row is None or str(row["last_action"]) != action:
        return False
    age = now - int(row["last_event_at"])
    return age < (6 * 60 * 60 if action in {"start", "pause"} else 60 * 60)


def _record_event(
    media_type: str,
    media_key: str,
    action: str,
    progress: float,
    now: int,
) -> None:
    db = _connect()
    try:
        db.execute(
            """
            INSERT OR REPLACE INTO mdblist_event_state
                (media_type, video_id, last_action, last_event_at, last_progress)
            VALUES (?, ?, ?, ?, ?)
            """,
            (media_type, media_key, action, now, progress),
        )
        db.commit()
    finally:
        db.close()


def scrobble_event(event: PlaybackEvent) -> dict[str, Any]:
    """Forward a normalized player state to MDBList."""
    if not isinstance(event, PlaybackEvent):
        return {"status": "ignored", "reason": "invalid_event"}
    if event.action not in {"start", "pause", "stop"}:
        return {"status": "ignored", "reason": "unsupported_action"}
    if not _api_key():
        return {"status": "not_configured"}

    progress = event.progress
    if progress is None:
        if event.action == "start":
            progress = 0.0
        else:
            return {"status": "ignored", "reason": "duration_unknown"}
    try:
        progress = float(progress)
    except (TypeError, ValueError):
        return {"status": "ignored", "reason": "invalid_progress"}
    if not math.isfinite(progress) or not 0.0 <= progress <= 100.0:
        return {"status": "ignored", "reason": "invalid_progress"}

    payload = _build_payload(event, progress)
    if payload is None:
        return {"status": "ignored", "reason": "unsupported_media_id"}

    now = int(time.time())
    media_key = event.media_key
    with _DB_LOCK:
        if _event_is_duplicate(event.media_type, media_key, event.action, now):
            return {"status": "ignored", "reason": "duplicate_state"}

    try:
        status, data, headers = _api_request(
            "POST", f"/scrobble/{event.action}", payload=payload
        )
    except MDBListTrackingError:
        logger.warning(
            "MDBList scrobble could not reach API for %s (%s)",
            media_key,
            event.action,
        )
        return {"status": "error", "reason": "api_unreachable"}

    if status == 409 and event.action == "stop":
        with _DB_LOCK:
            _record_event(event.media_type, media_key, event.action, progress, now)
        return {
            "status": "duplicate_watch",
            "http_status": status,
            "action": event.action,
            "progress": progress,
        }
    if status == 429:
        return {
            "status": "rate_limited",
            "http_status": status,
            "retry_after": _rate_limit_info(headers).get("retry_after"),
        }
    if status >= 400:
        logger.warning(
            "MDBList scrobble failed for %s (%s): HTTP %s",
            media_key,
            event.action,
            status,
        )
        return {"status": "error", "http_status": status}

    with _DB_LOCK:
        _record_event(event.media_type, media_key, event.action, progress, now)
    return {
        "status": "sent",
        "http_status": status,
        "action": event.action,
        "progress": progress,
        "rate_limit": _rate_limit_info(headers),
    }
