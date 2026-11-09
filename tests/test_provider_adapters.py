"""Regression tests for provider adapter normalization."""

import unittest
from unittest.mock import patch

from aniworld.search import GENRE_FALLBACK
from providers.aniworld import AniWorldProvider
from providers.base import ProviderTitle
from providers.serienstream import SerienstreamProvider


class AniWorldAdapterTests(unittest.TestCase):
    def test_catalog_references_use_normalized_provider_genres_field(self):
        with patch(
            "providers.aniworld.fetch_genres",
            return_value=[{"slug": "drama", "name": "Drama"}],
        ), patch(
            "providers.aniworld.fetch_genre_animes",
            return_value={
                "results": [{
                    "url": "https://aniworld.to/anime/stream/example",
                    "title": "Example",
                    "poster_url": "https://img.example/poster.jpg",
                }],
                "has_more": False,
            },
        ):
            item = next(AniWorldProvider().iter_catalog())

        self.assertEqual(item.title, "Example")
        self.assertEqual(item.provider_genres, ["Drama"])

    def test_fallback_genre_index_is_not_considered_a_complete_scan(self):
        fallback = [
            {"name": name, "slug": slug} for name, slug in GENRE_FALLBACK
        ]
        provider = AniWorldProvider()
        with patch(
            "providers.aniworld.fetch_genres",
            return_value=fallback,
        ), patch(
            "providers.aniworld.fetch_genre_animes",
            return_value={"results": [], "has_more": False},
        ):
            list(provider.iter_catalog())

        self.assertFalse(provider.catalog_scan_complete)


class SerienstreamLazyStructureTests(unittest.TestCase):
    def test_structure_loading_does_not_read_episode_details(self):
        class FakeEpisode:
            url = "https://serienstream.to/serie/example/staffel-1/episode-1"
            episode_number = 1

            @property
            def title_de(self):
                raise AssertionError("episode title must not trigger an episode-page request")

            @property
            def title_en(self):
                raise AssertionError("episode title must not trigger an episode-page request")

            @property
            def provider_data(self):
                raise AssertionError("provider data must wait until playback")

        class FakeSeason:
            url = "https://serienstream.to/serie/example/staffel-1"
            season_number = 1
            episodes = [FakeEpisode()]

        class FakeSeries:
            def __init__(self, url):
                self.seasons = [FakeSeason()]

        title = ProviderTitle(
            provider="serienstream",
            media_type="series",
            title="Example",
            url="https://serienstream.to/serie/example",
        )
        with patch("providers.serienstream.SerienstreamSeries", FakeSeries):
            seasons = SerienstreamProvider().get_seasons(title)

        self.assertEqual(len(seasons), 1)
        self.assertEqual(len(seasons[0].episodes), 1)
        self.assertIsNone(seasons[0].episodes[0].title)
        self.assertEqual(seasons[0].episodes[0].variants, [])


if __name__ == "__main__":
    unittest.main()
