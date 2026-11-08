"""Tests for confidence-aware TMDB identity matching."""

import os
import tempfile
import unittest

import catalog_db
from identity_resolver import find_tmdb_match, resolve_unresolved_titles


class IdentityResolverTests(unittest.TestCase):
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

    def test_exact_title_and_year_returns_imdb_match(self):
        def fake_get(path, params):
            if path == "/search/tv":
                return {"results": [
                    {"id": 10, "name": "Dark", "original_name": "Dark",
                     "first_air_date": "2017-12-01"},
                    {"id": 11, "name": "Dark", "original_name": "Dark",
                     "first_air_date": "2022-01-01"},
                ]}
            return {"imdb_id": "tt5753856"}

        result = find_tmdb_match(
            {"media_type": "series", "provider_title": "Dark", "year": 2017},
            search_fn=fake_get,
        )
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["imdb_id"], "tt5753856")
        self.assertGreaterEqual(result["score"], 0.94)

    def test_similarly_scored_remakes_are_ambiguous(self):
        def fake_get(path, params):
            if path == "/search/tv":
                return {"results": [
                    {"id": 10, "name": "The Bridge", "original_name": "The Bridge",
                     "first_air_date": "2011-01-01"},
                    {"id": 11, "name": "The Bridge", "original_name": "The Bridge",
                     "first_air_date": "2013-01-01"},
                ]}
            return {"imdb_id": "tt1234567"}

        result = find_tmdb_match(
            {"media_type": "series", "provider_title": "The Bridge", "year": 2012},
            search_fn=fake_get,
        )
        self.assertEqual(result["status"], "ambiguous")

    def test_dry_run_does_not_admit_title_to_catalog(self):
        catalog_db.record_unresolved_title(
            "serienstream", "https://serienstream.to/serie/dark", "Dark",
            "series", year=2017,
        )

        def fake_get(path, params):
            if path == "/search/tv":
                return {"results": [
                    {"id": 10, "name": "Dark", "original_name": "Dark",
                     "first_air_date": "2017-12-01"}
                ]}
            return {"imdb_id": "tt5753856"}

        summary = resolve_unresolved_titles(apply=False, search_fn=fake_get)
        self.assertEqual(summary["matched"], 1)
        self.assertEqual(summary["applied"], 0)
        self.assertEqual(catalog_db.get_catalog_stats()["active_titles"], 0)

    def test_apply_records_match_and_resolves_queue_item(self):
        url = "https://serienstream.to/serie/dark"
        catalog_db.record_unresolved_title(
            "serienstream", url, "Dark", "series", year=2017,
            metadata={"country": "DE"},
        )

        def fake_get(path, params):
            if path == "/search/tv":
                return {"results": [
                    {"id": 10, "name": "Dark", "original_name": "Dark",
                     "first_air_date": "2017-12-01"}
                ]}
            return {"imdb_id": "tt5753856"}

        summary = resolve_unresolved_titles(apply=True, search_fn=fake_get)
        self.assertEqual(summary["applied"], 1)
        self.assertEqual(catalog_db.get_unresolved_titles(), [])
        self.assertEqual(
            catalog_db.get_resolved_imdb_id("serienstream", url), "tt5753856"
        )
        self.assertEqual(catalog_db.get_catalog_stats()["active_titles"], 1)


if __name__ == "__main__":
    unittest.main()
