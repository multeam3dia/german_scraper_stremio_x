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

    def test_catalog_filters_by_type_search_and_genre(self):
        catalog_db.record_discovery(
            "movie", "tt1234567", "filmpalast", "The Action Film",
            year=2020, genres=["Action"],
            metadata={"poster_url": "https://img.example/action.jpg"},
        )
        catalog_db.record_discovery(
            "movie", "tt2345678", "megakino", "A Comedy",
            year=2021, genres=["Comedy"],
        )
        catalog_db.record_discovery(
            "series", "tt3456789", "aniworld", "Action Show",
            genres=["Action"],
        )

        self.assertEqual(
            [item["title"] for item in catalog_db.get_catalog_titles("movie", genre="Action")],
            ["The Action Film"],
        )
        self.assertEqual(
            [item["title"] for item in catalog_db.get_catalog_titles("movie", search="comedy")],
            ["A Comedy"],
        )
        self.assertEqual(len(catalog_db.get_catalog_titles("series")), 1)
        self.assertEqual(
            catalog_db.get_catalog_titles("movie", skip=1, limit=1)[0]["title"],
            "A Comedy",
        )

    def test_catalog_excludes_titles_without_available_provider(self):
        catalog_db.record_discovery(
            "movie", "tt1234567", "filmpalast", "Only Source"
        )
        with catalog_db._connect() as db:
            db.execute(
                "UPDATE provider_entries SET availability_status='missing' "
                "WHERE media_type='movie' AND imdb_id='tt1234567'"
            )
        self.assertEqual(catalog_db.get_catalog_titles("movie"), [])

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
