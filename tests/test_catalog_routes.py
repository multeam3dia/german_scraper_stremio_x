"""Tests for the SQLite-backed Stremio catalog routes."""

import asyncio
import os
import tempfile
import unittest

import catalog_db
import app


class CatalogRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.previous_path = os.environ.get("CATALOG_DB_PATH")
        os.environ["CATALOG_DB_PATH"] = os.path.join(self.tmp.name, "catalog.sqlite3")
        catalog_db.init_db()
        catalog_db.record_discovery(
            "movie",
            "tt1234567",
            "filmpalast",
            "Provider title",
            year=2020,
            metadata={
                "poster_url": "https://provider.example/poster.jpg",
                "description": "Provider description",
            },
            genres=["Action"],
        )
        catalog_db.update_title_metadata(
            "movie",
            "tt1234567",
            {
                "tmdb": {
                    "title": "Localized title",
                    "year": 2021,
                    "poster_url": "https://image.tmdb.org/t/p/w500/poster.jpg",
                    "backdrop_url": "https://image.tmdb.org/t/p/w1280/backdrop.jpg",
                    "overview": "TMDB overview",
                }
            },
            genres=["Action"],
        )

    def tearDown(self):
        if self.previous_path is None:
            os.environ.pop("CATALOG_DB_PATH", None)
        else:
            os.environ["CATALOG_DB_PATH"] = self.previous_path
        self.tmp.cleanup()

    def test_catalog_route_maps_sqlite_record_to_stremio_meta(self):
        response = app.get_catalog("movie", "german_scraper_movies", genre="Action")
        self.assertEqual(len(response["metas"]), 1)
        meta = response["metas"][0]
        self.assertEqual(meta["id"], "tt1234567")
        self.assertEqual(meta["name"], "Localized title")
        self.assertEqual(meta["poster"], "https://image.tmdb.org/t/p/w500/poster.jpg")
        self.assertEqual(meta["background"], "https://image.tmdb.org/t/p/w1280/backdrop.jpg")
        self.assertEqual(meta["description"], "TMDB overview")
        self.assertEqual(meta["genres"], ["Action"])

    def test_path_encoded_extra_arguments_are_parsed(self):
        response = app.get_catalog_with_extra(
            "movie", "german_scraper_movies", "genre=Action&skip=0&search=Localized"
        )
        self.assertEqual([meta["id"] for meta in response["metas"]], ["tt1234567"])

    def test_manifest_advertises_search_pagination_and_available_genres(self):
        manifest = asyncio.run(app.get_manifest())
        self.assertIn("meta", manifest["resources"])
        movie_catalog = next(
            item for item in manifest["catalogs"]
            if item["id"] == "german_scraper_movies"
        )
        extra_names = [item["name"] for item in movie_catalog["extra"]]
        self.assertIn("search", extra_names)
        self.assertIn("skip", extra_names)
        self.assertIn("genre", extra_names)
        self.assertEqual(movie_catalog["genres"], ["Action"])


if __name__ == "__main__":
    unittest.main()
