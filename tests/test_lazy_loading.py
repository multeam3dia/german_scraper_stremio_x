"""Regression tests for the lazy provider crawl and structure cache."""

import os
import tempfile
import unittest
from unittest.mock import patch

import catalog_db
from providers.base import (
    BaseProvider,
    ProviderEpisode,
    ProviderSeason,
    ProviderTitle,
)
from providers.crawler import crawl_provider
from providers.lazy import get_series_meta, get_series_streams_for_episode


class _CatalogOnlyProvider(BaseProvider):
    provider_id = "testprovider"
    supported_media_types = ("series",)
    catalog_scan_complete = True

    def __init__(self):
        self.seasons_requested = False

    def iter_catalog(self):
        yield ProviderTitle(
            provider=self.provider_id,
            media_type="series",
            title="Example",
            url="https://provider.example/series/example",
        )

    def get_title(self, url):
        return ProviderTitle(
            provider=self.provider_id,
            media_type="series",
            title="Example",
            url=url,
            imdb_id="tt1234567",
        )

    def get_seasons(self, title):
        self.seasons_requested = True
        raise AssertionError("initial catalog crawl must not load seasons")


class _LazyPlaybackProvider(BaseProvider):
    provider_id = "testprovider"
    supported_media_types = ("series",)
    seasons_calls = 0
    stream_calls = 0

    def iter_catalog(self):
        return iter(())

    def get_title(self, url):
        raise NotImplementedError

    def get_seasons(self, title):
        type(self).seasons_calls += 1
        return [
            ProviderSeason(
                provider=self.provider_id,
                url="https://provider.example/series/example/season-1",
                season_number=1,
                episodes=[
                    ProviderEpisode(
                        provider=self.provider_id,
                        url="https://provider.example/series/example/season-1/episode-1",
                        season_number=1,
                        episode_number=1,
                        title="Pilot",
                    )
                ],
            )
        ]

    def get_episode_streams(self, episode_url, display_title, binge_group=None):
        type(self).stream_calls += 1
        return [{"name": "Example Hoster", "url": "https://cdn.example/video.mp4"}]


class LazyLoadingTests(unittest.TestCase):
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

    def test_initial_crawl_records_title_without_loading_seasons(self):
        provider = _CatalogOnlyProvider()
        result = crawl_provider(provider)

        self.assertFalse(provider.seasons_requested)
        self.assertEqual(result["titles_recorded"], 1)
        entries = catalog_db.get_available_provider_entries("series", "tt1234567")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["provider_url"], "https://provider.example/series/example")

    def test_series_meta_keeps_canonical_episodes_without_catalog_entries(self):
        canonical_meta = {
            "id": "tt2468101",
            "type": "series",
            "name": "Example",
            "videos": [{
                "id": "cinemeta-video-id",
                "title": "Pilot",
                "season": 1,
                "episode": 1,
                "released": "2020-01-01",
            }],
        }
        with patch(
            "providers.lazy._canonical_meta", return_value=canonical_meta
        ), patch("providers.lazy.ensure_series_structure", return_value=[]):
            response = get_series_meta("tt2468101")

        video = response["meta"]["videos"][0]
        self.assertEqual(video["id"], "tt2468101:1:1")
        self.assertEqual(video["title"], "Pilot")
        self.assertEqual(video["released"], "2020-01-01")

    def test_empty_catalog_still_triggers_live_stream_search_every_time(self):
        expected_streams = [{
            "name": "Example Hoster",
            "url": "https://cdn.example/video.mp4",
        }]
        with patch(
            "providers.lazy.get_available_provider_entries",
            side_effect=AssertionError("live playback must not query the catalog"),
        ), patch(
            "scraper.get_series_streams", return_value=expected_streams
        ) as live_search:
            first = get_series_streams_for_episode("tt2468101", 1, 1)
            second = get_series_streams_for_episode("tt2468101", 1, 1)

        self.assertEqual(first, expected_streams)
        self.assertEqual(second, expected_streams)
        self.assertEqual(live_search.call_count, 2)
        live_search.assert_any_call("tt2468101", 1, 1)

    def test_series_meta_does_not_touch_sqlite_when_catalog_is_disabled(self):
        canonical_meta = {
            "id": "tt2468104",
            "type": "series",
            "name": "Example",
            "videos": [{
                "title": "Pilot",
                "season": 1,
                "episode": 1,
            }],
        }
        with patch.dict(os.environ, {"ENABLE_LOCAL_CATALOG": "false"}), patch(
            "providers.lazy._canonical_meta", return_value=canonical_meta
        ), patch(
            "providers.lazy.get_available_provider_entries",
            side_effect=AssertionError("disabled catalog must not be queried"),
        ):
            response = get_series_meta("tt2468104")

        self.assertEqual(response["meta"]["videos"][0]["id"], "tt2468104:1:1")

    def test_series_meta_survives_sqlite_failure(self):
        canonical_meta = {
            "id": "tt2468103",
            "type": "series",
            "name": "Example",
            "videos": [{
                "title": "Pilot",
                "season": 1,
                "episode": 1,
            }],
        }
        with patch(
            "providers.lazy._canonical_meta", return_value=canonical_meta
        ), patch(
            "providers.lazy.get_available_provider_entries",
            side_effect=RuntimeError("database unavailable"),
        ):
            response = get_series_meta("tt2468103")

        self.assertEqual(response["meta"]["videos"][0]["id"], "tt2468103:1:1")

    def test_series_meta_uses_cached_structure_only_as_a_fallback(self):
        catalog_db.record_discovery(
            "series",
            "tt2468102",
            "testprovider",
            "Example",
            provider_title="Example",
            provider_url="https://provider.example/series/example",
        )
        _LazyPlaybackProvider.seasons_calls = 0
        canonical_meta = {
            "id": "tt2468102",
            "type": "series",
            "name": "Example",
            "videos": [],
        }
        with patch.dict(
            "providers.lazy.PROVIDERS",
            {"testprovider": _LazyPlaybackProvider},
        ), patch("providers.lazy._canonical_meta", return_value=canonical_meta):
            response = get_series_meta("tt2468102")

        self.assertEqual(response["meta"]["videos"][0]["id"], "tt2468102:1:1")
        self.assertEqual(_LazyPlaybackProvider.seasons_calls, 1)

    def test_provider_structure_cache_has_persistent_freshness(self):
        catalog_db.record_discovery(
            "series",
            "tt7654321",
            "testprovider",
            "Another example",
            provider_url="https://provider.example/series/another",
        )
        self.assertFalse(
            catalog_db.is_provider_structure_fresh(
                "series", "tt7654321", "testprovider"
            )
        )
        catalog_db.mark_provider_structure_fetched(
            "series", "tt7654321", "testprovider"
        )
        self.assertTrue(
            catalog_db.is_provider_structure_fresh(
                "series", "tt7654321", "testprovider"
            )
        )


if __name__ == "__main__":
    unittest.main()
