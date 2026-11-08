"""Regression tests for provider adapter normalization."""

import unittest
from unittest.mock import patch

from aniworld.search import GENRE_FALLBACK
from providers.aniworld import AniWorldProvider


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


if __name__ == "__main__":
    unittest.main()
