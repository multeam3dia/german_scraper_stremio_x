"""Tests for the persistent TMDB response cache."""

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

import catalog_db


class TmdbResponseCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.previous_path = os.environ.get("CATALOG_DB_PATH")
        os.environ["CATALOG_DB_PATH"] = os.path.join(self.tmp.name, "catalog.sqlite3")
        catalog_db.init_db()

    def tearDown(self):
        if self.previous_path is None:
            os.environ.pop("CATALOG_DB_PATH", None)
        else:
            os.environ["CATALOG_DB_PATH"] = self.previous_path
        self.tmp.cleanup()

    def test_response_round_trips_across_database_connections(self):
        key = '["/search/tv", {"query": "Example"}]'
        payload = {"results": [{"id": 123, "name": "Example"}]}
        catalog_db.set_tmdb_cached_response(key, payload)
        self.assertEqual(catalog_db.get_tmdb_cached_response(key), payload)

    def test_cache_entry_expires_after_ttl(self):
        key = "expired-entry"
        cached_at = datetime(2026, 11, 24, tzinfo=timezone.utc)
        catalog_db.set_tmdb_cached_response(key, {"results": []}, cached_at=cached_at)
        self.assertIsNotNone(catalog_db.get_tmdb_cached_response(
            key, ttl_seconds=3600, now=cached_at + timedelta(minutes=59)
        ))
        self.assertIsNone(catalog_db.get_tmdb_cached_response(
            key, ttl_seconds=3600, now=cached_at + timedelta(hours=1)
        ))

    def test_cache_updates_existing_key(self):
        key = "same-key"
        catalog_db.set_tmdb_cached_response(key, {"v": 1})
        catalog_db.set_tmdb_cached_response(key, {"v": 2})
        self.assertEqual(catalog_db.get_tmdb_cached_response(key), {"v": 2})

    def test_purge_removes_expired_rows_and_keeps_fresh_rows(self):
        now = datetime(2026, 11, 24, tzinfo=timezone.utc)
        catalog_db.set_tmdb_cached_response(
            "old", {"v": "old"}, cached_at=now - timedelta(days=8)
        )
        catalog_db.set_tmdb_cached_response(
            "fresh", {"v": "fresh"}, cached_at=now - timedelta(days=2)
        )
        removed = catalog_db.purge_expired_tmdb_cache(
            ttl_seconds=7 * 24 * 60 * 60, now=now
        )
        self.assertEqual(removed, 1)
        self.assertIsNone(catalog_db.get_tmdb_cached_response(
            "old", now=now, ttl_seconds=7 * 24 * 60 * 60
        ))
        self.assertEqual(catalog_db.get_tmdb_cached_response(
            "fresh", now=now, ttl_seconds=7 * 24 * 60 * 60
        ), {"v": "fresh"})

    def test_purge_handles_naive_now_as_utc(self):
        now = datetime(2026, 11, 24, 12, 0, 0)
        catalog_db.set_tmdb_cached_response(
            "expired-naive", {"v": 1}, cached_at=now - timedelta(days=10)
        )
        self.assertEqual(catalog_db.purge_expired_tmdb_cache(
            ttl_seconds=7 * 24 * 60 * 60, now=now
        ), 1)

    def test_usage_stats_track_avoided_requests_and_top_keys(self):
        catalog_db.record_tmdb_cache_usage(
            "key-a", endpoint="/search/tv", avoided_http_request=True
        )
        catalog_db.record_tmdb_cache_usage(
            "key-a", endpoint="/search/tv", avoided_http_request=True
        )
        catalog_db.record_tmdb_cache_usage(
            "key-a", endpoint="/search/tv", avoided_http_request=False
        )
        catalog_db.record_tmdb_cache_usage(
            "key-b", endpoint="/tv/123", avoided_http_request=True
        )
        totals = catalog_db.get_tmdb_cache_usage_totals()
        self.assertEqual(totals["cache_uses"], 4)
        self.assertEqual(totals["avoided_http_requests"], 3)
        self.assertEqual(totals["unique_cache_keys"], 2)
        top = catalog_db.get_tmdb_cache_usage(limit=1)
        self.assertEqual(top[0]["cache_key"], "key-a")
        self.assertEqual(top[0]["request_count"], 3)
        self.assertEqual(top[0]["avoided_http_requests"], 2)

    def test_non_object_payload_is_rejected(self):
        with self.assertRaises(ValueError):
            catalog_db.set_tmdb_cached_response("bad", ["not", "an", "object"])


if __name__ == "__main__":
    unittest.main()
