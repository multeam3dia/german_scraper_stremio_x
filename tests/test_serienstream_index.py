"""Unit tests for the Serienstream alphabetical index parser."""

import unittest

from aniworld.search import _parse_serienstream_index


class SerienstreamIndexParserTests(unittest.TestCase):
    def test_extracts_series_titles_and_posters(self):
        html = """
        <h1>Alle Einträge</h1>
        <a href="/serie/alpha-show">
          <img alt="Alpha Show" data-src="/media/alpha.jpg">
        </a>
        <a href="https://serienstream.to/serie/beta-show" title="Beta Show">
          Beta Show
        </a>
        """
        results = _parse_serienstream_index(
            html, "https://serienstream.to/serien"
        )

        self.assertEqual(
            [item["title"] for item in results],
            ["Alpha Show", "Beta Show"],
        )
        self.assertEqual(
            results[0]["url"],
            "https://serienstream.to/serie/alpha-show",
        )
        self.assertEqual(
            results[0]["poster_url"],
            "https://serienstream.to/media/alpha.jpg",
        )

    def test_deduplicates_season_links_to_series(self):
        html = """
        <a href="/serie/example/staffel-1">Example</a>
        <a href="/serie/example/staffel-2">Example, Staffel 2</a>
        <a href="/serie/another">Another</a>
        """
        results = _parse_serienstream_index(
            html, "https://serienstream.to/serien"
        )

        self.assertEqual(
            [item["url"] for item in results],
            [
                "https://serienstream.to/serie/example",
                "https://serienstream.to/serie/another",
            ],
        )

    def test_ignores_non_series_links_and_empty_anchors(self):
        html = """
        <a href="/genre/action">Action</a>
        <a href="/serie/no-title"></a>
        <a href="/faq/example">FAQ</a>
        """
        self.assertEqual(
            _parse_serienstream_index(html, "https://serienstream.to/serien"),
            [],
        )


if __name__ == "__main__":
    unittest.main()
