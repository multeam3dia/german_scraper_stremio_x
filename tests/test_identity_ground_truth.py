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
