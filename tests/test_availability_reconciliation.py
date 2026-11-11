"""Tests for safe provider availability reconciliation."""

import os
import tempfile
import unittest

import catalog_db


class AvailabilityReconciliationTests(unittest.TestCase):
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

    def _completed_scan(self, provider, status="completed"):
        run_id = catalog_db.start_crawl_run(provider, "series")
        catalog_db.finish_crawl_run(run_id, status=status)
        return catalog_db.reconcile_provider_availability(
            provider, "series", run_id
        )

    def _provider_entry(self, provider, imdb_id):
        with catalog_db._connect() as db:
            return db.execute(
                """
                SELECT availability_status, consecutive_misses
                FROM provider_entries
                WHERE provider=? AND media_type='series' AND imdb_id=?
                """,
                (provider, imdb_id),
            ).fetchone()

    def test_two_complete_misses_remove_only_after_all_providers_are_missing(self):
        imdb_id = "tt1234567"
        catalog_db.record_discovery(
            "series", imdb_id, "aniworld", "Example Show"
        )
        catalog_db.record_discovery(
            "series", imdb_id, "serienstream", "Example Show"
        )

        first = self._completed_scan("aniworld")
        self.assertEqual(first["provider_entries_checked"], 1)
        self.assertEqual(self._provider_entry("aniworld", imdb_id)["consecutive_misses"], 1)
        self.assertEqual(
            self._provider_entry("aniworld", imdb_id)["availability_status"],
            "available",
        )
        self.assertEqual(len(catalog_db.get_catalog_titles("series")), 1)

        second = self._completed_scan("aniworld")
        self.assertEqual(second["provider_entries_marked_missing"], 1)
        self.assertEqual(
            self._provider_entry("aniworld", imdb_id)["availability_status"],
            "missing",
        )
        # Serienstream still confirms the title, so it stays in the catalog.
        self.assertEqual(len(catalog_db.get_catalog_titles("series")), 1)

        self._completed_scan("serienstream")
        final = self._completed_scan("serienstream")
        self.assertEqual(final["titles_deactivated"], 1)
        self.assertEqual(catalog_db.get_catalog_titles("series"), [])
        with catalog_db._connect() as db:
            remove = db.execute(
                """
                SELECT COUNT(*) FROM sync_outbox
                WHERE media_type='series' AND imdb_id=? AND action='remove'
                  AND status='pending'
                """,
                (imdb_id,),
            ).fetchone()[0]
        self.assertEqual(remove, 1)

    def test_playback_only_provider_does_not_keep_catalog_title_active(self):
        imdb_id = "tt1234567"
        catalog_db.record_discovery(
            "series", imdb_id, "aniworld", "Example Show"
        )
        # Simulate a legacy/non-catalog provider record for the same title.
        catalog_db.record_discovery(
            "series", imdb_id, "megakino", "Example Show"
        )

        self._completed_scan("aniworld")
        result = self._completed_scan("aniworld")

        self.assertEqual(result["titles_deactivated"], 1)
        self.assertEqual(catalog_db.get_catalog_titles("series"), [])
        with catalog_db._connect() as db:
            playback_entry = db.execute(
                """
                SELECT availability_status FROM provider_entries
                WHERE provider='megakino' AND media_type='series' AND imdb_id=?
                """,
                (imdb_id,),
            ).fetchone()
        self.assertEqual(playback_entry["availability_status"], "available")

    def test_partial_scan_never_marks_a_provider_missing(self):
        imdb_id = "tt1234567"
        catalog_db.record_discovery(
            "series", imdb_id, "aniworld", "Example Show"
        )
        result = self._completed_scan("aniworld", status="partial")
        self.assertTrue(result["skipped"])
        entry = self._provider_entry("aniworld", imdb_id)
        self.assertEqual(entry["availability_status"], "available")
        self.assertEqual(entry["consecutive_misses"], 0)

    def test_title_seen_during_scan_is_not_counted_as_a_miss(self):
        imdb_id = "tt1234567"
        catalog_db.record_discovery(
            "series", imdb_id, "aniworld", "Example Show"
        )
        run_id = catalog_db.start_crawl_run("aniworld", "series")
        catalog_db.record_discovery(
            "series", imdb_id, "aniworld", "Example Show"
        )
        catalog_db.finish_crawl_run(run_id, status="completed")
        result = catalog_db.reconcile_provider_availability(
            "aniworld", "series", run_id
        )
        self.assertEqual(result["provider_entries_checked"], 0)
        entry = self._provider_entry("aniworld", imdb_id)
        self.assertEqual(entry["availability_status"], "available")
        self.assertEqual(entry["consecutive_misses"], 0)


if __name__ == "__main__":
    unittest.main()
