"""Tests for the additive GSX canonical identity store."""
import os
import tempfile
import unittest

from catalog_identity import (
    IdentityConflictError,
    add_external_id,
    create_title,
    get_title,
    get_title_by_external_id,
    get_title_by_provider_url,
    link_provider_title,
    init_identity_schema,
)


class CatalogIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.previous_path = os.environ.get("CATALOG_DB_PATH")
        os.environ["CATALOG_DB_PATH"] = os.path.join(self.temp_dir.name, "catalog.sqlite3")
        init_identity_schema()

    def tearDown(self):
        if self.previous_path is None:
            os.environ.pop("CATALOG_DB_PATH", None)
        else:
            os.environ["CATALOG_DB_PATH"] = self.previous_path
        self.temp_dir.cleanup()

    def test_create_title_without_imdb_id(self):
        title_id = create_title("series", "Example Anime", year=2024)
        self.assertRegex(title_id, r"^gsx:series:[0-9a-f]{32}$")
        record = get_title(title_id)
        self.assertEqual(record["title"], "Example Anime")
        self.assertEqual(record["year"], 2024)
        self.assertEqual(record["external_ids"], {})

    def test_external_ids_resolve_to_same_canonical_title(self):
        title_id = create_title("series", "Example Anime")
        add_external_id(title_id, "tmdb", "12345", source="tmdb", confidence=0.99)
        add_external_id(title_id, "mal", "6789", source="mdblist", verified=True)
        by_tmdb = get_title_by_external_id("series", "tmdb", "12345")
        by_mal = get_title_by_external_id("series", "mal", "6789")
        self.assertEqual(by_tmdb["title_id"], title_id)
        self.assertEqual(by_mal["title_id"], title_id)
        self.assertTrue(by_mal["external_ids"]["mal"]["verified"])

    def test_conflicting_external_id_is_rejected(self):
        first = create_title("series", "First")
        second = create_title("series", "Second")
        add_external_id(first, "tmdb", "42")
        with self.assertRaises(IdentityConflictError):
            add_external_id(second, "tmdb", "42")
        self.assertEqual(
            get_title_by_external_id("series", "tmdb", "42")["title_id"], first
        )

    def test_provider_url_links_to_canonical_title(self):
        title_id = create_title("series", "Linked Anime")
        link_provider_title(
            title_id, "aniworld", "https://aniworld.to/anime/stream/linked-anime",
            provider_title="Linked Anime",
        )
        record = get_title_by_provider_url(
            "aniworld", "https://aniworld.to/anime/stream/linked-anime"
        )
        self.assertEqual(record["title_id"], title_id)
        self.assertEqual(record["title"], "Linked Anime")

    def test_provider_url_cannot_be_reassigned_to_another_title(self):
        first = create_title("series", "First")
        second = create_title("series", "Second")
        url = "https://aniworld.to/anime/stream/shared"
        link_provider_title(first, "aniworld", url)
        with self.assertRaises(IdentityConflictError):
            link_provider_title(second, "aniworld", url)

    def test_media_type_must_match_gsx_id(self):
        with self.assertRaises(ValueError):
            create_title("movie", "Wrong type", title_id="gsx:series:" + "a" * 32)

    def test_legacy_catalog_tables_are_not_replaced(self):
        import sqlite3
        path = os.environ["CATALOG_DB_PATH"]
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE legacy_sentinel(value TEXT)")
            db.execute("INSERT INTO legacy_sentinel VALUES ('preserved')")
        init_identity_schema()
        with sqlite3.connect(path) as db:
            value = db.execute("SELECT value FROM legacy_sentinel").fetchone()[0]
        self.assertEqual(value, "preserved")


if __name__ == "__main__":
    unittest.main()
