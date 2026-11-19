"""Regression tests for provider adapter normalization."""

import unittest
from unittest.mock import patch

from providers.aniworld import AniWorldProvider
from providers.base import ProviderTitle
from providers.serienstream import SerienstreamProvider


class AniWorldAdapterTests(unittest.TestCase):
    def test_catalog_references_use_normalized_provider_fields(self):
        provider = AniWorldProvider()
        index_items = [{
            "url": "https://aniworld.to/anime/stream/example",
            "title": "Example",
            "poster_url": "https://img.example/poster.jpg",
        }]
        with patch(
            "providers.aniworld.fetch_aniworld_index",
            return_value=index_items,
        ) as fetch_index:
            item = next(provider.iter_catalog())

        fetch_index.assert_called_once_with(session=None)
        self.assertEqual(item.title, "Example")
        self.assertEqual(item.provider, "aniworld")
        self.assertEqual(item.provider_genres, [])
        self.assertEqual(item.poster_url, "https://img.example/poster.jpg")
        self.assertTrue(provider.catalog_scan_complete)

    def test_empty_az_index_is_not_considered_a_complete_scan(self):
        provider = AniWorldProvider()
        with patch(
            "providers.aniworld.fetch_aniworld_index",
            return_value=[],
        ):
            with self.assertRaises(RuntimeError):
                list(provider.iter_catalog())

        self.assertFalse(provider.catalog_scan_complete)


    def test_detail_classifies_movie_only_structure_as_movie(self):
        class MovieCollection:
            are_movies = True

        class FakeSeries:
            imdb = None
            title = "Bright: Samurai Soul"
            release_year = "2021"
            poster_url = None
            description = None
            genres = []
            has_movies = True
            seasons = [MovieCollection()]
            country = None
            age_rating = None
            rating = None
            directors = []
            actors = []
            producer = None

            def __init__(self, url, session=None):
                pass

        with patch("providers.aniworld.AniworldSeries", FakeSeries):
            item = AniWorldProvider().get_title(
                "https://aniworld.to/anime/stream/bright-samurai-soul"
            )
        self.assertEqual(item.media_type, "movie")
        self.assertTrue(item.metadata["standalone_movie"])

    def test_detail_keeps_mixed_series_and_movie_collection_as_series(self):
        class RegularSeason:
            are_movies = False

        class MovieCollection:
            are_movies = True

        class FakeSeries:
            imdb = None
            title = "Example"
            release_year = "2018"
            poster_url = None
            description = None
            genres = []
            has_movies = True
            seasons = [RegularSeason(), MovieCollection()]
            country = None
            age_rating = None
            rating = None
            directors = []
            actors = []
            producer = None

            def __init__(self, url, session=None):
                pass

        with patch("providers.aniworld.AniworldSeries", FakeSeries):
            item = AniWorldProvider().get_title(
                "https://aniworld.to/anime/stream/example"
            )
        self.assertEqual(item.media_type, "series")
        self.assertFalse(item.metadata["standalone_movie"])

    def test_detail_does_not_guess_movie_when_structure_is_empty(self):
        class FakeSeries:
            imdb = None
            title = "Unknown"
            release_year = None
            poster_url = None
            description = None
            genres = []
            has_movies = True
            seasons = []
            country = None
            age_rating = None
            rating = None
            directors = []
            actors = []
            producer = None

            def __init__(self, url, session=None):
                pass

        with patch("providers.aniworld.AniworldSeries", FakeSeries):
            item = AniWorldProvider().get_title(
                "https://aniworld.to/anime/stream/unknown"
            )
        self.assertEqual(item.media_type, "series")


class AniWorldLazyStructureTests(unittest.TestCase):
    def test_structure_loading_uses_only_season_page_title_cache(self):
        class FakeEpisode:
            url = "https://aniworld.to/anime/stream/example/staffel-1/episode-1"
            episode_number = 1
            is_movie = False
            _AniworldEpisode__title_de = "Pilot"
            _AniworldEpisode__title_en = "Pilot"

            @property
            def title_de(self):
                raise AssertionError("title property must not fetch episode page")

            @property
            def title_en(self):
                raise AssertionError("title property must not fetch episode page")

            @property
            def provider_data(self):
                raise AssertionError("provider data must wait until playback")

        class FakeSeason:
            url = "https://aniworld.to/anime/stream/example/staffel-1"
            season_number = 1
            are_movies = False
            episodes = [FakeEpisode()]

        class FakeSeries:
            def __init__(self, url):
                self.seasons = [FakeSeason()]

        title = ProviderTitle(
            provider="aniworld",
            media_type="series",
            title="Example",
            url="https://aniworld.to/anime/stream/example",
        )
        with patch("providers.aniworld.AniworldSeries", FakeSeries):
            seasons = AniWorldProvider().get_seasons(title)

        self.assertEqual(seasons[0].episodes[0].title, "Pilot")
        self.assertEqual(seasons[0].episodes[0].title_original, "Pilot")
        self.assertEqual(seasons[0].episodes[0].variants, [])


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
