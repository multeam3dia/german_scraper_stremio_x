"""Tests for TMDB metadata enrichment of verified catalog titles."""

import os
import tempfile
import unittest

import catalog_db
from metadata_enricher import enrich_catalog_metadata


class MetadataEnricherTests(unittest.TestCase):
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

    def test_enrichment_adds_localized_metadata_and_canonical_genres(self):
        catalog_db.record_discovery(
            "series", "tt1234567", "serienstream", "Example Show",
            year=2020,
            metadata={"poster_url": "https://provider.example/poster.jpg",
                      "provider_genres": ["GerDub"]},
        )

        def fake_get(path, params):
            if path == "/genre/tv/list":
                return {"genres": [
                    {"id": 18, "name": "Drama"},
                    {"id": 35, "name": "Komödie"},
                ]}
            if path == "/find/tt1234567":
                return {"tv_results": [{
                    "id": 42,
                    "name": "Beispielserie",
                    "original_name": "Example Show",
                    "first_air_date": "2020-01-01",
                    "overview": "Eine Beispielbeschreibung.",
                    "poster_path": "/poster.jpg",
                    "backdrop_path": "/backdrop.jpg",
                    "genre_ids": [18],
                    "vote_average": 8.2,
                    "original_language": "en",
                }]}
            raise AssertionError(f"Unexpected TMDB path: {path}")

        summary = enrich_catalog_metadata(search_fn=fake_get)
        self.assertEqual(summary["enriched"], 1)
        self.assertEqual(summary["errors"], 0)

        item = catalog_db.get_catalog_titles("series")[0]
        self.assertEqual(item["metadata"]["tmdb"]["title"], "Beispielserie")
        self.assertEqual(
            item["metadata"]["tmdb"]["poster_url"],
            "https://image.tmdb.org/t/p/w500/poster.jpg",
        )
        self.assertEqual(item["genres"], ["Drama"])
        self.assertEqual(item["metadata"]["provider_genres"], ["GerDub"])


if __name__ == "__main__":
    unittest.main()
