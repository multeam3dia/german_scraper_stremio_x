"""Integrity checks for user-verified AniWorld identity reference data."""
import json
import re
import unittest
from pathlib import Path


class AniWorldIdentityGroundTruthTests(unittest.TestCase):
    def test_reference_set_contains_all_verified_provider_pages(self):
        path = Path(__file__).parent / "fixtures" / "aniworld_identity_ground_truth.json"
        fixture = json.loads(path.read_text(encoding="utf-8"))
        entries = fixture["entries"]

        self.assertEqual(fixture["provider"], "aniworld")
        self.assertEqual(len(entries), 34)
        self.assertTrue(all(entry["verified"] for entry in entries))
        self.assertEqual(len({entry["provider_url"] for entry in entries}), 34)
        for entry in entries:
            self.assertTrue(re.fullmatch(r"https://aniworld\.to/anime/stream/[a-z0-9-]+", entry["provider_url"]))
            self.assertTrue(entry["imdb_ids"])
            for imdb_id in entry["imdb_ids"]:
                self.assertRegex(imdb_id, r"^tt\d{7,10}$")

    def test_user_audit_contains_confirmed_and_corrected_matches(self):
        fixture = json.loads(
            (Path(__file__).parent / "fixtures" / "aniworld_identity_ground_truth.json")
            .read_text(encoding="utf-8"
        )
        )
        audit = fixture["user_audit"]
        matches = audit["confirmed_matches"]
        by_title = {entry["provider_title"]: entry for entry in matches}

        self.assertEqual(len(matches), 9)
        self.assertEqual(by_title["Monthly Girls' Nozaki-kun"]["decision"], "corrected_match")
        self.assertEqual(by_title["Monthly Girls' Nozaki-kun"]["imdb_id"], "tt3615002")
        for entry in matches:
            if "imdb_id" in entry:
                self.assertRegex(entry["imdb_id"], r"^tt\d{7,10}$")

    def test_user_audit_records_rejected_candidate(self):
        fixture = json.loads(
            (Path(__file__).parent / "fixtures" / "aniworld_identity_ground_truth.json")
            .read_text(encoding="utf-8")
        )
        rejected = fixture["user_audit"]["rejected_candidates"]
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]["provider_title"], "Musasi-no")
        self.assertEqual(rejected[0]["rejected_candidate_title"], "Urawa no Usagi-chan")
        self.assertEqual(rejected[0]["decision"], "rejected_match")

    def test_persona_3_collection_keeps_four_distinct_film_identities(self):
        fixture = json.loads(
            (Path(__file__).parent / "fixtures" / "aniworld_identity_ground_truth.json")
            .read_text(encoding="utf-8")
        )
        persona = next(entry for entry in fixture["entries"] if entry["title"] == "Persona 3: The Movie")
        self.assertEqual(len(persona["imdb_ids"]), 4)
        self.assertEqual(len(set(persona["imdb_ids"])), 4)

    def test_special_cases_are_explicit_in_reference_data(self):
        fixture = json.loads(
            (Path(__file__).parent / "fixtures" / "aniworld_identity_ground_truth.json")
            .read_text(encoding="utf-8")
        )
        by_title = {entry["title"]: entry for entry in fixture["entries"]}
        self.assertIn("manual-review policy", by_title["Cardcaptors"]["note"])
        self.assertIn("first episode", by_title["Miss Kobayashi's Dragon Maid S Short Animation Series"]["note"])


if __name__ == "__main__":
    unittest.main()
