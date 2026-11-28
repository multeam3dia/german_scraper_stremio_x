"""Additive canonical identity store for titles without requiring IMDb IDs.

This module deliberately lives beside the legacy IMDb-keyed catalog. It creates
only new tables in the existing catalog SQLite file and never rewrites legacy
rows or tracking databases.
"""
from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

MEDIA_TYPES = {"movie", "series"}
GSX_ID_RE = re.compile(r"^gsx:(movie|series):[0-9a-f]{32}$")
IMDB_ID_RE = re.compile(r"^tt\d{7,10}$")
EXTERNAL_ID_NAMESPACES = {
    "imdb", "tmdb", "mal", "tvdb", "trakt", "wikidata", "mdblist"
}


class IdentityConflictError(ValueError):
    """An external identifier is already attached to another canonical title."""


def _db_path() -> Path:
    return Path(os.getenv("CATALOG_DB_PATH", "/app/data/catalog.sqlite3"))


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path), timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=30000")
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def init_identity_schema() -> None:
    """Create the identity tables without modifying any legacy catalog table."""
    with _connect() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS canonical_titles (
                title_id TEXT PRIMARY KEY,
                media_type TEXT NOT NULL CHECK(media_type IN ('movie', 'series')),
                title TEXT NOT NULL,
                year INTEGER,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );

            CREATE TABLE IF NOT EXISTS external_title_ids (
                media_type TEXT NOT NULL CHECK(media_type IN ('movie', 'series')),
                namespace TEXT NOT NULL,
                external_id TEXT NOT NULL,
                title_id TEXT NOT NULL,
                source TEXT,
                confidence REAL,
                verified INTEGER NOT NULL DEFAULT 0 CHECK(verified IN (0,1)),
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                PRIMARY KEY(media_type, namespace, external_id),
                FOREIGN KEY(title_id) REFERENCES canonical_titles(title_id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_external_title_ids_title
                ON external_title_ids(title_id, namespace);

            CREATE TABLE IF NOT EXISTS canonical_provider_links (
                provider TEXT NOT NULL,
                provider_url TEXT NOT NULL,
                title_id TEXT NOT NULL,
                provider_title TEXT,
                availability_status TEXT NOT NULL DEFAULT 'available'
                    CHECK(availability_status IN ('available','missing','unknown')),
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                PRIMARY KEY(provider, provider_url),
                FOREIGN KEY(title_id) REFERENCES canonical_titles(title_id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_canonical_provider_links_title
                ON canonical_provider_links(title_id, provider);
            """
        )


def _validate_media_type(media_type: str) -> str:
    value = str(media_type or "").strip().lower()
    if value not in MEDIA_TYPES:
        raise ValueError("media_type must be 'movie' or 'series'")
    return value


def _validate_title_id(title_id: str) -> str:
    value = str(title_id or "").strip().lower()
    if not GSX_ID_RE.fullmatch(value):
        raise ValueError("Invalid GSX title ID")
    return value


def create_title(
    media_type: str,
    title: str,
    *,
    year: int | None = None,
    metadata: dict[str, Any] | None = None,
    title_id: str | None = None,
) -> str:
    """Create a stable GSX identity. No IMDb ID is required."""
    media_type = _validate_media_type(media_type)
    title = str(title or "").strip()
    if not title:
        raise ValueError("title is required")
    if year is not None and (isinstance(year, bool) or not isinstance(year, int)):
        raise ValueError("year must be an integer or None")
    if year is not None and not -(1 << 63) <= year <= (1 << 63) - 1:
        raise ValueError("year must fit SQLite INTEGER")
    if metadata is not None and not isinstance(metadata, dict):
        raise ValueError("metadata must be a dictionary or None")
    if title_id is None:
        title_id = f"gsx:{media_type}:{uuid.uuid4().hex}"
    title_id = _validate_title_id(title_id)
    if title_id.split(":")[1] != media_type:
        raise ValueError("title ID media type does not match media_type")
    metadata_json = json.dumps(metadata or {}, ensure_ascii=False)
    init_identity_schema()
    with _connect() as db:
        db.execute(
            """INSERT INTO canonical_titles(title_id,media_type,title,year,metadata_json)
               VALUES(?,?,?,?,?)""",
            (title_id, media_type, title, year, metadata_json),
        )
    return title_id


def add_external_id(
    title_id: str,
    namespace: str,
    external_id: str,
    *,
    source: str | None = None,
    confidence: float | None = None,
    verified: bool = False,
) -> None:
    """Attach an external ID; conflicting mappings are rejected, never overwritten.
    
    IMDb IDs are globally unique across canonical media types.
    """
    title_id = _validate_title_id(title_id)
    namespace = str(namespace or "").strip().lower()
    external_id = str(external_id or "").strip()
    if namespace not in EXTERNAL_ID_NAMESPACES:
        raise ValueError("Unsupported external ID namespace")
    if not external_id:
        raise ValueError("external_id is required")
    if namespace == "imdb" and not IMDB_ID_RE.fullmatch(external_id):
        raise ValueError("Invalid IMDb external ID")
    if confidence is not None:
        if isinstance(confidence, bool):
            raise ValueError("confidence must be between 0 and 1")
        try:
            confidence_value = float(confidence)
        except (TypeError, ValueError, OverflowError):
            raise ValueError("confidence must be between 0 and 1") from None
        if not math.isfinite(confidence_value) or not 0 <= confidence_value <= 1:
            raise ValueError("confidence must be between 0 and 1")
        confidence = confidence_value
    if not isinstance(verified, bool):
        raise ValueError("verified must be a boolean")
    init_identity_schema()
    with _connect() as db:
        # IMDb IDs identify a title globally, not within a media-type namespace.
        # Serialize the check-and-insert to prevent concurrent cross-type mappings.
        db.execute("BEGIN IMMEDIATE")
        if namespace == "imdb":
            owner = db.execute(
                """SELECT title_id FROM external_title_ids
                   WHERE namespace=? AND external_id=?""",
                (namespace, external_id),
            ).fetchone()
        else:
            owner = db.execute(
                """SELECT title_id FROM external_title_ids
                   WHERE media_type=(SELECT media_type FROM canonical_titles WHERE title_id=?)
                     AND namespace=? AND external_id=?""",
                (title_id, namespace, external_id),
            ).fetchone()
        if owner and owner["title_id"] != title_id:
            raise IdentityConflictError(
                f"{namespace}:{external_id} is already linked to another title"
            )
        cursor = db.execute(
            """INSERT INTO external_title_ids(
                   media_type,namespace,external_id,title_id,source,confidence,verified
               )
               SELECT media_type,?,?,?,?,?,? FROM canonical_titles WHERE title_id=?
               ON CONFLICT(media_type,namespace,external_id) DO UPDATE SET
                   source=COALESCE(excluded.source,external_title_ids.source),
                   confidence=COALESCE(excluded.confidence,external_title_ids.confidence),
                   verified=MAX(external_title_ids.verified,excluded.verified)""",
            (namespace, external_id, title_id, source, confidence,
             int(bool(verified)), title_id),
        )
        if cursor.rowcount != 1:
            raise ValueError("Unknown GSX title ID")


def get_title(title_id: str) -> dict[str, Any] | None:
    title_id = _validate_title_id(title_id)
    init_identity_schema()
    with _connect() as db:
        row = db.execute(
            "SELECT * FROM canonical_titles WHERE title_id=?", (title_id,)
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["metadata"] = json.loads(result.pop("metadata_json") or "{}")
        result["external_ids"] = {}
        for item in db.execute(
            "SELECT namespace,external_id,source,confidence,verified "
            "FROM external_title_ids WHERE title_id=? ORDER BY namespace",
            (title_id,),
        ):
            result["external_ids"][item["namespace"]] = {
                "id": item["external_id"], "source": item["source"],
                "confidence": item["confidence"], "verified": bool(item["verified"]),
            }
        return result


def get_title_by_external_id(
    media_type: str, namespace: str, external_id: str
) -> dict[str, Any] | None:
    media_type = _validate_media_type(media_type)
    namespace = str(namespace or "").strip().lower()
    external_id = str(external_id or "").strip()
    if namespace not in EXTERNAL_ID_NAMESPACES or not external_id:
        return None
    init_identity_schema()
    with _connect() as db:
        row = db.execute(
            """SELECT title_id FROM external_title_ids
               WHERE media_type=? AND namespace=? AND external_id=?""",
            (media_type, namespace, external_id),
        ).fetchone()
    return get_title(str(row["title_id"])) if row else None


def link_provider_title(
    title_id: str,
    provider: str,
    provider_url: str,
    *,
    provider_title: str | None = None,
    availability_status: str = "available",
) -> None:
    """Link one provider detail page to a canonical GSX title."""
    title_id = _validate_title_id(title_id)
    provider = str(provider or "").strip().lower()
    provider_url = str(provider_url or "").strip()
    status = str(availability_status or "").strip().lower()
    if not provider or not provider_url:
        raise ValueError("provider and provider_url are required")
    if status not in {"available", "missing", "unknown"}:
        raise ValueError("invalid availability_status")
    init_identity_schema()
    with _connect() as db:
        cursor = db.execute(
            """INSERT INTO canonical_provider_links(
                   provider,provider_url,title_id,provider_title,availability_status
               ) VALUES(?,?,?,?,?)
               ON CONFLICT(provider,provider_url) DO UPDATE SET
                   provider_title=COALESCE(excluded.provider_title,canonical_provider_links.provider_title),
                   availability_status=excluded.availability_status,
                   updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
               WHERE canonical_provider_links.title_id=excluded.title_id""",
            (provider, provider_url, title_id, provider_title, status),
        )
        if cursor.rowcount != 1:
            raise IdentityConflictError("Provider URL is already linked to another title")


def get_title_by_provider_url(provider: str, provider_url: str) -> dict[str, Any] | None:
    provider = str(provider or "").strip().lower()
    provider_url = str(provider_url or "").strip()
    if not provider or not provider_url:
        return None
    init_identity_schema()
    with _connect() as db:
        row = db.execute(
            """SELECT title_id FROM canonical_provider_links
               WHERE provider=? AND provider_url=?""",
            (provider, provider_url),
        ).fetchone()
    return get_title(str(row["title_id"])) if row else None


def get_provider_links(title_id: str) -> list[dict[str, Any]]:
    """Return available provider detail-page links for a canonical title."""
    title_id = _validate_title_id(title_id)
    init_identity_schema()
    with _connect() as db:
        rows = db.execute(
            """SELECT provider,provider_url,provider_title,availability_status
               FROM canonical_provider_links
               WHERE title_id=? AND availability_status='available'
               ORDER BY provider""",
            (title_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def get_unmatched_catalog_media_types() -> list[str]:
    """List media types with available canonical titles that have no IMDb ID."""
    init_identity_schema()
    with _connect() as db:
        rows = db.execute(
            """SELECT DISTINCT t.media_type
               FROM canonical_titles t
               JOIN canonical_provider_links p ON p.title_id=t.title_id
                    AND p.availability_status='available'
               WHERE NOT EXISTS (
                   SELECT 1 FROM external_title_ids x
                   WHERE x.title_id=t.title_id AND x.namespace='imdb'
               )
               ORDER BY t.media_type"""
        ).fetchall()
    return [str(row["media_type"]) for row in rows]


def get_unmatched_catalog_titles(
    media_type: str, *, skip: int = 0, limit: int = 100, search: str = ""
) -> list[dict[str, Any]]:
    """Return available canonical titles that cannot be represented by IMDb IDs."""
    media_type = _validate_media_type(media_type)
    skip = max(0, int(skip))
    limit = min(500, max(1, int(limit)))
    search = str(search or "").strip()
    params: list[Any] = [media_type]
    search_sql = ""
    if search:
        search_sql = " AND lower(t.title) LIKE ?"
        params.append(f"%{search.casefold()}%")
    params.extend([limit, skip])
    init_identity_schema()
    with _connect() as db:
        rows = db.execute(
            """SELECT t.* FROM canonical_titles t
               WHERE t.media_type=?
                 AND NOT EXISTS (
                   SELECT 1 FROM external_title_ids x
                   WHERE x.title_id=t.title_id AND x.namespace='imdb'
                 )
                 AND EXISTS (
                   SELECT 1 FROM canonical_provider_links p
                   WHERE p.title_id=t.title_id AND p.availability_status='available'
                 )"""
            + search_sql
            + " ORDER BY t.title COLLATE NOCASE, t.year DESC LIMIT ? OFFSET ?",
            tuple(params),
        ).fetchall()
    results = []
    for row in rows:
        item = dict(row)
        item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
        results.append(item)
    return results