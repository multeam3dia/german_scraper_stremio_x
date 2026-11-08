"""Regression tests for provider adapter normalization."""

import unittest
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()
