"""Tests for durable unresolved provider-title storage."""

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from identity_resolver import resolver_retry_due

import catalog_db



class ResolverRetryCooldownTests(unittest.TestCase):
    def test_new_records_are_immediately_retryable(self):
        self.assertTrue(resolver_retry_due({"resolver_attempts": 0}))

    def test_first_failure_waits_one_day(self):
        now = datetime(2026, 11, 24, tzinfo=timezone.utc)
        record = {
            "resolver_attempts": 1,
            "resolver_last_attempt_at": now.isoformat(),
        }
        self.assertFalse(resolver_retry_due(record, now=now + timedelta(hours=23)))
        self.assertTrue(resolver_retry_due(record, now=now + timedelta(days=1)))

    def test_retry_delay_exponentially_steps_and_caps_at_90_days(self):
        now = datetime(2026, 11, 24, tzinfo=timezone.utc)
        schedule = (1, 3, 7, 14, 30, 60, 90, 90)
        for attempts, delay in enumerate(schedule, start=1):
            record = {
                "resolver_attempts": attempts,
                "resolver_last_attempt_at": now.isoformat(),
            }
            self.assertFalse(
                resolver_retry_due(record, now=now + timedelta(days=delay, hours=-1)),
                f"attempt {attempts} should not retry before {delay} days",
            )
            self.assertTrue(
                resolver_retry_due(record, now=now + timedelta(days=delay)),
                f"attempt {attempts} should retry at {delay} days",
            )

    def test_missing_or_invalid_retry_timestamp_is_retryable(self):
        self.assertTrue(resolver_retry_due({"resolver_attempts": 3}))
        self.assertTrue(resolver_retry_due({
            "resolver_attempts": 3,
            "resolver_last_attempt_at": "not-a-date",
        }))

    def test_non_finite_retry_attempt_count_is_retryable(self):
        record = {
            "resolver_attempts": float("inf"),
            "resolver_last_attempt_at": "2026-11-24T00:00:00+00:00",
        }
        now = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)
        self.assertTrue(resolver_retry_due(record, now=now))

    def test_naive_timestamp_is_interpreted_as_utc(self):
        record = {
            "resolver_attempts": 2,
            "resolver_last_attempt_at": "2026-11-24T00:00:00",
        }
        now = datetime(2026, 11, 27, tzinfo=timezone.utc)
        self.assertTrue(resolver_retry_due(record, now=now))

    def test_offset_timestamp_uses_absolute_instant_for_retry_boundary(self):
        record = {
            "resolver_attempts": 1,
            "resolver_last_attempt_at": "2026-11-24T00:00:00+02:00",
        }
        before_due = datetime(2026, 11, 24, 21, 59, tzinfo=timezone.utc)
        due = datetime(2026, 11, 24, 22, 0, tzinfo=timezone.utc)
        self.assertFalse(resolver_retry_due(record, now=before_due))
        self.assertTrue(resolver_retry_due(record, now=due))

class UnresolvedProviderTitleTests(unittest.TestCase):
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

    def test_unresolved_title_is_persisted_and_counted(self):
        catalog_db.record_unresolved_title(
            "serienstream",
            "https://serienstream.to/serie/example",
            "Example Show",
            year=2022,
            poster_url="https://img.example/poster.jpg",
            metadata={"country": "DE"},
        )
        records = catalog_db.get_unresolved_titles("serienstream")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["provider_title"], "Example Show")
        self.assertEqual(records[0]["normalized_title"], "example show")
        self.assertEqual(records[0]["metadata"], {"country": "DE"})
        self.assertEqual(records[0]["attempts"], 1)
        self.assertIsNotNone(records[0]["last_attempt_at"])
        self.assertEqual(records[0]["resolver_attempts"], 0)
        self.assertIsNone(records[0]["resolver_last_attempt_at"])
        self.assertEqual(catalog_db.get_catalog_stats()["unresolved_titles"], 1)

    def test_discovery_refresh_does_not_count_resolver_attempts(self):
        url = "https://serienstream.to/serie/example"
        catalog_db.record_unresolved_title("serienstream", url, "Example Show")
        catalog_db.record_unresolved_title("serienstream", url, "Example Show")
        record = catalog_db.get_unresolved_titles()[0]
        self.assertEqual(record["attempts"], 2)
        self.assertEqual(record["resolver_attempts"], 0)
        self.assertIsNone(record["resolver_last_attempt_at"])

    def test_resolver_attempt_is_counted_and_timestamps(self):
        url = "https://serienstream.to/serie/example"
        catalog_db.record_unresolved_title("serienstream", url, "Example Show")
        catalog_db.record_unresolved_attempt("serienstream", url)
        record = catalog_db.get_unresolved_titles()[0]
        self.assertEqual(record["attempts"], 1)
        self.assertEqual(record["resolver_attempts"], 1)
        self.assertIsNotNone(record["resolver_last_attempt_at"])

    def test_resolved_record_leaves_pending_queue(self):
        url = "https://serienstream.to/serie/example"
        catalog_db.record_unresolved_title("serienstream", url, "Example Show")
        catalog_db.mark_unresolved_title_resolved("serienstream", url, "tt1234567")
        self.assertEqual(catalog_db.get_unresolved_titles(), [])
        self.assertEqual(catalog_db.get_catalog_stats()["unresolved_titles"], 0)

    def test_resolved_record_cannot_be_overwritten_by_later_resolution(self):
        url = "https://serienstream.to/serie/example"
        catalog_db.record_unresolved_title("serienstream", url, "Example Show")
        catalog_db.mark_unresolved_title_resolved("serienstream", url, "tt1234567")
        catalog_db.mark_unresolved_title_identified("serienstream", url, "gsx:series:abc")
        record = catalog_db.get_resolved_imdb_id("serienstream", url)
        self.assertEqual(record, "tt1234567")
        self.assertEqual(catalog_db.get_unresolved_titles(), [])

    def test_invalid_imdb_id_cannot_resolve_record(self):
        url = "https://serienstream.to/serie/example"
        catalog_db.record_unresolved_title("serienstream", url, "Example Show")
        with self.assertRaises(ValueError):
            catalog_db.mark_unresolved_title_resolved("serienstream", url, "not-an-id")


if __name__ == "__main__":
    unittest.main()
