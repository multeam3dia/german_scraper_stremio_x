"""Small SQLite helpers for incremental provider catalog crawls."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from catalog_db import _connect


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def load_provider_url_state(provider: str):
    """Return known URLs and unresolved URLs with their retry metadata."""
    provider = provider.strip().lower()
    with _connect() as db:
        known_rows = db.execute(
            """
            SELECT DISTINCT provider_url FROM provider_entries
            WHERE provider=? AND provider_url IS NOT NULL AND provider_url!=''
            """,
            (provider,),
        ).fetchall()
        unresolved_rows = db.execute(
            """
            SELECT provider_url, attempts, last_attempt_at
            FROM unresolved_provider_titles
            WHERE provider=? AND resolved_imdb_id IS NULL
            """,
            (provider,),
        ).fetchall()
    known = {str(row["provider_url"]) for row in known_rows}
    pending = {
        str(row["provider_url"]): {
            "attempts": int(row["attempts"] or 0),
            "last_attempt_at": row["last_attempt_at"],
        }
        for row in unresolved_rows
    }
    return known, pending


def touch_provider_url(provider: str, provider_url: str) -> None:
    """Refresh presence timestamps when a complete index still lists a known URL."""
    now = _now()
    with _connect() as db:
        db.execute(
            """
            UPDATE provider_entries
            SET last_seen_at=?, last_checked_at=?, availability_status='available',
                consecutive_misses=0, last_error=NULL
            WHERE provider=? AND provider_url=?
            """,
            (now, now, provider.strip().lower(), provider_url),
        )
        db.execute(
            """
            UPDATE unresolved_provider_titles
            SET last_seen_at=?
            WHERE provider=? AND provider_url=? AND resolved_imdb_id IS NULL
            """,
            (now, provider.strip().lower(), provider_url),
        )


def unresolved_retry_due(entry: dict, *, now: datetime | None = None) -> bool:
    """Retry unresolved IMDb extraction infrequently, with capped backoff."""
    last_attempt = entry.get("last_attempt_at")
    if not last_attempt:
        return True
    try:
        attempted_at = datetime.fromisoformat(str(last_attempt))
        if attempted_at.tzinfo is None:
            attempted_at = attempted_at.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return True
    attempts = max(1, int(entry.get("attempts") or 1))
    delay_days = min(90, max(14, 7 * (2 ** min(attempts, 4))))
    current = now or datetime.now(timezone.utc)
    return current - attempted_at >= timedelta(days=delay_days)
