"""Tests for the SQLite-backed Stremio catalog routes."""

import asyncio
import os
import tempfile
import unittest
from unittest.mock import patch

import catalog_db
import app
from catalog_identity import create_title, link_provider_title


class CatalogRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.previous_path = os.environ.get("CATALOG_DB_PATH")
        os.environ["CATALOG_DB_PATH"] = os.path.join(self.tmp.name, "catalog.sqlite3")
        catalog_db.init_db()
        catalog_db.record_discovery(
            "series",
            "tt1234567",
            "serienstream",
            "Provider title",
            year=2020,
            metadata={
                "poster_url": "https://provider.example/poster.jpg",
                "description": "Provider description",
            },
            genres=["Action"],
        )
        catalog_db.update_title_metadata(
            "series",
            "tt1234567",
            {
                "tmdb": {
                    "title": "Localized title",
                    "year": 2021,
                    "poster_url": "https://image.tmdb.org/t/p/w500/poster.jpg",
                    "backdrop_url": "https://image.tmdb.org/t/p/w1280/backdrop.jpg",
                    "overview": "TMDB overview",
                },
                "mdblist": {
                    "status": "found",
                    "imdb_rating": 8.4,
                    "description": "MDBList fallback description",
                },
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
        response = app.get_catalog("series", "german_scraper_shows", genre="Action")
        self.assertEqual(len(response["metas"]), 1)
        meta = response["metas"][0]
        self.assertEqual(meta["id"], "tt1234567")
        self.assertEqual(meta["name"], "Localized title")
        self.assertEqual(meta["poster"], "https://image.tmdb.org/t/p/w500/poster.jpg")
        self.assertEqual(meta["background"], "https://image.tmdb.org/t/p/w1280/backdrop.jpg")
        self.assertEqual(meta["description"], "TMDB overview")
        self.assertEqual(meta["imdbRating"], "8.4")
        self.assertEqual(meta["genres"], ["Action"])

    def test_path_encoded_extra_arguments_are_parsed(self):
        response = app.get_catalog_with_extra(
            "series", "german_scraper_shows", "genre=Action&skip=0&search=Localized"
        )
        self.assertEqual([meta["id"] for meta in response["metas"]], ["tt1234567"])

    def test_local_catalog_can_be_disabled_without_disabling_live_resources(self):
        with patch.dict(os.environ, {"ENABLE_LOCAL_CATALOG": "false"}):
            manifest = app._manifest_response()
            catalog = app.get_catalog("series", "german_scraper_shows")

        self.assertEqual(manifest["catalogs"], [])
        self.assertIn("stream", manifest["resources"])
        self.assertIn("meta", manifest["resources"])
        self.assertEqual(catalog, {"metas": []})

    def test_sqlite_failure_does_not_break_app_startup_or_manifest(self):
        with patch.dict(os.environ, {"ENABLE_LOCAL_CATALOG": "true"}), patch(
            "app.init_db", side_effect=RuntimeError("database unavailable")
        ), patch(
            "app.list_genres", side_effect=RuntimeError("database unavailable")
        ):
            asyncio.run(app.initialize_catalog_database())
            manifest = app._manifest_response()

        self.assertEqual(len(manifest["catalogs"]), 1)
        self.assertEqual(manifest["catalogs"][0]["type"], "series")
        self.assertIn("stream", manifest["resources"])

    def test_meta_route_uses_cached_mdblist_fields_without_overwriting_metadata(self):
        canonical_meta = {
            "id": "tt1234567",
            "type": "series",
            "name": "Canonical series title",
            "videos": [{
                "id": "tt1234567:1:1",
                "title": "Pilot",
                "season": 1,
                "episode": 1,
            }],
        }
        with patch("app.get_series_meta", return_value={"meta": canonical_meta}):
            response = app.get_meta("series", "tt1234567")

        meta = response["meta"]
        self.assertEqual(meta["name"], "Canonical series title")
        self.assertEqual(meta["imdbRating"], "8.4")
        self.assertEqual(meta["description"], "MDBList fallback description")
        self.assertEqual(meta["videos"], canonical_meta["videos"])

    def test_meta_fallback_preserves_canonical_episode_list(self):
        canonical_meta = {
            "id": "tt7654321",
            "type": "series",
            "name": "Example",
            "videos": [{
                "id": "tt7654321:1:1",
                "title": "Pilot",
                "season": 1,
                "episode": 1,
            }],
        }
        with patch("app.get_series_meta", side_effect=RuntimeError("catalog unavailable")), patch(
            "app.get_metadata", return_value=canonical_meta
        ):
            response = app.get_meta("series", "tt7654321")

        self.assertEqual(response["meta"]["videos"], canonical_meta["videos"])



    def test_gsx_titles_without_imdb_are_listed_in_separate_catalog(self):
        title_id = create_title(
            "series", "Series without IMDb", year=2024,
            metadata={"description": "Canonical description"},
        )
        link_provider_title(
            title_id, "aniworld",
            "https://aniworld.to/anime/stream/no-imdb",
            provider_title="Series without IMDb",
        )
        manifest = app._manifest_response()
        gsx_catalogs = [
            item for item in manifest["catalogs"]
            if item["id"] == "german_scraper_gsx_shows"
        ]
        self.assertEqual(len(gsx_catalogs), 1)
        response = app.get_catalog("series", "german_scraper_gsx_shows")
        self.assertEqual(len(response["metas"]), 1)
        self.assertEqual(response["metas"][0]["id"], title_id)
        self.assertEqual(response["metas"][0]["name"], "Series without IMDb")
        self.assertEqual(
            response["metas"][0]["description"], "Canonical description"
        )

    def test_manifest_accepts_canonical_gsx_ids(self):
        manifest = app._manifest_response()
        self.assertIn("gsx:series:", manifest["idPrefixes"])
        self.assertIn("gsx:movie:", manifest["idPrefixes"])

    def test_gsx_series_meta_is_routed_to_canonical_provider_metadata(self):
        title_id = create_title("series", "Series without IMDb")
        link_provider_title(
            title_id, "aniworld",
            "https://aniworld.to/anime/stream/example",
            provider_title="Series without IMDb",
        )
        expected = {
            "meta": {
                "id": title_id, "type": "series",
                "name": "Series without IMDb", "videos": [],
            }
        }
        with patch("app.get_gsx_series_meta", return_value=expected) as resolver:
            response = app.get_meta("series", title_id)
        self.assertEqual(response, expected)
        resolver.assert_called_once_with(title_id)

    def test_gsx_series_stream_route_uses_canonical_provider_resolver(self):
        title_id = create_title("series", "Series without IMDb")
        episode_id = f"{title_id}:1:2"
        expected = [{"name": "Direct provider stream", "url": "https://cdn.example/episode.mp4"}]
        with patch("app.get_gsx_series_streams_for_episode", return_value=expected) as resolver:
            response = app.get_stream("series", episode_id)
        self.assertEqual(response["streams"], expected)
        resolver.assert_called_once_with(title_id, 1, 2)

    def test_manifest_advertises_only_configured_catalog_sources(self):
        manifest = app._manifest_response()
        self.assertIn("meta", manifest["resources"])
        self.assertNotIn("player", manifest["resources"])
        player_manifest = app._manifest_response(enable_player_events=True)
        self.assertIn(
            "player",
            [
                resource if isinstance(resource, str) else resource.get("name")
                for resource in player_manifest["resources"]
            ],
        )
        self.assertEqual(len(manifest["catalogs"]), 1)
        show_catalog = manifest["catalogs"][0]
        self.assertEqual(show_catalog["id"], "german_scraper_shows")
        self.assertEqual(show_catalog["type"], "series")
        extra_names = [item["name"] for item in show_catalog["extra"]]
        self.assertIn("search", extra_names)
        self.assertIn("skip", extra_names)
        self.assertIn("genre", extra_names)
        self.assertEqual(show_catalog["genres"], ["Action"])


if __name__ == "__main__":
    unittest.main()
