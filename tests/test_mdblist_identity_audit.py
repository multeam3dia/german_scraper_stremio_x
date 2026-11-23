"""Offline tests for the read-only MDBList identity audit."""
import json
import re
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from identity_resolver import find_tmdb_match
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

    def test_user_verified_ground_truth_fixture_integrity(self):
        fixture_path = (
            Path(__file__).resolve().parent
            / "fixtures"
            / "aniworld_identity_ground_truth.json"
        )
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        entries = fixture["entries"]
        self.assertEqual(len(entries), 34)
        self.assertTrue(all(entry.get("verified") is True for entry in entries))
        self.assertTrue(all(entry.get("title") and entry.get("provider_url") for entry in entries))
        all_ids = [
            imdb_id
            for entry in entries
            for imdb_id in entry.get("imdb_ids", [])
        ]
        self.assertEqual(len(all_ids), 37)
        self.assertTrue(all(
            isinstance(imdb_id, str) and imdb_id.startswith("tt")
            and imdb_id[2:].isdigit()
            for imdb_id in all_ids
        ))
        self.assertEqual(len({entry["provider_url"] for entry in entries}), 34)

    def test_verified_ground_truth_smoke_benchmark_with_synthetic_tmdb(self):
        """Smoke-test resolver decisions against verified IDs using synthetic TMDB payloads.

        This checks the resolver's decision path, not real TMDB search recall.
        Special multi-title pages intentionally remain manual-review/ambiguous.
        """
        fixture_path = (
            Path(__file__).resolve().parent
            / "fixtures"
            / "aniworld_identity_ground_truth.json"
        )
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        manual_review_titles = {"Persona 3: The Movie", "Cardcaptors"}
        movie_titles = {"Mirai", "Baggy"}
        passed = 0
        manual_review = 0

        for index, entry in enumerate(fixture["entries"]):
            title = entry["title"]
            expected_ids = entry["imdb_ids"]
            is_manual = title in manual_review_titles
            media_type = "movie" if title in movie_titles else "series"
            candidate_id = 90000 + index
            year_match = re.search(r"\b(?:19|20)\d{2}\b", title)
            candidate_year = year_match.group(0) if year_match else "2018"
            candidate_title = re.sub(r"\s*[（(](?:19|20)\d{2}[）)]\s*$", "", title).strip()

            def fake_get(path, params, *, _title=candidate_title, _candidate_id=candidate_id,
                         _media_type=media_type, _year=candidate_year, _imdb=expected_ids[0]):
                if path.startswith("/search/"):
                    name_key = "name" if _media_type == "series" else "title"
                    original_key = "original_name" if _media_type == "series" else "original_title"
                    date_key = "first_air_date" if _media_type == "series" else "release_date"
                    return {"results": [{
                        "id": _candidate_id, name_key: _title, original_key: _title,
                        date_key: f"{_year}-01-01",
                    }]}
                if path.endswith("/external_ids"):
                    return {"imdb_id": _imdb}
                return {}

            record = {
                "provider": "aniworld",
                "provider_title": title,
                "media_type": media_type,
                "year": int(candidate_year) if year_match else None,
                "metadata": {"multi_movie_collection": is_manual},
            }
            result = find_tmdb_match(record, search_fn=fake_get)
            if is_manual:
                self.assertEqual(result["status"], "ambiguous", title)
                self.assertTrue(result.get("multi_movie_collection"), title)
                manual_review += 1
            else:
                self.assertEqual(result["status"], "matched", title)
                self.assertEqual(result["imdb_id"], expected_ids[0], title)
                passed += 1

        self.assertEqual(passed, 32)
        self.assertEqual(manual_review, 2)

    def test_verified_year_collision_pairs_resolve_to_the_expected_version(self):
        """Replay the known Kanon and Space Between year collisions with synthetic TMDB."""
        fixture_path = (
            Path(__file__).resolve().parent
            / "fixtures"
            / "aniworld_identity_ground_truth.json"
        )
        entries = json.loads(fixture_path.read_text(encoding="utf-8"))["entries"]
        collision_groups = [
            ("Kanon (2002)", "Kanon (2006)"),
            ("The Space Between (1992)", "The Space Between (2012)"),
        ]
        for older_title, newer_title in collision_groups:
            pair = [next(entry for entry in entries if entry["title"] == title)
                    for title in (older_title, newer_title)]
            years = [int(re.search(r"\b(?:19|20)\d{2}\b", entry["title"]).group(0))
                     for entry in pair]
            base_title = re.sub(r"\s*[（(](?:19|20)\d{2}[）)]\s*$", "", older_title)
            candidate_rows = [
                {"id": 88001, "name": base_title, "original_name": base_title,
                 "first_air_date": f"{years[0]}-01-01"},
                {"id": 88002, "name": base_title, "original_name": base_title,
                 "first_air_date": f"{years[1]}-01-01"},
            ]
            ids_by_candidate = {88001: pair[0]["imdb_ids"][0], 88002: pair[1]["imdb_ids"][0]}

            for entry, expected_year, expected_id in zip(pair, years, [ids_by_candidate[88001], ids_by_candidate[88002]]):
                def fake_get(path, params):
                    if path == "/search/tv":
                        return {"results": candidate_rows}
                    if path.endswith("/external_ids"):
                        tmdb_id = int(path.split("/")[2])
                        return {"imdb_id": ids_by_candidate[tmdb_id]}
                    return {}

                result = find_tmdb_match({
                    "provider": "aniworld", "media_type": "series",
                    "provider_title": entry["title"], "year": expected_year,
                }, search_fn=fake_get)
                self.assertEqual(result["status"], "matched", entry["title"])
                self.assertEqual(result["imdb_id"], expected_id, entry["title"])

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
