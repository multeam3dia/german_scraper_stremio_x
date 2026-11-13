"""Conservative, persistent safety controls for catalog crawls."""
from __future__ import annotations

import os
import random
import time
import uuid
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

from niquests import Session

from aniworld.config import CA_CERT_BUNDLE, GLOBAL_SESSION
from catalog_db import _connect


class CrawlBlockedError(RuntimeError):
    """The provider appears to be blocking automated requests."""

    crawl_blocked = True


class ProviderCooldownError(CrawlBlockedError):
    """A previous block signal put this provider into a cooldown period."""


class ProviderAlreadyRunningError(RuntimeError):
    """A crawl for this provider is already active."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _ensure_state_table(db) -> None:
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS provider_crawl_guard (
            provider TEXT PRIMARY KEY,
            blocked_until TEXT,
            block_reason TEXT,
            lock_owner TEXT,
            lock_expires_at TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )


def _retry_after_seconds(response) -> int | None:
    value = (getattr(response, "headers", {}) or {}).get("Retry-After")
    if not value:
        return None
    try:
        return max(0, int(value.strip()))
    except (TypeError, ValueError):
        try:
            when = parsedate_to_datetime(value)
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            return max(0, int((when - _now()).total_seconds()))
        except (TypeError, ValueError, OverflowError):
            return None


class GuardedSession:
    """A dedicated provider session with serialized, throttled requests."""

    def __init__(self, session: Session, provider: str, guard: "CrawlGuard"):
        self._session = session
        self._provider = provider
        self._guard = guard
        self._last_request: float | None = None
        self.request_count = 0

    def get(self, url, *args, **kwargs):
        return self._request(self._session.get, url, *args, **kwargs)

    def post(self, url, *args, **kwargs):
        return self._request(self._session.post, url, *args, **kwargs)

    def close(self):
        self._session.close()

    def _wait(self):
        if self._last_request is not None:
            time.sleep(random.uniform(self._guard.delay_min, self._guard.delay_max))
        self._last_request = time.monotonic()

    def _block(self, reason: str, cooldown: timedelta):
        until = _now() + cooldown
        self._guard.set_cooldown(until, reason)
        raise CrawlBlockedError(
            f"{self._provider} crawl stopped: {reason}; cooldown until {_iso(until)}"
        )

    def _request(self, method, url, *args, **kwargs):
        host = (urlparse(str(url)).hostname or "").lower()
        if host not in self._guard.allowed_hosts:
            raise RuntimeError(
                f"Unexpected host during {self._provider} crawl: {host or 'unknown'}"
            )

        for attempt in range(3):
            self._guard.heartbeat()
            self._wait()
            self.request_count += 1
            try:
                response = method(url, *args, **kwargs)
            except Exception as exc:
                if attempt >= 2:
                    self._block(
                        f"Repeated network errors ({type(exc).__name__})",
                        timedelta(minutes=15),
                    )
                time.sleep(random.uniform(self._guard.delay_min, self._guard.delay_max))
                continue

            status = int(getattr(response, "status_code", 0) or 0)
            if status == 403:
                self._block("HTTP 403 Forbidden", timedelta(hours=6))
            if status == 429:
                seconds = _retry_after_seconds(response)
                self._block(
                    "HTTP 429 Too Many Requests",
                    timedelta(seconds=max(300, seconds or 0)),
                )

            body = str(getattr(response, "text", "") or "")[:12000].lower()
            challenge_markers = (
                "cf-chl-",
                "verify you are human",
                "checking your browser",
                "captcha-container",
                "captcha verification",
                "please solve the captcha",
                "unusual traffic",
                "robot check",
            )
            if any(marker in body for marker in challenge_markers):
                self._block("CAPTCHA or anti-bot challenge detected", timedelta(hours=12))

            if 500 <= status <= 599:
                if attempt < 2:
                    time.sleep(min(120, 20 * (2 ** attempt)))
                    continue
                self._block(
                    f"HTTP {status} persisted after retries",
                    timedelta(minutes=30),
                )
            return response

        raise RuntimeError(f"{self._provider} request failed after retries: {url}")


class CrawlGuard:
    """Acquire a per-provider lease and expose a throttled, isolated session."""

    HOSTS = {
        "aniworld": {"aniworld.to", "www.aniworld.to"},
        "serienstream": {
            "serienstream.to",
            "www.serienstream.to",
            "serienstream.cx",
            "www.serienstream.cx",
            "186.2.175.5",
        },
    }

    def __init__(self, provider: str):
        self.provider = provider.strip().lower()
        if self.provider not in self.HOSTS:
            raise ValueError(f"No crawl host policy configured for {self.provider}")
        self.allowed_hosts = self.HOSTS[self.provider]
        self.delay_min = max(1.0, float(os.getenv("CATALOG_CRAWL_DELAY_MIN", "5")))
        self.delay_max = max(
            self.delay_min,
            float(os.getenv("CATALOG_CRAWL_DELAY_MAX", "12")),
        )
        self.owner = uuid.uuid4().hex
        self.session: GuardedSession | None = None
        self._last_heartbeat = 0.0

    def __enter__(self) -> GuardedSession:
        now = _now()
        with _connect() as db:
            db.execute("BEGIN IMMEDIATE")
            _ensure_state_table(db)
            global_row = db.execute(
                "SELECT * FROM provider_crawl_guard WHERE provider='__global__'"
            ).fetchone()
            if (
                global_row
                and global_row["lock_expires_at"]
                and global_row["lock_expires_at"] > _iso(now)
            ):
                raise ProviderAlreadyRunningError(
                    "Another catalog crawl is already running; providers are serialized"
                )

            row = db.execute(
                "SELECT * FROM provider_crawl_guard WHERE provider=?",
                (self.provider,),
            ).fetchone()
            if row and row["blocked_until"] and row["blocked_until"] > _iso(now):
                raise ProviderCooldownError(
                    f"{self.provider} is in cooldown until {row['blocked_until']}: "
                    f"{row['block_reason'] or 'previous block signal'}"
                )
            if row and row["lock_expires_at"] and row["lock_expires_at"] > _iso(now):
                raise ProviderAlreadyRunningError(
                    f"A {self.provider} crawl is already running"
                )

            lease_until = _iso(now + timedelta(hours=12))
            for lock_provider in (self.provider, "__global__"):
                db.execute(
                    """
                    INSERT INTO provider_crawl_guard(
                        provider, blocked_until, block_reason, lock_owner,
                        lock_expires_at, updated_at
                    ) VALUES (?, NULL, NULL, ?, ?, ?)
                    ON CONFLICT(provider) DO UPDATE SET
                        lock_owner=excluded.lock_owner,
                        lock_expires_at=excluded.lock_expires_at,
                        updated_at=excluded.updated_at
                    """,
                    (lock_provider, self.owner, lease_until, _iso(now)),
                )

        try:
            raw = Session(
                resolver=["doh+cloudflare://"],
                disable_http3=True,
                multiplexed=False,
                headers=dict(GLOBAL_SESSION.headers),
            )
            raw.verify = CA_CERT_BUNDLE
            self.session = GuardedSession(raw, self.provider, self)
            return self.session
        except Exception:
            self._release_lock()
            raise

    def heartbeat(self) -> None:
        """Extend the lease periodically during a long initial catalog crawl."""
        current = time.monotonic()
        if current - self._last_heartbeat < 60:
            return
        now = _now()
        with _connect() as db:
            _ensure_state_table(db)
            cursor = db.execute(
                """
                UPDATE provider_crawl_guard
                SET lock_expires_at=?, updated_at=?
                WHERE provider IN (?, '__global__') AND lock_owner=?
                """,
                (
                    _iso(now + timedelta(hours=12)),
                    _iso(now),
                    self.provider,
                    self.owner,
                ),
            )
            if cursor.rowcount != 2:
                raise ProviderAlreadyRunningError(
                    f"The {self.provider} crawl lost its exclusive lease"
                )
        self._last_heartbeat = current

    def set_cooldown(self, until: datetime, reason: str) -> None:
        with _connect() as db:
            _ensure_state_table(db)
            db.execute(
                """
                UPDATE provider_crawl_guard
                SET blocked_until=?, block_reason=?, updated_at=?
                WHERE provider=? AND lock_owner=?
                """,
                (_iso(until), reason[:500], _iso(_now()), self.provider, self.owner),
            )

    def _release_lock(self) -> None:
        with _connect() as db:
            _ensure_state_table(db)
            db.execute(
                """
                UPDATE provider_crawl_guard
                SET lock_owner=NULL, lock_expires_at=NULL, updated_at=?
                WHERE provider IN (?, '__global__') AND lock_owner=?
                """,
                (_iso(_now()), self.provider, self.owner),
            )

    def __exit__(self, exc_type, exc, traceback):
        try:
            if self.session is not None:
                self.session.close()
        finally:
            self._release_lock()
        return False
