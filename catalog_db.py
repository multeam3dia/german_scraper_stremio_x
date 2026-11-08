"""Persistent catalog index for provider discovery and availability tracking.

SQLite is the source of truth for what our providers have actually exposed.
MDBList is only a downstream catalog/metadata integration.
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

IMDB_ID_RE = __import__("re").compile(r"^tt\d{7,10}$")
MEDIA_TYPES = {"movie", "series"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _db_path() -> Path:
    return Path(os.getenv("CATALOG_DB_PATH", "/app/data/catalog.sqlite3"))


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path), timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def init_db() -> None:
    """Create or migrate the small, versioned catalog schema."""
    with _connect() as db:
        db.execute("PRAGMA journal_mode = WAL")
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_version (
                version INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS titles (
                media_type TEXT NOT NULL CHECK(media_type IN ('movie', 'series')),
                imdb_id TEXT NOT NULL,
                title TEXT NOT NULL,
                year INTEGER,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                last_verified_at TEXT,
                catalog_active INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY (media_type, imdb_id)
            );

            CREATE TABLE IF NOT EXISTS provider_entries (
                media_type TEXT NOT NULL,
                imdb_id TEXT NOT NULL,
                provider TEXT NOT NULL,
                provider_title TEXT,
                provider_url TEXT,
                availability_status TEXT NOT NULL DEFAULT 'available'
                    CHECK(availability_status IN ('available', 'missing', 'unknown')),
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                last_checked_at TEXT,
                consecutive_misses INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                PRIMARY KEY (media_type, imdb_id, provider),
                FOREIGN KEY (media_type, imdb_id)
                    REFERENCES titles(media_type, imdb_id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_provider_entries_status
                ON provider_entries(provider, availability_status, last_checked_at);

            CREATE TABLE IF NOT EXISTS crawl_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider TEXT NOT NULL,
                media_type TEXT NOT NULL CHECK(media_type IN ('movie', 'series')),
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL DEFAULT 'running'
                    CHECK(status IN ('running', 'completed', 'partial', 'failed')),
                pages_scanned INTEGER NOT NULL DEFAULT 0,
                titles_found INTEGER NOT NULL DEFAULT 0,
                error TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_crawl_runs_provider_started
                ON crawl_runs(provider, started_at DESC);

            CREATE TABLE IF NOT EXISTS sync_outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                media_type TEXT NOT NULL CHECK(media_type IN ('movie', 'series')),
                imdb_id TEXT NOT NULL,
                action TEXT NOT NULL CHECK(action IN ('add', 'remove')),
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending', 'processing', 'done', 'retry')),
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at TEXT,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(media_type, imdb_id, action, status)
            );

            CREATE INDEX IF NOT EXISTS idx_sync_outbox_pending
                ON sync_outbox(status, next_attempt_at, id);
            """
        )
        row = db.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
        if row is None:
            db.execute("INSERT INTO schema_version(version) VALUES (1)")
        elif row["version"] > 1:
            raise RuntimeError("Catalog database schema is newer than this application")


def record_discovery(
    media_type: str,
    imdb_id: str,
    provider: str,
    title: str,
    *,
    provider_title: Optional[str] = None,
    provider_url: Optional[str] = None,
    year: Optional[int] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> None:
    """Record a positively discovered title/provider pair.

    Call this only after a provider detail page has been resolved and the IMDb
    identity is sufficiently certain. A discovery automatically queues an MDBList add.
    """
    if media_type not in MEDIA_TYPES:
        raise ValueError("media_type must be 'movie' or 'series'")
    imdb_id = str(imdb_id or "").strip()
    if not IMDB_ID_RE.fullmatch(imdb_id):
        raise ValueError("A valid IMDb ID is required")
    provider = str(provider or "").strip().lower()
    title = str(title or "").strip()
    if not provider or not title:
        raise ValueError("provider and title are required")

    now = _utc_now()
    metadata_json = json.dumps(metadata or {}, ensure_ascii=False)
    with _connect() as db:
        db.execute(
            """
            INSERT INTO titles(
                media_type, imdb_id, title, year, metadata_json,
                first_seen_at, last_seen_at, last_verified_at, catalog_active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(media_type, imdb_id) DO UPDATE SET
                title=excluded.title,
                year=COALESCE(excluded.year, titles.year),
                metadata_json=CASE WHEN excluded.metadata_json='{}'
                    THEN titles.metadata_json ELSE excluded.metadata_json END,
                last_seen_at=excluded.last_seen_at,
                last_verified_at=excluded.last_verified_at,
                catalog_active=1
            """,
            (media_type, imdb_id, title, year, metadata_json, now, now, now),
        )
        db.execute(
            """
            INSERT INTO provider_entries(
                media_type, imdb_id, provider, provider_title, provider_url,
                availability_status, first_seen_at, last_seen_at,
                last_checked_at, consecutive_misses, last_error
            ) VALUES (?, ?, ?, ?, ?, 'available', ?, ?, ?, 0, NULL)
            ON CONFLICT(media_type, imdb_id, provider) DO UPDATE SET
                provider_title=COALESCE(excluded.provider_title, provider_entries.provider_title),
                provider_url=COALESCE(excluded.provider_url, provider_entries.provider_url),
                availability_status='available',
                last_seen_at=excluded.last_seen_at,
                last_checked_at=excluded.last_checked_at,
                consecutive_misses=0,
                last_error=NULL
            """,
            (media_type, imdb_id, provider, provider_title or title,
             provider_url, now, now, now),
        )
        db.execute(
            """
            INSERT OR IGNORE INTO sync_outbox(
                media_type, imdb_id, action, status, created_at, updated_at
            ) VALUES (?, ?, 'add', 'pending', ?, ?)
            """,
            (media_type, imdb_id, now, now),
        )


def start_crawl_run(provider: str, media_type: str) -> int:
    if media_type not in MEDIA_TYPES:
        raise ValueError("Unsupported media type")
    with _connect() as db:
        cursor = db.execute(
            "INSERT INTO crawl_runs(provider, media_type, started_at) VALUES (?, ?, ?)",
            (provider.strip().lower(), media_type, _utc_now()),
        )
        return int(cursor.lastrowid)


def finish_crawl_run(
    run_id: int,
    *,
    status: str,
    pages_scanned: int = 0,
    titles_found: int = 0,
    error: Optional[str] = None,
) -> None:
    if status not in {"completed", "partial", "failed"}:
        raise ValueError("Invalid crawl status")
    with _connect() as db:
        db.execute(
            """
            UPDATE crawl_runs
            SET finished_at=?, status=?, pages_scanned=?, titles_found=?, error=?
            WHERE id=?
            """,
            (_utc_now(), status, max(0, pages_scanned), max(0, titles_found),
             (error or "")[:1000] or None, run_id),
        )


def get_catalog_stats() -> dict[str, int]:
    """Small operational summary, useful for diagnostics and future admin UI."""
    with _connect() as db:
        titles = db.execute(
            "SELECT COUNT(*) FROM titles WHERE catalog_active=1"
        ).fetchone()[0]
        providers = db.execute(
            "SELECT COUNT(DISTINCT provider) FROM provider_entries "
            "WHERE availability_status='available'"
        ).fetchone()[0]
        pending = db.execute(
            "SELECT COUNT(*) FROM sync_outbox WHERE status IN ('pending', 'retry')"
        ).fetchone()[0]
    return {"active_titles": int(titles), "active_providers": int(providers),
            "pending_mdb_sync": int(pending)}
