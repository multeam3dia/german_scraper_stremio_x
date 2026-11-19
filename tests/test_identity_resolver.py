"""Tests for confidence-aware TMDB identity matching."""

import os
import tempfile
import unittest

import catalog_db
from identity_resolver import find_tmdb_match, resolve_unresolved_titles


class IdentityResolverTests(unittest.TestCase):
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

    def test_exact_title_and_year_returns_imdb_match(self):
        def fake_get(path, params):
            if path == "/search/tv":
                return {"results": [
                    {"id": 10, "name": "Dark", "original_name": "Dark",
                     "first_air_date": "2017-12-01"},
                    {"id": 11, "name": "Dark: The Remake", "original_name": "Dark: The Remake",
                     "first_air_date": "2022-01-01"},
                ]}
            return {"imdb_id": "tt5753856"}

        result = find_tmdb_match(
            {"media_type": "series", "provider_title": "Dark", "year": 2017},
            search_fn=fake_get,
        )
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["imdb_id"], "tt5753856")
        self.assertGreaterEqual(result["score"], 0.94)
        self.assertEqual(result["candidate_year"], 2017)

    def test_similarly_scored_remakes_are_ambiguous(self):
        def fake_get(path, params):
            if path == "/search/tv":
                return {"results": [
                    {"id": 10, "name": "The Bridge", "original_name": "The Bridge",
                     "first_air_date": "2011-01-01"},
                    {"id": 11, "name": "The Bridge", "original_name": "The Bridge",
                     "first_air_date": "2013-01-01"},
                ]}
            return {"imdb_id": "tt1234567"}

        result = find_tmdb_match(
            {"media_type": "series", "provider_title": "The Bridge", "year": 2012},
            search_fn=fake_get,
        )
        self.assertEqual(result["status"], "ambiguous")

    def test_dry_run_does_not_admit_title_to_catalog(self):
        catalog_db.record_unresolved_title(
            "serienstream", "https://serienstream.to/serie/dark", "Dark",
            "series", year=2017,
        )

        def fake_get(path, params):
            if path == "/search/tv":
                return {"results": [
                    {"id": 10, "name": "Dark", "original_name": "Dark",
                     "first_air_date": "2017-12-01"}
                ]}
            return {"imdb_id": "tt5753856"}

        summary = resolve_unresolved_titles(apply=False, search_fn=fake_get)
        self.assertEqual(summary["matched"], 1)
        self.assertEqual(summary["applied"], 0)
        self.assertEqual(catalog_db.get_catalog_stats()["active_titles"], 0)

    def test_apply_records_match_and_resolves_queue_item(self):
        url = "https://serienstream.to/serie/dark"
        catalog_db.record_unresolved_title(
            "serienstream", url, "Dark", "series", year=2017,
            metadata={"country": "DE"},
        )

        def fake_get(path, params):
            if path == "/search/tv":
                return {"results": [
                    {"id": 10, "name": "Dark", "original_name": "Dark",
                     "first_air_date": "2017-12-01"}
                ]}
            return {"imdb_id": "tt5753856"}

        summary = resolve_unresolved_titles(apply=True, search_fn=fake_get)
        self.assertEqual(summary["applied"], 1)
        self.assertEqual(catalog_db.get_unresolved_titles(), [])
        self.assertEqual(
            catalog_db.get_resolved_imdb_id("serienstream", url), "tt5753856"
        )
        self.assertEqual(catalog_db.get_catalog_stats()["active_titles"], 1)

    def test_partial_substring_title_is_not_accepted(self):
        def fake_get(path, params):
            if path == "/search/tv":
                return {"results": [
                    {"id": 21, "name": "Bad Monkey", "original_name": "Bad Monkey",
                     "first_air_date": "2024-07-01"}
                ]}
            return {"imdb_id": "tt1234567"}

        result = find_tmdb_match(
            {"media_type": "series", "provider_title": "Monk", "year": 2002},
            search_fn=fake_get,
        )
        self.assertEqual(result["status"], "no_match")

    def test_provider_year_difference_does_not_exclude_exact_title(self):
        def fake_get(path, params):
            if path == "/search/tv":
                return {"results": [
                    {"id": 22, "name": "Dark", "original_name": "Dark",
                     "first_air_date": "2019-01-01"}
                ]}
            if path == "/tv/22/external_ids":
                return {"imdb_id": "tt5753856"}
            return {}

        result = find_tmdb_match(
            {"media_type": "series", "provider_title": "Dark", "year": 2017},
            search_fn=fake_get,
        )
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["candidate_year_delta"], 2)
        self.assertFalse(result["diagnostics"]["candidates"][0]["year"]["used_for_exclusion"])
        self.assertEqual(result["diagnostics"]["candidates"][0]["year"]["status"], "mismatch")

    def test_one_year_difference_does_not_force_manual_review(self):
        def fake_get(path, params):
            if path == "/search/tv":
                return {"results": [
                    {"id": 23, "name": "Dark", "original_name": "Dark",
                     "first_air_date": "2018-01-01"}
                ]}
            if path == "/tv/23/external_ids":
                return {"imdb_id": "tt5753856"}
            return {}

        result = find_tmdb_match(
            {"media_type": "series", "provider_title": "Dark", "year": 2017},
            search_fn=fake_get,
        )
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["candidate_year_delta"], 1)

    def test_missing_tmdb_year_is_diagnostic_not_automatic_rejection(self):
        def fake_get(path, params):
            if path == "/search/tv":
                return {"results": [
                    {"id": 24, "name": "Dark", "original_name": "Dark"}
                ]}
            if path == "/tv/24/external_ids":
                return {"imdb_id": "tt5753856"}
            return {}

        result = find_tmdb_match(
            {"media_type": "series", "provider_title": "Dark", "year": 2017},
            search_fn=fake_get,
        )
        self.assertEqual(result["status"], "matched")
        self.assertIsNone(result["candidate_year"])
        self.assertEqual(result["diagnostics"]["candidates"][0]["year"]["status"], "missing")

    def test_same_title_homonyms_without_provider_year_are_ambiguous(self):
        def fake_get(path, params):
            if path == "/search/tv":
                return {"results": [
                    {"id": 25, "name": "The Office", "original_name": "The Office",
                     "first_air_date": "2001-04-09"},
                    {"id": 26, "name": "The Office", "original_name": "The Office",
                     "first_air_date": "2005-03-24"},
                ]}
            return {"imdb_id": "tt1234567"}

        result = find_tmdb_match(
            {"media_type": "series", "provider_title": "The Office"},
            search_fn=fake_get,
        )
        self.assertEqual(result["status"], "ambiguous")
        self.assertIn("similar scores", result["reason"])

    def test_exact_title_without_year_can_match_when_unique(self):
        def fake_get(path, params):
            if path == "/search/tv":
                return {"results": [
                    {"id": 28, "name": "Dark", "original_name": "Dark",
                     "first_air_date": "2017-12-01"}
                ]}
            return {"imdb_id": "tt5753856"}

        result = find_tmdb_match(
            {"media_type": "series", "provider_title": "Dark"},
            search_fn=fake_get,
        )
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["imdb_id"], "tt5753856")

    def test_movie_search_uses_movie_endpoint_and_release_year(self):
        calls = []

        def fake_get(path, params):
            calls.append((path, params))
            if path == "/search/movie":
                return {"results": [
                    {"id": 27, "title": "Dune", "original_title": "Dune",
                     "release_date": "2021-09-03"}
                ]}
            return {"imdb_id": "tt1160419"}

        result = find_tmdb_match(
            {"media_type": "movie", "provider_title": "Dune", "year": 2021},
            search_fn=fake_get,
        )
        self.assertEqual(result["status"], "matched")
        self.assertEqual(calls[0][0], "/search/movie")
        self.assertNotIn("year", calls[0][1])


    def test_genres_break_tie_between_same_title_and_year_candidates(self):
        def fake_get(path, params):
            if path == "/search/tv":
                return {"results": [
                    {"id": 31, "name": "Example", "original_name": "Example",
                     "first_air_date": "2020-01-01", "genre_ids": [18]},
                    {"id": 32, "name": "Example", "original_name": "Example",
                     "first_air_date": "2020-01-01", "genre_ids": [10759]},
                ]}
            return {"imdb_id": "tt1234567"}

        result = find_tmdb_match(
            {
                "media_type": "series",
                "provider_title": "Example",
                "year": 2020,
                "metadata": {"provider_genres": ["Action"]},
            },
            search_fn=fake_get,
            genre_map={18: "Drama", 10759: "Action & Adventure"},
        )
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["tmdb_id"], 32)
        self.assertEqual(result["genre_score"], 1.0)
        self.assertGreaterEqual(result["margin"], 0.08)

    def test_genre_signal_is_optional_when_provider_has_no_genres(self):
        calls = []

        def fake_get(path, params):
            calls.append(path)
            if path == "/search/tv":
                return {"results": [
                    {"id": 33, "name": "Dark", "original_name": "Dark",
                     "first_air_date": "2017-12-01"}
                ]}
            return {"imdb_id": "tt5753856"}

        result = find_tmdb_match(
            {"media_type": "series", "provider_title": "Dark", "year": 2017},
            search_fn=fake_get,
        )
        self.assertEqual(result["status"], "matched")
        self.assertIsNone(result["genre_score"])
        self.assertEqual(calls, ["/search/tv", "/tv/33/external_ids"])


    def test_optional_actor_signal_breaks_same_title_year_tie(self):
        calls = []

        def fake_get(path, params):
            calls.append(path)
            if path == "/search/tv":
                return {"results": [
                    {"id": 41, "name": "Example", "original_name": "Example",
                     "first_air_date": "2020-01-01"},
                    {"id": 42, "name": "Example", "original_name": "Example",
                     "first_air_date": "2020-01-01"},
                ]}
            if path == "/tv/41/credits":
                return {"cast": [{"name": "Actor One"}, {"name": "Actor Two"}]}
            if path == "/tv/42/credits":
                return {"cast": [{"name": "Unrelated Actor"}]}
            if path == "/tv/41/external_ids":
                return {"imdb_id": "tt1234567"}
            return {"imdb_id": "tt7654321"}

        result = find_tmdb_match(
            {
                "media_type": "series",
                "provider_title": "Example",
                "year": 2020,
                "metadata": {"actors": ["Actor One", "Actor Two"]},
            },
            search_fn=fake_get,
        )
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["tmdb_id"], 41)
        self.assertEqual(result["actor_score"], 1.0)
        self.assertIn("/tv/41/credits", calls)
        self.assertIn("/tv/42/credits", calls)

    def test_missing_actor_data_is_neutral_and_keeps_tie_ambiguous(self):
        def fake_get(path, params):
            if path == "/search/tv":
                return {"results": [
                    {"id": 43, "name": "Example", "original_name": "Example",
                     "first_air_date": "2020-01-01"},
                    {"id": 44, "name": "Example", "original_name": "Example",
                     "first_air_date": "2020-01-01"},
                ]}
            if path.endswith("/credits"):
                return {}
            return {"imdb_id": "tt1234567"}

        result = find_tmdb_match(
            {
                "media_type": "series",
                "provider_title": "Example",
                "year": 2020,
                "metadata": {"actors": ["Actor One"]},
            },
            search_fn=fake_get,
        )
        self.assertEqual(result["status"], "ambiguous")
        self.assertIsNone(result["actor_score"])


    def test_director_and_aniworld_studio_signals_break_candidate_tie(self):
        calls = []

        def fake_get(path, params):
            calls.append(path)
            if path == "/search/tv":
                return {"results": [
                    {"id": 51, "name": "Blood Lad", "original_name": "Blood Lad",
                     "first_air_date": "2013-07-01"},
                    {"id": 52, "name": "Blood Lad", "original_name": "Blood Lad",
                     "first_air_date": "2013-07-01"},
                ]}
            if path == "/tv/51/credits":
                return {"cast": [], "crew": [{"name": "Takeshi Yasuda", "job": "Director"}]}
            if path == "/tv/52/credits":
                return {"cast": [], "crew": [{"name": "Different Director", "job": "Director"}]}
            if path == "/tv/51":
                return {"production_companies": [{"name": "Brain's Base"}]}
            if path == "/tv/52":
                return {"production_companies": [{"name": "Different Studio"}]}
            if path == "/tv/51/external_ids":
                return {"imdb_id": "tt1234567"}
            return {"imdb_id": "tt7654321"}

        result = find_tmdb_match(
            {
                "provider": "aniworld",
                "media_type": "series",
                "provider_title": "Blood Lad",
                "year": 2013,
                "metadata": {
                    "directors": ["Takeshi Yasuda"],
                    "producer": "Brain's Base",
                },
            },
            search_fn=fake_get,
        )
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["tmdb_id"], 51)
        self.assertEqual(result["director_score"], 1.0)
        self.assertEqual(result["studio_score"], 1.0)
        self.assertEqual(result["metadata_score"], 1.0)
        self.assertIn("/tv/51/credits", calls)
        self.assertIn("/tv/51", calls)

    def test_serienstream_producer_people_are_not_treated_as_studio(self):
        calls = []

        def fake_get(path, params):
            calls.append(path)
            if path == "/search/tv":
                return {"results": [
                    {"id": 53, "name": "Unique", "original_name": "Unique",
                     "first_air_date": "2020-01-01"}
                ]}
            return {"imdb_id": "tt1234567"}

        result = find_tmdb_match(
            {
                "provider": "serienstream",
                "media_type": "series",
                "provider_title": "Unique",
                "year": 2020,
                "metadata": {"producer": ["Person One", "Person Two"]},
            },
            search_fn=fake_get,
        )
        self.assertEqual(result["status"], "matched")
        self.assertIsNone(result["studio_score"])
        self.assertEqual(calls, ["/search/tv", "/tv/53/external_ids"])


    def test_yugioh_zero_matches_tmdb_alternative_title(self):
        calls = []

        def fake_get(path, params):
            calls.append((path, params))
            if path == "/search/tv":
                return {"results": [{
                    "id": 101, "name": "Yu-Gi-Oh!", "original_name": "遊☆戯☆王",
                    "first_air_date": "1998-04-04",
                }]}
            if path == "/tv/101/alternative_titles":
                return {"results": [{"title": "Yu-Gi-Oh! Zero", "iso_3166_1": "US"}]}
            if path == "/tv/101/external_ids":
                return {"imdb_id": "tt4834194"}
            return {}

        result = find_tmdb_match(
            {"media_type": "series", "provider_title": "Yu-Gi-Oh! Zero", "year": 1998},
            search_fn=fake_get,
        )
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["imdb_id"], "tt4834194")
        self.assertIn("Yu-Gi-Oh! Zero", result["diagnostics"]["candidates"][0]["alternative_titles"])
        self.assertEqual(
            result["diagnostics"]["candidates"][0]["title"]["matched_variant"],
            "Yu-Gi-Oh! Zero",
        )
        self.assertIn("/tv/101/alternative_titles", [path for path, _ in calls])

    def test_yakitate_japan_matches_tmdb_alias_when_main_title_is_japanese(self):
        def fake_get(path, params):
            if path == "/search/tv":
                return {"results": [{
                    "id": 102, "name": "焼きたて!! ジャぱん",
                    "original_name": "焼きたて!! ジャぱん",
                    "first_air_date": "2004-10-12",
                }]}
            if path == "/tv/102/alternative_titles":
                return {"titles": [{"title": "Yakitate!! Japan", "iso_3166_1": "US"}]}
            if path == "/tv/102/external_ids":
                return {"imdb_id": "tt0479783"}
            return {}

        result = find_tmdb_match(
            {"media_type": "series", "provider_title": "Yakitate!! Japan", "year": 2004},
            search_fn=fake_get,
        )
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["imdb_id"], "tt0479783")
        self.assertEqual(result["title_score"], 1.0)

    def test_world_dai_star_exact_title_is_found(self):
        def fake_get(path, params):
            if path == "/search/tv":
                return {"results": [{
                    "id": 103, "name": "World Dai Star",
                    "original_name": "ワールドダイスター",
                    "first_air_date": "2023-04-09",
                }]}
            if path == "/tv/103/external_ids":
                return {"imdb_id": "tt27418288"}
            return {}

        result = find_tmdb_match(
            {"media_type": "series", "provider_title": "World Dai Star", "year": 2023},
            search_fn=fake_get,
        )
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["imdb_id"], "tt27418288")

    def test_rejected_candidates_are_included_in_no_match_diagnostics(self):
        def fake_get(path, params):
            if path == "/search/tv":
                return {"results": [{
                    "id": 104, "name": "Unrelated Show",
                    "original_name": "別の番組",
                    "first_air_date": "2020-01-01",
                }]}
            if path == "/tv/104/alternative_titles":
                return {"results": [{"title": "Other Local Title"}]}
            return {}

        result = find_tmdb_match(
            {"media_type": "series", "provider_title": "No Such Anime"},
            search_fn=fake_get,
        )
        self.assertEqual(result["status"], "no_match")
        candidate = result["diagnostics"]["candidates"][0]
        self.assertEqual(candidate["tmdb_id"], 104)
        self.assertEqual(candidate["rejection"], "below_title_similarity_threshold")
        self.assertIn("Other Local Title", candidate["alternative_titles"])



    def test_english_fallback_runs_when_german_results_are_irrelevant(self):
        calls = []

        def fake_get(path, params):
            if path != "/search/tv":
                return {"imdb_id": "tt1234567"}
            calls.append(params.get("language"))
            if params.get("language") == "de-DE":
                return {"results": [
                    {"id": 61, "name": "Grey's Anatomy",
                     "original_name": "Grey's Anatomy"}
                ]}
            return {"results": [
                {"id": 62, "name": "Yuri Is My Job!",
                 "original_name": "私の百合はお仕事です！"}
            ]}

        result = find_tmdb_match(
            {"media_type": "series", "provider_title": "Yuri Is My Job!"},
            search_fn=fake_get,
        )
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["tmdb_id"], 62)
        self.assertIn("en-US", calls)

    def test_compact_spacing_difference_is_an_exact_title_match(self):
        def fake_get(path, params):
            if path == "/search/tv":
                return {"results": [
                    {"id": 63, "name": "Two Car", "original_name": "Two Car"}
                ]}
            return {"imdb_id": "tt1234567"}

        result = find_tmdb_match(
            {"media_type": "series", "provider_title": "TwoCar"},
            search_fn=fake_get,
        )
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["title_score"], 1.0)



    def test_verified_aniworld_aliases_require_matching_tmdb_and_imdb_ids(self):
        cases = [
            ("Zakuro", 36836, "tt1738078"),
            ("Yuri Is My Job!", 201949, "tt20234560"),
            ("Yu-Gi-Oh! Zero", 36406, "tt4834194"),
        ]
        for title, tmdb_id, imdb_id in cases:
            with self.subTest(title=title):
                def fake_get(path, params):
                    if path == "/search/tv":
                        return {"results": [{
                            "id": tmdb_id,
                            "name": "TMDB localized title",
                            "original_name": "Japanese title",
                            "first_air_date": "2010-01-01",
                        }]}
                    if path == f"/tv/{tmdb_id}/external_ids":
                        return {"imdb_id": imdb_id}
                    if path == f"/tv/{tmdb_id}/alternative_titles":
                        return {"results": []}
                    return {}

                result = find_tmdb_match(
                    {
                        "provider": "aniworld",
                        "media_type": "series",
                        "provider_title": title,
                        "year": 2010,
                    },
                    search_fn=fake_get,
                )
                self.assertEqual(result["status"], "matched")
                self.assertEqual(result["tmdb_id"], tmdb_id)
                self.assertEqual(result["imdb_id"], imdb_id)
                self.assertIsNotNone(result["diagnostics"]["verified_alias"])

    def test_verified_aniworld_alias_rejects_unexpected_imdb_id(self):
        def fake_get(path, params):
            if path == "/search/tv":
                return {"results": [{
                    "id": 36836, "name": "Otome Yokai Zakuro",
                    "original_name": "おとめ妖怪 ざくろ",
                }]}
            if path == "/tv/36836/external_ids":
                return {"imdb_id": "tt9999999"}
            if path == "/tv/36836/alternative_titles":
                return {"results": []}
            return {}

        result = find_tmdb_match(
            {
                "provider": "aniworld",
                "media_type": "series",
                "provider_title": "Zakuro",
            },
            search_fn=fake_get,
        )
        self.assertEqual(result["status"], "ambiguous")
        self.assertEqual(result["reason"], "Verified alias IMDb ID did not match")


if __name__ == "__main__":
    unittest.main()
