"""Tests for durable unresolved provider-title storage."""

import os
import tempfile
import unittest

import catalog_db


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
        self.assertEqual(catalog_db.get_catalog_stats()["unresolved_titles"], 1)

    def test_resolved_record_leaves_pending_queue(self):
        url = "https://serienstream.to/serie/example"
        catalog_db.record_unresolved_title("serienstream", url, "Example Show")
        catalog_db.mark_unresolved_title_resolved("serienstream", url, "tt1234567")
        self.assertEqual(catalog_db.get_unresolved_titles(), [])
        self.assertEqual(catalog_db.get_catalog_stats()["unresolved_titles"], 0)

    def test_invalid_imdb_id_cannot_resolve_record(self):
        url = "https://serienstream.to/serie/example"
        catalog_db.record_unresolved_title("serienstream", url, "Example Show")
        with self.assertRaises(ValueError):
            catalog_db.mark_unresolved_title_resolved("serienstream", url, "not-an-id")


if __name__ == "__main__":
    unittest.main()
