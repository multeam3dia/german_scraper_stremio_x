"""Tests for the verified SQLite Stremio catalog reader."""

import os
import tempfile
import unittest

import catalog_db


class CatalogDatabaseTests(unittest.TestCase):
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

    def test_catalog_filters_by_type_search_genre_and_pagination(self):
        # Legacy/playback-only film records must not create a film catalog.
        catalog_db.record_discovery(
            "movie", "tt1234567", "filmpalast", "The Action Film",
            year=2020, genres=["Action"],
            metadata={"poster_url": "https://img.example/action.jpg"},
        )
        catalog_db.record_discovery(
            "movie", "tt2345678", "megakino", "A Comedy Film",
            year=2021, genres=["Comedy"],
        )
        catalog_db.record_discovery(
            "series", "tt3456789", "aniworld", "Action Show",
            genres=["Action"],
        )
        catalog_db.record_discovery(
            "series", "tt4567890", "serienstream", "Comedy Show",
            genres=["Comedy"],
        )

        self.assertEqual(catalog_db.get_catalog_media_types(), ("series",))
        self.assertEqual(catalog_db.get_catalog_titles("movie"), [])
        self.assertEqual(
            [item["title"] for item in catalog_db.get_catalog_titles("series", genre="Action")],
            ["Action Show"],
        )
        self.assertEqual(
            [item["title"] for item in catalog_db.get_catalog_titles("series", search="comedy")],
            ["Comedy Show"],
        )
        self.assertEqual(len(catalog_db.get_catalog_titles("series")), 2)
        page = catalog_db.get_catalog_titles("series", skip=1, limit=1)
        self.assertEqual(len(page), 1)
        self.assertIn(page[0]["title"], {"Action Show", "Comedy Show"})

    def test_playback_only_discoveries_do_not_activate_or_export_catalog_titles(self):
        catalog_db.record_discovery(
            "movie", "tt1234567", "megakino", "Playback-only Film"
        )
        with catalog_db._connect() as db:
            title = db.execute(
                "SELECT catalog_active FROM titles WHERE media_type='movie' AND imdb_id='tt1234567'"
            ).fetchone()
            queued = db.execute(
                "SELECT COUNT(*) FROM sync_outbox WHERE media_type='movie' AND imdb_id='tt1234567'"
            ).fetchone()[0]

        self.assertEqual(title["catalog_active"], 0)
        self.assertEqual(queued, 0)
        self.assertEqual(catalog_db.get_catalog_titles("movie"), [])
        self.assertEqual(catalog_db.get_metadata_candidates(force=True), [])

    def test_catalog_excludes_titles_without_available_catalog_provider(self):
        catalog_db.record_discovery(
            "series", "tt1234567", "aniworld", "Only Source"
        )
        with catalog_db._connect() as db:
            db.execute(
                "UPDATE provider_entries SET availability_status='missing' "
                "WHERE media_type='series' AND imdb_id='tt1234567'"
            )
        self.assertEqual(catalog_db.get_catalog_titles("series"), [])

    def test_metadata_update_merges_instead_of_erasing_provider_data(self):
        catalog_db.record_discovery(
            "series", "tt1234567", "serienstream", "Example Show",
            metadata={"poster_url": "https://img.example/poster.jpg",
                      "provider_genres": ["GerDub"]},
        )
        updated = catalog_db.update_title_metadata(
            "series",
            "tt1234567",
            {"tmdb": {"title": "Beispielserie", "year": 2022}},
            genres=["Drama"],
        )
        self.assertTrue(updated)
        item = catalog_db.get_catalog_titles("series")[0]
        self.assertEqual(item["metadata"]["poster_url"], "https://img.example/poster.jpg")
        self.assertEqual(item["metadata"]["provider_genres"], ["GerDub"])
        self.assertEqual(item["metadata"]["tmdb"]["title"], "Beispielserie")
        self.assertEqual(item["genres"], ["Drama"])


if __name__ == "__main__":
    unittest.main()
