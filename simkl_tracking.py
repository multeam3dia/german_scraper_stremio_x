"""Simkl OAuth V2 and playback scrobbling for German Scraper X.

Only player state changes are sent to Simkl. There is deliberately no heartbeat:
Simkl extrapolates progress between start/pause/stop events.
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
import time
from pathlib import Path
from threading import Lock
from typing import Any, Optional

import niquests

from playback_events import PlaybackEvent
from cryptography.fernet import Fernet, InvalidToken

API_BASE = "https://api.simkl.com"
APP_NAME = "german-scraper-x"
APP_VERSION = "1.4.0"
IMDB_ID_RE = re.compile(r"^tt\d{7,10}$")
REFRESH_TTL_SECONDS = 180 * 24 * 60 * 60
DEFAULT_ACCESS_TTL_SECONDS = 7 * 24 * 60 * 60
logger = logging.getLogger(__name__)
_TOKEN_LOCK = Lock()


class SimklError(RuntimeError):
    """A safe-to-log Simkl integration error."""


def _db_path() -> Path:
    catalog_path = Path(os.getenv("CATALOG_DB_PATH", "/app/data/catalog.sqlite3"))
    return catalog_path.parent / "simkl_tracking.sqlite3"


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path), timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout = 30000")
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS simkl_credentials (
            id INTEGER PRIMARY KEY CHECK(id = 1),
            access_token TEXT NOT NULL,
            refresh_token TEXT NOT NULL,
            access_expires_at INTEGER NOT NULL,
            refresh_expires_at INTEGER NOT NULL,
            scope TEXT NOT NULL DEFAULT '',
            updated_at INTEGER NOT NULL
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS simkl_device_auth (
            id INTEGER PRIMARY KEY CHECK(id = 1),
            device_code TEXT NOT NULL,
            user_code TEXT NOT NULL,
            verification_uri_complete TEXT NOT NULL,
            expires_at INTEGER NOT NULL,
            interval_seconds INTEGER NOT NULL,
            next_poll_at INTEGER NOT NULL
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS simkl_event_state (
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
    """Create the persistent Simkl state database."""
    db = _connect()
    db.close()


def _fernet() -> Fernet:
    configured_key = os.getenv("SIMKL_TOKEN_ENCRYPTION_KEY", "").strip()
    if configured_key:
        try:
            return Fernet(configured_key.encode("ascii"))
        except Exception as exc:
            raise SimklError("SIMKL_TOKEN_ENCRYPTION_KEY is not a valid Fernet key") from exc

    key_path = _db_path().with_name("simkl_tokens.key")
    key_path.parent.mkdir(parents=True, exist_ok=True)
    if key_path.exists():
        key = key_path.read_bytes().strip()
    else:
        key = Fernet.generate_key()
        try:
            descriptor = os.open(str(key_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(key)
        except FileExistsError:
            key = key_path.read_bytes().strip()
    try:
        os.chmod(key_path, 0o600)
        return Fernet(key)
    except Exception as exc:
        raise SimklError("Could not load the local Simkl token encryption key") from exc


def _encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def _decrypt(value: str) -> str:
    try:
        return _fernet().decrypt(value.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeError, ValueError) as exc:
        raise SimklError("Stored Simkl credentials could not be decrypted") from exc


def _client_id() -> str:
    value = os.getenv("SIMKL_CLIENT_ID", "").strip()
    if not value:
        raise SimklError("SIMKL_CLIENT_ID is not configured")
    return value


def _api_post(
    path: str,
    payload: dict[str, Any],
    *,
    access_token: Optional[str] = None,
    json_body: bool = False,
    params: Optional[dict[str, str]] = None,
) -> tuple[int, dict[str, Any]]:
    headers = {
        "User-Agent": f"{APP_NAME}/{APP_VERSION}",
        "Accept": "application/json",
    }
    if json_body:
        headers["Content-Type"] = "application/json"
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    kwargs: dict[str, Any] = {"headers": headers, "timeout": 10}
    if json_body:
        kwargs["json"] = payload
    else:
        kwargs["data"] = payload
    if params:
        kwargs["params"] = params
    try:
        response = niquests.post(f"{API_BASE}{path}", **kwargs)
    except Exception as exc:
        raise SimklError("Could not reach the Simkl API") from exc
    try:
        data = response.json()
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    return int(response.status_code), data


def _save_token_response(data: dict[str, Any]) -> None:
    access_token = str(data.get("access_token") or "").strip()
    refresh_token = str(data.get("refresh_token") or "").strip()
    if not access_token or not refresh_token:
        raise SimklError("Simkl token response did not contain both tokens")
    now = int(time.time())
    access_ttl = int(data.get("expires_in") or DEFAULT_ACCESS_TTL_SECONDS)
    refresh_ttl = int(data.get("refresh_expires_in") or REFRESH_TTL_SECONDS)
    scope = str(data.get("scope") or "")
    db = _connect()
    try:
        db.execute(
            """
            INSERT OR REPLACE INTO simkl_credentials
                (id, access_token, refresh_token, access_expires_at,
                 refresh_expires_at, scope, updated_at)
            VALUES (1, ?, ?, ?, ?, ?, ?)
            """,
            (
                _encrypt(access_token),
                _encrypt(refresh_token),
                now + max(60, access_ttl),
                now + max(60, refresh_ttl),
                scope,
                now,
            ),
        )
        db.commit()
    finally:
        db.close()


def _load_credentials() -> Optional[dict[str, Any]]:
    db = _connect()
    try:
        row = db.execute(
            "SELECT * FROM simkl_credentials WHERE id = 1"
        ).fetchone()
    finally:
        db.close()
    if row is None:
        return None
    try:
        return {
            "access_token": _decrypt(str(row["access_token"])),
            "refresh_token": _decrypt(str(row["refresh_token"])),
            "access_expires_at": int(row["access_expires_at"]),
            "refresh_expires_at": int(row["refresh_expires_at"]),
            "scope": str(row["scope"] or ""),
        }
    except SimklError:
        logger.error("Stored Simkl credentials are unreadable; reconnect Simkl")
        return None


def _clear_credentials() -> None:
    db = _connect()
    try:
        db.execute("DELETE FROM simkl_credentials WHERE id = 1")
        db.commit()
    finally:
        db.close()


def get_simkl_status() -> dict[str, Any]:
    credentials = _load_credentials()
    if not credentials:
        return {"connected": False}
    now = int(time.time())
    if credentials["refresh_expires_at"] <= now:
        _clear_credentials()
        return {"connected": False}
    return {
        "connected": True,
        "scope": credentials["scope"],
        "access_expires_at": credentials["access_expires_at"],
        "access_expires_in": max(0, credentials["access_expires_at"] - now),
    }


def start_device_authorization() -> dict[str, Any]:
    """Start Simkl AUTH V2 device flow and return only user-displayable fields."""
    client_id = _client_id()
    status, data = _api_post(
        "/oauth2/device",
        {"client_id": client_id, "scope": "media:read media:write"},
    )
    if status >= 400 or not data.get("device_code"):
        raise SimklError(
            f"Simkl device authorization failed (HTTP {status}, "
            f"{data.get('error') or 'unexpected response'})"
        )
    now = int(time.time())
    expires_in = max(60, int(data.get("expires_in") or 900))
    interval = max(5, int(data.get("interval") or 5))
    db = _connect()
    try:
        db.execute(
            """
            INSERT OR REPLACE INTO simkl_device_auth
                (id, device_code, user_code, verification_uri_complete,
                 expires_at, interval_seconds, next_poll_at)
            VALUES (1, ?, ?, ?, ?, ?, ?)
            """,
            (
                _encrypt(str(data["device_code"])),
                str(data.get("user_code") or ""),
                str(data.get("verification_uri_complete") or data.get("verification_uri") or "https://simkl.com/pin"),
                now + expires_in,
                interval,
                now + interval,
            ),
        )
        db.commit()
    finally:
        db.close()
    return {
        "status": "pending",
        "user_code": str(data.get("user_code") or ""),
        "verification_uri_complete": str(
            data.get("verification_uri_complete") or data.get("verification_uri") or "https://simkl.com/pin"
        ),
        "expires_in": expires_in,
        "interval": interval,
    }


def poll_device_authorization() -> dict[str, Any]:
    """Poll once; callers must respect the interval returned by Simkl."""
    db = _connect()
    try:
        row = db.execute(
            "SELECT * FROM simkl_device_auth WHERE id = 1"
        ).fetchone()
    finally:
        db.close()
    if row is None:
        return {"status": "not_started"}
    now = int(time.time())
    expires_at = int(row["expires_at"])
    interval = int(row["interval_seconds"])
    if now >= expires_at:
        _clear_device_authorization()
        return {"status": "expired"}
    next_poll_at = int(row["next_poll_at"])
    if now < next_poll_at:
        return {"status": "pending", "retry_after": next_poll_at - now}

    device_code = _decrypt(str(row["device_code"]))
    status, data = _api_post(
        "/oauth2/token",
        {
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "client_id": _client_id(),
            "device_code": device_code,
        },
    )
    if status < 400 and data.get("access_token"):
        _save_token_response(data)
        _clear_device_authorization()
        saved = get_simkl_status()
        if "media:write" not in str(saved.get("scope") or ""):
            return {"status": "connected_readonly"}
        return {"status": "connected"}

    error = str(data.get("error") or "")
    if error == "authorization_pending":
        next_interval = interval
        result = {"status": "pending", "retry_after": next_interval}
    elif error == "slow_down":
        next_interval = interval + 5
        result = {"status": "pending", "retry_after": next_interval}
    elif error == "expired_token":
        _clear_device_authorization()
        return {"status": "expired"}
    else:
        logger.warning("Simkl device authorization failed: %s (HTTP %s)", error or "unknown error", status)
        return {"status": "error", "message": error or f"HTTP {status}"}

    db = _connect()
    try:
        db.execute(
            """
            UPDATE simkl_device_auth
            SET interval_seconds = ?, next_poll_at = ?
            WHERE id = 1
            """,
            (next_interval, now + next_interval),
        )
        db.commit()
    finally:
        db.close()
    return result


def _clear_device_authorization() -> None:
    db = _connect()
    try:
        db.execute("DELETE FROM simkl_device_auth WHERE id = 1")
        db.commit()
    finally:
        db.close()


def _refresh_access_token(force: bool = False) -> Optional[str]:
    with _TOKEN_LOCK:
        credentials = _load_credentials()
        if not credentials:
            return None
        now = int(time.time())
        if not force and credentials["access_expires_at"] > now + 300:
            return credentials["access_token"]
        if credentials["refresh_expires_at"] <= now:
            _clear_credentials()
            return None
        status, data = _api_post(
            "/oauth2/token",
            {
                "grant_type": "refresh_token",
                "client_id": _client_id(),
                "refresh_token": credentials["refresh_token"],
            },
        )
        if status >= 400 or not data.get("access_token"):
            error = str(data.get("error") or "")
            if error in {"invalid_grant", "invalid_client"}:
                _clear_credentials()
            logger.warning("Simkl token refresh failed: %s (HTTP %s)", error or "unknown error", status)
            return None
        _save_token_response(data)
        refreshed = _load_credentials()
        return refreshed["access_token"] if refreshed else None


def _get_access_token(force_refresh: bool = False) -> Optional[str]:
    credentials = _load_credentials()
    if not credentials:
        return None
    if force_refresh or credentials["access_expires_at"] <= int(time.time()) + 300:
        refreshed_token = _refresh_access_token(force=force_refresh)
        refreshed_credentials = _load_credentials()
        if (
            not refreshed_token
            or not refreshed_credentials
            or "media:write" not in refreshed_credentials["scope"]
        ):
            return None
        return refreshed_token
    if "media:write" not in credentials["scope"]:
        return None
    return credentials["access_token"]


def _event_is_duplicate(media_type: str, media_key: str, action: str, now: int) -> bool:
    db = _connect()
    try:
        row = db.execute(
            """
            SELECT last_action, last_event_at FROM simkl_event_state
            WHERE media_type = ? AND video_id = ?
            """,
            (media_type, media_key),
        ).fetchone()
    finally:
        db.close()
    if row is None or str(row["last_action"]) != action:
        return False
    age = now - int(row["last_event_at"])
    # Coalesce duplicate states from any client, including seeks that resend state.
    return age < (6 * 60 * 60 if action in {"start", "pause"} else 60 * 60)


def _record_event(
    media_type: str, media_key: str, action: str, progress: float, now: int
) -> None:
    db = _connect()
    try:
        db.execute(
            """
            INSERT OR REPLACE INTO simkl_event_state
                (media_type, video_id, last_action, last_event_at, last_progress)
            VALUES (?, ?, ?, ?, ?)
            """,
            (media_type, media_key, action, now, progress),
        )
        db.commit()
    finally:
        db.close()


def _build_media_payload(event: PlaybackEvent) -> Optional[dict[str, Any]]:
    """Build a Simkl payload from canonical identity, independent of client format."""
    if event.media_type not in {"movie", "series"}:
        return None

    allowed_ids = {
        "imdb", "tmdb", "tvdb", "trakt", "simkl",
        "anidb", "mal", "anilist", "kitsu",
    }
    ids = {
        str(key).lower(): str(value).strip()
        for key, value in event.ids.items()
        if str(key).lower() in allowed_ids and value is not None and str(value).strip()
    }
    if not ids:
        return None
    imdb_id = ids.get("imdb")
    if imdb_id and not IMDB_ID_RE.fullmatch(imdb_id):
        ids.pop("imdb", None)
        imdb_id = None
    if not ids:
        return None

    season = episode = None
    if event.media_type == "series":
        try:
            season = int(event.season)
            episode = int(event.episode)
        except (TypeError, ValueError):
            return None
        if season < 0 or episode < 1:
            return None

    record: dict[str, Any] = {}
    if imdb_id:
        try:
            from catalog_db import get_title_for_tracking
            record = get_title_for_tracking(event.media_type, imdb_id) or {}
        except Exception:
            record = {}

    item: dict[str, Any] = {"ids": ids}
    title = str(event.title or record.get("title") or "").strip()
    if title:
        item["title"] = title
    year = event.year or record.get("year")
    if year:
        try:
            item["year"] = int(str(year)[:4])
        except (TypeError, ValueError):
            pass

    if event.media_type == "movie":
        return {"movie": item}
    return {
        "show": item,
        "episode": {"season": season, "number": episode},
    }


def scrobble_event(event: PlaybackEvent) -> dict[str, Any]:
    """Send one normalized playback event to Simkl, regardless of its client."""
    if not isinstance(event, PlaybackEvent):
        return {"status": "ignored", "reason": "invalid_event"}
    if event.action not in {"start", "pause", "stop"}:
        return {"status": "ignored", "reason": "unsupported_action"}

    payload = _build_media_payload(event)
    if payload is None:
        return {"status": "ignored", "reason": "unsupported_media_id"}

    progress = event.progress
    if progress is None:
        if event.action != "start":
            return {"status": "ignored", "reason": "duration_unknown"}
        progress = 0.0
    try:
        progress = float(progress)
    except (TypeError, ValueError):
        return {"status": "ignored", "reason": "invalid_progress"}
    if not 0.0 <= progress <= 100.0:
        return {"status": "ignored", "reason": "invalid_progress"}
    payload["progress"] = progress

    now = int(time.time())
    media_key = event.media_key
    if _event_is_duplicate(event.media_type, media_key, event.action, now):
        return {"status": "ignored", "reason": "duplicate_state"}

    token = _get_access_token()
    if not token:
        return {"status": "not_connected"}

    params = {
        "client_id": _client_id(),
        "app-name": APP_NAME,
        "app-version": APP_VERSION,
    }
    status, data = _api_post(
        f"/scrobble/{event.action}",
        payload,
        access_token=token,
        json_body=True,
        params=params,
    )
    if status == 401:
        refreshed = _get_access_token(force_refresh=True)
        if refreshed:
            status, data = _api_post(
                f"/scrobble/{event.action}",
                payload,
                access_token=refreshed,
                json_body=True,
                params=params,
            )
    if status == 409 and event.action == "stop":
        _record_event(event.media_type, media_key, event.action, progress, now)
        return {"status": "duplicate_watch"}
    if status == 400 and str(data.get("error") or data.get("code") or "").upper() == "RATE_LIMIT":
        logger.warning(
            "Simkl rate-limited a %s event from %s for %s",
            event.action, event.source, media_key,
        )
        return {"status": "rate_limited"}
    if status >= 400:
        logger.warning(
            "Simkl scrobble failed for %s from %s (%s): HTTP %s",
            media_key, event.source, event.action, status,
        )
        return {"status": "error", "http_status": status}

    _record_event(event.media_type, media_key, event.action, progress, now)
    return {
        "status": "sent",
        "action": str(data.get("action") or event.action),
        "progress": progress,
    }
