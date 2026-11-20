"""Offline tests for the read-only MDBList identity audit."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.mdblist_identity_audit import (
    run_audit,
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
        # MDBList search responses use `ids.imdbid`, unlike detail responses.
        self.assertEqual(_imdb_id({"ids": {"imdbid": "tt1234567"}}), "tt1234567")
        self.assertEqual(_imdb_id({"imdb_id": "tt7654321"}), "tt7654321")
        self.assertIsNone(_imdb_id({"imdb": "not-an-imdb-id"}))

    def test_uploaded_anime_sample_fixture_has_210_unique_series_identities(self):
        fixture_path = (
            Path(__file__).resolve().parent
            / "fixtures"
            / "aniworld_anime_identity_sample.json"
        )
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        entries = fixture["entries"]
        self.assertEqual(len(entries), 210)
        self.assertTrue(all(entry["media_type"] == "series" for entry in entries))
        ids = [entry["imdb_ids"][0] for entry in entries]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(value.startswith("tt") for value in ids))

    def test_run_audit_accepts_single_imdb_id_and_series_only_fixture(self):
        fixture = {
            "provider": "AniWorld",
            "entries": [{
                "media_type": "series",
                "imdb_id": "tt1234567",
                "title": "Example Series",
            }],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fixture.json"
            path.write_text(json.dumps(fixture), encoding="utf-8")
            responses = [
                ([{"title": "Example Series", "ids": {"imdbid": "tt1234567"}}], {}),
                ({"title": "Example Series", "ids": {"imdb": "tt1234567"}}, {}),
            ]
            with patch.dict(os.environ, {"MDBLIST_API_KEY": "test-key"}):
                with patch(
                    "tools.mdblist_identity_audit._get_json",
                    side_effect=responses,
                ) as request:
                    report = run_audit(
                        fixture_path=path,
                        media_types=("show",),
                        pause=0,
                    )

        self.assertEqual(report["entries_scanned"], 1)
        self.assertEqual(report["media_types"], ["show"])
        self.assertEqual(report["requests"], 2)
        self.assertEqual(request.call_count, 2)
        self.assertEqual(report["summary"]["exact_imdb_lookup_hits"], 1)
        self.assertEqual(report["summary"]["show_search_expected_id_hits"], 1)

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
