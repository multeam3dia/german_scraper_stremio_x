"""Tests for IMDb-ID-based MDBList metadata enrichment."""

import os
import tempfile
import unittest

import catalog_db
from mdblist_enricher import (
    MDBListEnrichmentError,
    enrich_mdblist_metadata,
    find_mdblist_metadata,
)


class MDBListEnricherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.previous_path = os.environ.get("CATALOG_DB_PATH")
        os.environ["CATALOG_DB_PATH"] = os.path.join(self.tmp.name, "catalog.sqlite3")
        catalog_db.init_db()
        catalog_db.record_discovery(
            "series", "tt1234567", "aniworld", "Example Show", year=2020
        )

    def tearDown(self):
        if self.previous_path is None:
            os.environ.pop("CATALOG_DB_PATH", None)
        else:
            os.environ["CATALOG_DB_PATH"] = self.previous_path
        self.tmp.cleanup()

    def test_lookup_uses_only_confirmed_imdb_id_and_preserves_supplemental_fields(self):
        calls = []

        def fake_get(path, params):
            calls.append((path, params))
            return {
                "title": "Example Show",
                "year": 2020,
                "ids": {"imdb": "tt1234567"},
                "description": "MDBList description",
                "score": 82,
                "ratings": {"imdb": {"rating": 8.4}},
                "streams": [{"source": "Example"}],
                "watch_providers": [{"name": "Example Provider"}],
                "seasons": [{"number": 1, "episode_count": 12}],
                "last_episode": {"title": "Finale"},
            }

        result = find_mdblist_metadata(
            {"media_type": "series", "imdb_id": "tt1234567"},
            fetch_fn=fake_get,
        )

        self.assertEqual(calls, [("/imdb/show/tt1234567/", {})])
        self.assertEqual(result["status"], "found")
        self.assertEqual(result["imdb_rating"], 8.4)
        self.assertEqual(result["score"], 82)
        self.assertEqual(result["streams"][0]["source"], "Example")
        self.assertEqual(result["seasons"][0]["episode_count"], 12)
        self.assertNotIn("episodes", result)

    def test_lookup_rejects_a_different_returned_identity(self):
        with self.assertRaises(MDBListEnrichmentError):
            find_mdblist_metadata(
                {"media_type": "movie", "imdb_id": "tt1234567"},
                fetch_fn=lambda path, params: {
                    "title": "Wrong movie",
                    "ids": {"imdb": "tt7654321"},
                },
            )

    def test_enrichment_persists_metadata_without_replacing_tmdb(self):
        catalog_db.update_title_metadata(
            "series", "tt1234567",
            {"tmdb": {"title": "TMDB title", "overview": "TMDB overview"}},
        )

        summary = enrich_mdblist_metadata(
            fetch_fn=lambda path, params: {
                "title": "MDBList title",
                "ids": {"imdb": "tt1234567"},
                "ratings": [{"source": "IMDb", "value": "8.1/10"}],
                "watch_providers": [{"name": "Provider"}],
            },
        )

        self.assertEqual(summary["enriched"], 1)
        item = catalog_db.get_catalog_titles("series")[0]
        self.assertEqual(item["metadata"]["tmdb"]["title"], "TMDB title")
        self.assertEqual(item["metadata"]["mdblist"]["title"], "MDBList title")
        self.assertEqual(item["metadata"]["mdblist"]["imdb_rating"], 8.1)

        # A second run must reuse the persisted result without contacting MDBList.
        second = enrich_mdblist_metadata(
            fetch_fn=lambda path, params: self.fail("unexpected MDBList request"),
        )
        self.assertEqual(second["scanned"], 0)
        self.assertEqual(second["skipped_cached"], 1)

    def test_no_match_result_is_persisted_and_not_retried_by_default(self):
        calls = []

        def not_found(path, params):
            calls.append(path)
            from mdblist_enricher import MDBListNotFound
            raise MDBListNotFound("not found")

        first = enrich_mdblist_metadata(fetch_fn=not_found)
        self.assertEqual(first["no_match"], 1)
        self.assertEqual(len(calls), 1)

        item = catalog_db.get_catalog_titles("series")[0]
        self.assertEqual(item["metadata"]["mdblist"]["status"], "no_match")

        second = enrich_mdblist_metadata(
            fetch_fn=lambda path, params: self.fail("no-match result was retried"),
        )
        self.assertEqual(second["scanned"], 0)
        self.assertEqual(second["skipped_cached"], 1)

    def test_missing_api_key_disables_optional_enrichment(self):
        previous = os.environ.pop("MDBLIST_API_KEY", None)
        try:
            summary = enrich_mdblist_metadata()
        finally:
            if previous is not None:
                os.environ["MDBLIST_API_KEY"] = previous
        self.assertTrue(summary["disabled"])
        self.assertEqual(summary["scanned"], 0)


if __name__ == "__main__":
    unittest.main()
