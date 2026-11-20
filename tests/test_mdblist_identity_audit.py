"""Offline tests for the read-only MDBList identity audit."""
import unittest

from tools.mdblist_identity_audit import (
    _candidate_rows,
    _imdb_id,
    _summary,
    normalize,
    similarity,
    summarize,
)


class MDBListIdentityAuditTests(unittest.TestCase):
    def test_normalization_preserves_non_latin_titles(self):
        self.assertEqual(normalize("  TwoCar  "), "twocar")
        self.assertEqual(similarity("TwoCar", "Two Car"), 1.0)
        self.assertGreater(similarity("千と千尋の神隠し", "千と千尋の神隠し"), 0.99)

    def test_candidate_rows_accepts_documented_response_shapes(self):
        self.assertEqual(len(_candidate_rows([{"title": "A"}])), 1)
        self.assertEqual(len(_candidate_rows({"search": [{"title": "A"}]})), 1)
        self.assertEqual(len(_candidate_rows({"title": "A", "ids": {"imdb": "tt1234567"}})), 1)
        self.assertEqual(_candidate_rows({"unexpected": True}), [])

    def test_imdb_id_extraction_supports_common_shapes(self):
        self.assertEqual(_imdb_id({"ids": {"imdb": "tt1234567"}}), "tt1234567")
        self.assertEqual(_imdb_id({"imdb_id": "tt7654321"}), "tt7654321")
        self.assertIsNone(_imdb_id({"imdb": "not-an-imdb-id"}))

    def test_summary_reports_exact_lookup_and_search_hits_separately(self):
        report = summarize([{
            "expected_imdb_ids": ["tt1234567", "tt7654321"],
            "exact_lookup": {
                "tt1234567": {"show": {"returned_ids": ["tt1234567"]}},
                "tt7654321": {"movie": {"returned_ids": ["tt7654321"]}},
            },
            "search": {
                "show": {"expected_id_hits": ["tt1234567"]},
                "movie": {"expected_id_hits": []},
            },
        }])
        self.assertEqual(report["expected_imdb_id_count"], 2)
        self.assertEqual(report["exact_imdb_lookup_hits"], 2)
        self.assertEqual(report["title_search_expected_id_hits"], 1)
        self.assertEqual(report["show_search_expected_id_hits"], 1)
        self.assertEqual(report["movie_search_expected_id_hits"], 0)

    def test_summary_does_not_count_title_only_as_identity_hit(self):
        report = summarize([{
            "expected_imdb_ids": ["tt1234567"],
            "exact_lookup": {},
            "search": {
                "show": {"candidate_count": 1, "expected_id_hits": []},
                "movie": {"candidate_count": 1, "expected_id_hits": []},
            },
        }])
        self.assertEqual(report["exact_imdb_lookup_hits"], 0)
        self.assertEqual(report["title_search_expected_id_hits"], 0)

    def test_summary_of_item_exposes_metadata_field_inventory(self):
        item = {
            "title": "Example",
            "year": 2020,
            "ids": {"imdb": "tt1234567"},
            "credits": {"cast": [{"name": "Actor"}]},
            "seasons": [{"episodes": [{"title": "Episode 1"}]}],
        }
        result = _summary(item, "Example")
        self.assertEqual(result["imdb_id"], "tt1234567")
        self.assertEqual(result["title_similarity"], 1.0)
        self.assertIn("credits.cast[].name", result["available_fields"])
        self.assertIn("seasons[].episodes[].title", result["available_fields"])


if __name__ == "__main__":
    unittest.main()
