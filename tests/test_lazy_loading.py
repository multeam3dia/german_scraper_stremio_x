"""Regression tests for the lazy provider crawl and structure cache."""

import os
import tempfile
import unittest

import catalog_db
from providers.base import BaseProvider, ProviderTitle
from providers.crawler import crawl_provider


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
