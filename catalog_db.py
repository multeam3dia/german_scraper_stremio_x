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

            CREATE TABLE IF NOT EXISTS unresolved_provider_titles (
                provider TEXT NOT NULL,
                provider_url TEXT NOT NULL,
                provider_title TEXT NOT NULL,
                normalized_title TEXT NOT NULL,
                media_type TEXT NOT NULL CHECK(media_type IN ('movie', 'series')),
                year INTEGER,
                poster_url TEXT,
                description TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                last_attempt_at TEXT,
                last_error TEXT,
                resolved_imdb_id TEXT,
                resolved_at TEXT,
                PRIMARY KEY (provider, provider_url)
            );

            CREATE INDEX IF NOT EXISTS idx_unresolved_titles_pending
                ON unresolved_provider_titles(resolved_imdb_id, provider, last_seen_at);

            CREATE TABLE IF NOT EXISTS genres (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slug TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL UNIQUE
            );

            CREATE TABLE IF NOT EXISTS title_genres (
                media_type TEXT NOT NULL,
                imdb_id TEXT NOT NULL,
                genre_id INTEGER NOT NULL,
                PRIMARY KEY (media_type, imdb_id, genre_id),
                FOREIGN KEY (media_type, imdb_id)
                    REFERENCES titles(media_type, imdb_id) ON DELETE CASCADE,
                FOREIGN KEY (genre_id)
                    REFERENCES genres(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_title_genres_genre
                ON title_genres(genre_id, media_type, imdb_id);

            CREATE TABLE IF NOT EXISTS provider_seasons (
                media_type TEXT NOT NULL CHECK(media_type IN ('movie', 'series')),
                imdb_id TEXT NOT NULL,
                provider TEXT NOT NULL,
                season_number INTEGER NOT NULL,
                provider_url TEXT NOT NULL,
                is_movie_collection INTEGER NOT NULL DEFAULT 0,
                last_seen_at TEXT NOT NULL,
                PRIMARY KEY (media_type, imdb_id, provider, season_number),
                FOREIGN KEY (media_type, imdb_id, provider)
                    REFERENCES provider_entries(media_type, imdb_id, provider) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS provider_episodes (
                media_type TEXT NOT NULL CHECK(media_type IN ('movie', 'series')),
                imdb_id TEXT NOT NULL,
                provider TEXT NOT NULL,
                season_number INTEGER NOT NULL,
                episode_number INTEGER NOT NULL,
                provider_url TEXT NOT NULL,
                title TEXT,
                title_original TEXT,
                is_movie INTEGER NOT NULL DEFAULT 0,
                last_seen_at TEXT NOT NULL,
                PRIMARY KEY (media_type, imdb_id, provider, season_number, episode_number),
                FOREIGN KEY (media_type, imdb_id, provider, season_number)
                    REFERENCES provider_seasons(media_type, imdb_id, provider, season_number)
                    ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS episode_sources (
                media_type TEXT NOT NULL CHECK(media_type IN ('movie', 'series')),
                imdb_id TEXT NOT NULL,
                provider TEXT NOT NULL,
                season_number INTEGER NOT NULL,
                episode_number INTEGER NOT NULL,
                audio_language TEXT NOT NULL,
                subtitle_language TEXT NOT NULL DEFAULT '',
                hoster TEXT NOT NULL DEFAULT '',
                source_url TEXT,
                last_seen_at TEXT NOT NULL,
                PRIMARY KEY (
                    media_type, imdb_id, provider, season_number, episode_number,
                    audio_language, subtitle_language, hoster
                ),
                FOREIGN KEY (media_type, imdb_id, provider, season_number, episode_number)
                    REFERENCES provider_episodes(
                        media_type, imdb_id, provider, season_number, episode_number
                    ) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_provider_episodes_lookup
                ON provider_episodes(media_type, imdb_id, provider, season_number);

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
            db.execute("INSERT INTO schema_version(version) VALUES (4)")
        elif row["version"] < 4:
            db.execute("UPDATE schema_version SET version = 4")
        elif row["version"] > 4:
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
    genres: Optional[list[str]] = None,
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
    with _connect() as db:
        existing = db.execute(
            "SELECT metadata_json FROM titles WHERE media_type=? AND imdb_id=?",
            (media_type, imdb_id),
        ).fetchone()
        merged_metadata = {}
        if existing:
            try:
                stored = json.loads(existing["metadata_json"] or "{}")
                if isinstance(stored, dict):
                    merged_metadata.update(stored)
            except (TypeError, json.JSONDecodeError):
                pass
        for key, value in (metadata or {}).items():
            if value is not None:
                merged_metadata[key] = value
        metadata_json = json.dumps(merged_metadata, ensure_ascii=False)
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
        # When genre data is supplied, replace the previous assignments.
        # None means "metadata not refreshed"; an empty list means "no genres".
        if genres is not None:
            db.execute(
                "DELETE FROM title_genres WHERE media_type = ? AND imdb_id = ?",
                (media_type, imdb_id),
            )
        # Genres are normalized independently from provider-specific labels.
        for genre in genres or []:
            name = str(genre or "").strip()
            if not name:
                continue
            slug = __import__("re").sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")
            if not slug:
                continue
            db.execute(
                "INSERT OR IGNORE INTO genres(slug, name) VALUES (?, ?)",
                (slug, name),
            )
            genre_row = db.execute(
                "SELECT id FROM genres WHERE slug = ?", (slug,)
            ).fetchone()
            db.execute(
                """
                INSERT OR IGNORE INTO title_genres(media_type, imdb_id, genre_id)
                VALUES (?, ?, ?)
                """,
                (media_type, imdb_id, int(genre_row["id"])),
            )

        db.execute(
            """
            INSERT OR IGNORE INTO sync_outbox(
                media_type, imdb_id, action, status, created_at, updated_at
            ) VALUES (?, ?, 'add', 'pending', ?, ?)
            """,
            (media_type, imdb_id, now, now),
        )



def record_unresolved_title(
    provider: str,
    provider_url: str,
    title: str,
    media_type: str = "series",
    *,
    year: Optional[int] = None,
    poster_url: Optional[str] = None,
    description: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
    error: Optional[str] = None,
) -> None:
    """Persist a provider title that could not yet be matched to an IMDb ID.

    This is deliberately separate from the public catalog: unresolved titles
    are retained for review/retry but are never exposed as verified entries.
    """
    if media_type not in MEDIA_TYPES:
        raise ValueError("media_type must be 'movie' or 'series'")
    provider = str(provider or "").strip().lower()
    provider_url = str(provider_url or "").strip()
    title = str(title or "").strip()
    if not provider or not provider_url or not title:
        raise ValueError("provider, provider_url and title are required")

    normalized = __import__("re").sub(
        r"[^\w]+", " ", title.casefold()
    ).strip()
    now = _utc_now()
    with _connect() as db:
        db.execute(
            """
            INSERT INTO unresolved_provider_titles(
                provider, provider_url, provider_title, normalized_title,
                media_type, year, poster_url, description, metadata_json,
                first_seen_at, last_seen_at, attempts, last_attempt_at, last_error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            ON CONFLICT(provider, provider_url) DO UPDATE SET
                provider_title=excluded.provider_title,
                normalized_title=excluded.normalized_title,
                media_type=excluded.media_type,
                year=COALESCE(excluded.year, unresolved_provider_titles.year),
                poster_url=COALESCE(excluded.poster_url, unresolved_provider_titles.poster_url),
                description=COALESCE(excluded.description, unresolved_provider_titles.description),
                metadata_json=CASE WHEN excluded.metadata_json='{}'
                    THEN unresolved_provider_titles.metadata_json
                    ELSE excluded.metadata_json END,
                last_seen_at=excluded.last_seen_at,
                attempts=unresolved_provider_titles.attempts + 1,
                last_attempt_at=excluded.last_attempt_at,
                last_error=excluded.last_error
            """,
            (provider, provider_url, title, normalized, media_type, year,
             poster_url, description, json.dumps(metadata or {}, ensure_ascii=False),
             now, now, now, (error or "")[:1000] or None),
        )


def get_unresolved_titles(
    provider: Optional[str] = None, limit: int = 500
) -> list[dict[str, Any]]:
    """Return unresolved provider records for matching/review tooling."""
    limit = max(1, min(int(limit), 5000))
    with _connect() as db:
        if provider:
            rows = db.execute(
                """
                SELECT * FROM unresolved_provider_titles
                WHERE resolved_imdb_id IS NULL AND provider = ?
                ORDER BY last_seen_at DESC LIMIT ?
                """,
                (provider.strip().lower(), limit),
            ).fetchall()
        else:
            rows = db.execute(
                """
                SELECT * FROM unresolved_provider_titles
                WHERE resolved_imdb_id IS NULL
                ORDER BY last_seen_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
    return [
        {**dict(row), "metadata": json.loads(row["metadata_json"] or "{}")}
        for row in rows
    ]


def mark_unresolved_title_resolved(
    provider: str, provider_url: str, imdb_id: str
) -> None:
    """Mark a queued record as resolved after a separate confidence check."""
    imdb_id = str(imdb_id or "").strip()
    if not IMDB_ID_RE.fullmatch(imdb_id):
        raise ValueError("A valid IMDb ID is required")
    with _connect() as db:
        db.execute(
            """
            UPDATE unresolved_provider_titles
            SET resolved_imdb_id=?, resolved_at=?, last_error=NULL
            WHERE provider=? AND provider_url=?
            """,
            (imdb_id, _utc_now(), provider.strip().lower(), provider_url.strip()),
        )


def get_resolved_imdb_id(provider: str, provider_url: str) -> Optional[str]:
    """Return a manually or automatically verified IMDb mapping for a provider URL."""
    with _connect() as db:
        row = db.execute(
            """
            SELECT resolved_imdb_id
            FROM unresolved_provider_titles
            WHERE provider = ? AND provider_url = ?
              AND resolved_imdb_id IS NOT NULL
            """,
            (provider.strip().lower(), provider_url.strip()),
        ).fetchone()
    return str(row["resolved_imdb_id"]) if row else None


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
        unresolved = db.execute(
            "SELECT COUNT(*) FROM unresolved_provider_titles WHERE resolved_imdb_id IS NULL"
        ).fetchone()[0]
    return {"active_titles": int(titles), "active_providers": int(providers),
            "pending_mdb_sync": int(pending), "unresolved_titles": int(unresolved)}

def get_title_genres(media_type: str, imdb_id: str) -> list[str]:
    """Return canonical genre names assigned to a title."""
    if media_type not in MEDIA_TYPES:
        raise ValueError("Unsupported media type")
    with _connect() as db:
        rows = db.execute(
            """
            SELECT g.name
            FROM genres g
            JOIN title_genres tg ON tg.genre_id = g.id
            WHERE tg.media_type = ? AND tg.imdb_id = ?
            ORDER BY g.name COLLATE NOCASE
            """,
            (media_type, imdb_id),
        ).fetchall()
    return [str(row["name"]) for row in rows]


def get_catalog_titles(
    media_type: str,
    *,
    skip: int = 0,
    limit: int = 100,
    search: str = "",
    genre: str = "",
) -> list[dict[str, Any]]:
    """Return one Stremio catalog page from verified, currently available titles."""
    if media_type not in MEDIA_TYPES:
        raise ValueError("Unsupported media type")
    try:
        offset = max(0, int(skip))
    except (TypeError, ValueError):
        offset = 0
    try:
        page_size = max(1, min(100, int(limit)))
    except (TypeError, ValueError):
        page_size = 100

    clauses = [
        "t.media_type = ?",
        "t.catalog_active = 1",
        """EXISTS (
            SELECT 1 FROM provider_entries pe
            WHERE pe.media_type = t.media_type
              AND pe.imdb_id = t.imdb_id
              AND pe.availability_status = 'available'
        )""",
    ]
    params: list[Any] = [media_type]
    query = str(search or "").strip()
    if query:
        clauses.append(
            "(instr(lower(t.title), lower(?)) > 0 "
            "OR instr(lower(t.metadata_json), lower(?)) > 0)"
        )
        params.extend([query[:200], query[:200]])
    selected_genre = str(genre or "").strip()
    if selected_genre:
        clauses.append(
            """EXISTS (
                SELECT 1
                FROM title_genres tg
                JOIN genres g ON g.id = tg.genre_id
                WHERE tg.media_type = t.media_type
                  AND tg.imdb_id = t.imdb_id
                  AND (g.slug = ? OR g.name = ? COLLATE NOCASE)
            )"""
        )
        params.extend([selected_genre, selected_genre])

    with _connect() as db:
        rows = db.execute(
            f"""
            SELECT t.media_type, t.imdb_id, t.title, t.year, t.metadata_json
            FROM titles t
            WHERE {" AND ".join(clauses)}
            ORDER BY t.last_verified_at DESC, t.title COLLATE NOCASE
            LIMIT ? OFFSET ?
            """,
            (*params, page_size, offset),
        ).fetchall()

        imdb_ids = [str(row["imdb_id"]) for row in rows]
        genres_by_id: dict[str, list[str]] = {imdb_id: [] for imdb_id in imdb_ids}
        if imdb_ids:
            placeholders = ",".join("?" for _ in imdb_ids)
            genre_rows = db.execute(
                f"""
                SELECT tg.imdb_id, g.name
                FROM title_genres tg
                JOIN genres g ON g.id = tg.genre_id
                WHERE tg.media_type = ? AND tg.imdb_id IN ({placeholders})
                ORDER BY g.name COLLATE NOCASE
                """,
                (media_type, *imdb_ids),
            ).fetchall()
            for genre_row in genre_rows:
                genres_by_id[str(genre_row["imdb_id"])].append(str(genre_row["name"]))

    result: list[dict[str, Any]] = []
    for row in rows:
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        imdb_id = str(row["imdb_id"])
        result.append({
            "media_type": str(row["media_type"]),
            "imdb_id": imdb_id,
            "title": str(row["title"]),
            "year": row["year"],
            "metadata": metadata,
            "genres": genres_by_id.get(imdb_id, []),
        })
    return result


def get_metadata_candidates(*, limit: int = 500, force: bool = False) -> list[dict[str, Any]]:
    """Return active titles that still need TMDB metadata enrichment."""
    try:
        max_items = max(1, min(5000, int(limit)))
    except (TypeError, ValueError):
        max_items = 500
    with _connect() as db:
        rows = db.execute(
            """
            SELECT t.media_type, t.imdb_id, t.title, t.year, t.metadata_json
            FROM titles t
            WHERE t.catalog_active = 1
              AND EXISTS (
                  SELECT 1 FROM provider_entries pe
                  WHERE pe.media_type = t.media_type
                    AND pe.imdb_id = t.imdb_id
                    AND pe.availability_status = 'available'
              )
            ORDER BY t.last_verified_at DESC, t.title COLLATE NOCASE
            """
        ).fetchall()

    candidates: list[dict[str, Any]] = []
    for row in rows:
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        if not force and isinstance(metadata.get("tmdb"), dict):
            continue
        candidates.append({
            "media_type": str(row["media_type"]),
            "imdb_id": str(row["imdb_id"]),
            "title": str(row["title"]),
            "year": row["year"],
            "metadata": metadata,
        })
        if len(candidates) >= max_items:
            break
    return candidates


def update_title_metadata(
    media_type: str,
    imdb_id: str,
    metadata: dict[str, Any],
    *,
    genres: Optional[list[str]] = None,
) -> bool:
    """Merge enriched metadata into a verified title and optionally replace genres."""
    if media_type not in MEDIA_TYPES:
        raise ValueError("Unsupported media type")
    imdb_id = str(imdb_id or "").strip()
    if not IMDB_ID_RE.fullmatch(imdb_id):
        raise ValueError("A valid IMDb ID is required")

    with _connect() as db:
        row = db.execute(
            "SELECT metadata_json FROM titles WHERE media_type=? AND imdb_id=?",
            (media_type, imdb_id),
        ).fetchone()
        if row is None:
            return False
        try:
            merged = json.loads(row["metadata_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            merged = {}
        if not isinstance(merged, dict):
            merged = {}
        for key, value in (metadata or {}).items():
            if value is not None:
                merged[key] = value
        db.execute(
            "UPDATE titles SET metadata_json=? WHERE media_type=? AND imdb_id=?",
            (json.dumps(merged, ensure_ascii=False), media_type, imdb_id),
        )

        if genres is not None:
            db.execute(
                "DELETE FROM title_genres WHERE media_type=? AND imdb_id=?",
                (media_type, imdb_id),
            )
            for genre in genres:
                name = str(genre or "").strip()
                if not name:
                    continue
                slug = __import__("re").sub(
                    r"[^a-z0-9]+", "-", name.casefold()
                ).strip("-")
                if not slug:
                    continue
                db.execute(
                    "INSERT OR IGNORE INTO genres(slug, name) VALUES (?, ?)",
                    (slug, name),
                )
                genre_row = db.execute(
                    "SELECT id FROM genres WHERE slug=?", (slug,)
                ).fetchone()
                db.execute(
                    """
                    INSERT OR IGNORE INTO title_genres(media_type, imdb_id, genre_id)
                    VALUES (?, ?, ?)
                    """,
                    (media_type, imdb_id, int(genre_row["id"])),
                )
    return True


def get_genre_catalog(media_type: str, genre_slug: str) -> list[dict[str, Any]]:
    """Return active titles for one genre, ready for Stremio catalog mapping."""
    if media_type not in MEDIA_TYPES:
        raise ValueError("Unsupported media type")
    with _connect() as db:
        rows = db.execute(
            """
            SELECT t.media_type, t.imdb_id, t.title, t.year, t.metadata_json
            FROM titles t
            JOIN title_genres tg
              ON tg.media_type = t.media_type AND tg.imdb_id = t.imdb_id
            JOIN genres g ON g.id = tg.genre_id
            WHERE t.media_type = ? AND t.catalog_active = 1 AND g.slug = ?
            ORDER BY t.title COLLATE NOCASE
            """,
            (media_type, genre_slug),
        ).fetchall()
    return [
        {
            "media_type": row["media_type"],
            "imdb_id": row["imdb_id"],
            "title": row["title"],
            "year": row["year"],
            "metadata": json.loads(row["metadata_json"] or "{}"),
        }
        for row in rows
    ]


def list_genres(media_type: Optional[str] = None) -> list[dict[str, str]]:
    """List genres currently assigned to active titles."""
    if media_type is not None and media_type not in MEDIA_TYPES:
        raise ValueError("Unsupported media type")
    with _connect() as db:
        if media_type:
            rows = db.execute(
                """
                SELECT DISTINCT g.slug, g.name
                FROM genres g
                JOIN title_genres tg ON tg.genre_id = g.id
                JOIN titles t
                  ON t.media_type = tg.media_type AND t.imdb_id = tg.imdb_id
                WHERE tg.media_type = ? AND t.catalog_active = 1
                  AND EXISTS (
                      SELECT 1 FROM provider_entries pe
                      WHERE pe.media_type = t.media_type
                        AND pe.imdb_id = t.imdb_id
                        AND pe.availability_status = 'available'
                  )
                ORDER BY g.name COLLATE NOCASE
                """,
                (media_type,),
            ).fetchall()
        else:
            rows = db.execute(
                """
                SELECT DISTINCT g.slug, g.name
                FROM genres g
                JOIN title_genres tg ON tg.genre_id = g.id
                JOIN titles t
                  ON t.media_type = tg.media_type AND t.imdb_id = tg.imdb_id
                WHERE t.catalog_active = 1
                  AND EXISTS (
                      SELECT 1 FROM provider_entries pe
                      WHERE pe.media_type = t.media_type
                        AND pe.imdb_id = t.imdb_id
                        AND pe.availability_status = 'available'
                  )
                ORDER BY g.name COLLATE NOCASE
                """
            ).fetchall()
    return [{"slug": row["slug"], "name": row["name"]} for row in rows]

def record_series_structure(
    media_type: str,
    imdb_id: str,
    provider: str,
    seasons: list[dict[str, Any]],
) -> None:
    """Persist seasons, episodes and available language/hoster source variants.

    Source URLs are provider redirect/embed URLs, not resolved temporary media
    URLs. This keeps expiring playback tokens out of the catalog database.
    """
    if media_type not in MEDIA_TYPES:
        raise ValueError("Unsupported media type")
    if not IMDB_ID_RE.fullmatch(str(imdb_id or "")):
        raise ValueError("A valid IMDb ID is required")
    provider = str(provider or "").strip().lower()
    if not provider:
        raise ValueError("provider is required")

    now = _utc_now()
    with _connect() as db:
        for season in seasons or []:
            season_number = int(season.get("season_number", 0))
            season_url = str(season.get("url") or "").strip()
            if not season_url:
                continue
            db.execute(
                """
                INSERT INTO provider_seasons(
                    media_type, imdb_id, provider, season_number,
                    provider_url, is_movie_collection, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(media_type, imdb_id, provider, season_number)
                DO UPDATE SET provider_url=excluded.provider_url,
                    is_movie_collection=excluded.is_movie_collection,
                    last_seen_at=excluded.last_seen_at
                """,
                (media_type, imdb_id, provider, season_number, season_url,
                 int(bool(season.get("is_movie_collection"))), now),
            )
            for episode in season.get("episodes") or []:
                episode_number = int(episode.get("episode_number", 0))
                episode_url = str(episode.get("url") or "").strip()
                if not episode_url:
                    continue
                db.execute(
                    """
                    INSERT INTO provider_episodes(
                        media_type, imdb_id, provider, season_number,
                        episode_number, provider_url, title, title_original,
                        is_movie, last_seen_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(
                        media_type, imdb_id, provider, season_number, episode_number
                    ) DO UPDATE SET provider_url=excluded.provider_url,
                        title=COALESCE(excluded.title, provider_episodes.title),
                        title_original=COALESCE(excluded.title_original, provider_episodes.title_original),
                        is_movie=excluded.is_movie,
                        last_seen_at=excluded.last_seen_at
                    """,
                    (media_type, imdb_id, provider, season_number, episode_number,
                     episode_url, episode.get("title"), episode.get("title_original"),
                     int(bool(episode.get("is_movie"))), now),
                )
                for variant in episode.get("variants") or []:
                    audio = str(variant.get("audio_language") or "Unknown").strip()
                    subtitles = str(variant.get("subtitle_language") or "").strip()
                    hoster = str(variant.get("hoster") or "").strip()
                    db.execute(
                        """
                        INSERT INTO episode_sources(
                            media_type, imdb_id, provider, season_number, episode_number,
                            audio_language, subtitle_language, hoster, source_url, last_seen_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(
                            media_type, imdb_id, provider, season_number, episode_number,
                            audio_language, subtitle_language, hoster
                        ) DO UPDATE SET source_url=excluded.source_url,
                            last_seen_at=excluded.last_seen_at
                        """,
                        (media_type, imdb_id, provider, season_number, episode_number,
                         audio, subtitles, hoster, variant.get("source_url"), now),
                    )


def get_series_structure(media_type: str, imdb_id: str) -> list[dict[str, Any]]:
    """Read the stored provider season/episode/source tree for one IMDb title."""
    if media_type not in MEDIA_TYPES:
        raise ValueError("Unsupported media type")
    with _connect() as db:
        seasons = db.execute(
            """
            SELECT provider, season_number, provider_url, is_movie_collection
            FROM provider_seasons
            WHERE media_type=? AND imdb_id=?
            ORDER BY provider, season_number
            """,
            (media_type, imdb_id),
        ).fetchall()
        result = []
        for season in seasons:
            episodes = db.execute(
                """
                SELECT provider, season_number, episode_number, provider_url,
                       title, title_original, is_movie
                FROM provider_episodes
                WHERE media_type=? AND imdb_id=? AND provider=? AND season_number=?
                ORDER BY episode_number
                """,
                (media_type, imdb_id, season["provider"], season["season_number"]),
            ).fetchall()
            episode_items = []
            for episode in episodes:
                sources = db.execute(
                    """
                    SELECT audio_language, subtitle_language, hoster, source_url
                    FROM episode_sources
                    WHERE media_type=? AND imdb_id=? AND provider=?
                      AND season_number=? AND episode_number=?
                    ORDER BY audio_language, hoster
                    """,
                    (media_type, imdb_id, episode["provider"],
                     episode["season_number"], episode["episode_number"]),
                ).fetchall()
                episode_items.append({
                    "episode_number": episode["episode_number"],
                    "url": episode["provider_url"],
                    "title": episode["title"],
                    "title_original": episode["title_original"],
                    "is_movie": bool(episode["is_movie"]),
                    "variants": [dict(source) for source in sources],
                })
            result.append({
                "provider": season["provider"],
                "season_number": season["season_number"],
                "url": season["provider_url"],
                "is_movie_collection": bool(season["is_movie_collection"]),
                "episodes": episode_items,
            })
    return result

