"""Tests for incremental provider URL state and unresolved-title backoff."""

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

import catalog_db
from providers.crawl_state import (
    load_provider_url_state,
    touch_provider_url,
    unresolved_retry_due,
)


class CrawlStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.previous = os.environ.get("CATALOG_DB_PATH")
        os.environ["CATALOG_DB_PATH"] = os.path.join(self.tmp.name, "catalog.sqlite3")
        catalog_db.init_db()

    def tearDown(self):
        if self.previous is None:
            os.environ.pop("CATALOG_DB_PATH", None)
        else:
            os.environ["CATALOG_DB_PATH"] = self.previous
        self.tmp.cleanup()

    def test_known_provider_url_is_loaded_and_touch_refreshes_presence(self):
        catalog_db.record_discovery(
            "series",
            "tt1234567",
            "aniworld",
            "Example Show",
            provider_url="https://aniworld.to/anime/stream/example",
        )

        known, pending = load_provider_url_state("aniworld")
        self.assertIn("https://aniworld.to/anime/stream/example", known)
        self.assertEqual(pending, {})

        touch_provider_url(
            "aniworld", "https://aniworld.to/anime/stream/example"
        )
        with catalog_db._connect() as db:
            row = db.execute(
                """
                SELECT availability_status, consecutive_misses, last_seen_at,
                       last_checked_at, last_error
                FROM provider_entries
                WHERE provider='aniworld' AND imdb_id='tt1234567'
                """
            ).fetchone()

        self.assertEqual(row["availability_status"], "available")
        self.assertEqual(row["consecutive_misses"], 0)
        self.assertIsNotNone(row["last_seen_at"])
        self.assertIsNotNone(row["last_checked_at"])
        self.assertIsNone(row["last_error"])

    def test_unresolved_urls_are_loaded_with_retry_metadata(self):
        catalog_db.record_unresolved_title(
            "serienstream",
            "https://serienstream.to/serie/example",
            "Example Show",
        )

        known, pending = load_provider_url_state("serienstream")
        self.assertEqual(known, set())
        self.assertIn("https://serienstream.to/serie/example", pending)
        entry = pending["https://serienstream.to/serie/example"]
        self.assertEqual(entry["attempts"], 1)
        self.assertIsNotNone(entry["last_attempt_at"])

    def test_unresolved_retry_backoff_grows_and_is_capped(self):
        now = datetime(2026, 11, 18, tzinfo=timezone.utc)
        attempted = now - timedelta(days=27)

        self.assertFalse(unresolved_retry_due({
            "attempts": 2, "last_attempt_at": attempted.isoformat()
        }, now=now))
        self.assertTrue(unresolved_retry_due({
            "attempts": 2,
            "last_attempt_at": (now - timedelta(days=28)).isoformat()
        }, now=now))
        self.assertFalse(unresolved_retry_due({
            "attempts": 5,
            "last_attempt_at": (now - timedelta(days=89)).isoformat()
        }, now=now))
        self.assertTrue(unresolved_retry_due({
            "attempts": 5,
            "last_attempt_at": (now - timedelta(days=90)).isoformat()
        }, now=now))

    def test_missing_or_invalid_attempt_timestamp_is_retryable(self):
        self.assertTrue(unresolved_retry_due({"attempts": 3}, now=datetime.now(timezone.utc)))
        self.assertTrue(unresolved_retry_due({
            "attempts": 3, "last_attempt_at": "not-a-date"
        }, now=datetime.now(timezone.utc)))


if __name__ == "__main__":
    unittest.main()
