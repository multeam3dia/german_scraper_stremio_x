"""Tests for confidence-aware TMDB identity matching."""

import os
import tempfile
import unittest

import catalog_db
from identity_resolver import (
    find_tmdb_match, resolve_unresolved_titles, _structure_comparison,
    _episode_title_similarity, _episode_title_comparison, _candidate_episode_titles, _genre_similarity,
)
from catalog_identity import get_title_by_provider_url, get_title_by_external_id


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

    def test_confident_tmdb_match_without_imdb_is_still_identified(self):
        def fake_get(path, params):
            if path == "/search/tv":
                return {"results": [
                    {"id": 901, "name": "Exact Match", "original_name": "Exact Match",
                     "first_air_date": "2020-01-01"}
                ]}
            if path == "/tv/901/external_ids":
                return {"imdb_id": None}
            return {}

        result = find_tmdb_match(
            {"media_type": "series", "provider_title": "Exact Match", "year": 2020},
            search_fn=fake_get,
        )
        self.assertEqual(result["status"], "identified")
        self.assertEqual(result["tmdb_id"], 901)

    def test_nonmatching_metadata_does_not_penalize_candidate(self):
        def fake_get(path, params):
            if path == "/search/tv":
                return {"results": [
                    {"id": 902, "name": "Exact Match", "original_name": "Exact Match",
                     "first_air_date": "2020-01-01"},
                    {"id": 903, "name": "Exact Match", "original_name": "Exact Match",
                     "first_air_date": "2020-01-01"},
                ]}
            if path == "/tv/902/credits":
                return {"cast": [{"name": "Known Actor"}], "crew": []}
            if path == "/tv/903/credits":
                return {"cast": [{"name": "Different Actor"}], "crew": []}
            if path == "/tv/902/external_ids":
                return {"imdb_id": "tt1234567"}
            return {"imdb_id": "tt7654321"}

        result = find_tmdb_match(
            {
                "media_type": "series", "provider_title": "Exact Match", "year": 2020,
                "metadata": {"actors": ["Known Actor"]},
            },
            search_fn=fake_get,
        )
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["tmdb_id"], 902)
        self.assertGreaterEqual(result["margin"], 0.08)

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


    def test_apply_persists_title_without_imdb_and_resolves_queue_item(self):
        url = "https://aniworld.to/anime/stream/example-no-imdb"
        catalog_db.record_unresolved_title(
            "aniworld", url, "Example Anime", "series", year=2024,
        )

        def fake_get(path, params):
            if path == "/search/tv":
                return {"results": [{
                    "id": 98765, "name": "Example Anime",
                    "original_name": "Example Anime",
                    "first_air_date": "2024-01-01",
                }]}
            if path == "/tv/98765/external_ids":
                return {"imdb_id": None, "tvdb_id": 123456}
            return {}

        summary = resolve_unresolved_titles(apply=True, search_fn=fake_get)
        self.assertEqual(summary["identified_without_imdb"], 1)
        self.assertEqual(summary["applied"], 1)
        self.assertEqual(catalog_db.get_unresolved_titles(), [])
        canonical = get_title_by_provider_url("aniworld", url)
        self.assertIsNotNone(canonical)
        self.assertEqual(canonical["title"], "Example Anime")
        self.assertNotIn("imdb", canonical["external_ids"])
        self.assertEqual(canonical["external_ids"]["tmdb"]["id"], "98765")
        self.assertEqual(canonical["external_ids"]["tvdb"]["id"], "123456")
        self.assertEqual(
            get_title_by_external_id("series", "tmdb", "98765")["title_id"],
            canonical["title_id"],
        )

    def test_distinct_provider_pages_sharing_tmdb_identity_are_not_auto_applied(self):
        first_url = "https://aniworld.to/anime/stream/beyblade-burst-rise"
        second_url = "https://aniworld.to/anime/stream/beyblade-burst-quaddrive"
        catalog_db.record_unresolved_title(
            "aniworld", first_url, "Beyblade Burst Rise", "series", year=2023,
        )
        catalog_db.record_unresolved_title(
            "aniworld", second_url, "Beyblade Burst QuadDrive", "series", year=2023,
        )

        def fake_get(path, params):
            if path == "/search/tv":
                return {"results": [{
                    "id": 67667, "name": "Beyblade Burst",
                    "original_name": "ベイブレードバースト",
                    "first_air_date": "2016-04-04",
                }]}
            if path == "/tv/67667/alternative_titles":
                return {"results": [
                    {"title": "Beyblade Burst Rise"},
                    {"title": "Beyblade Burst QuadDrive"},
                ]}
            if path == "/tv/67667/external_ids":
                return {"imdb_id": "tt6348126"}
            return {}

        summary = resolve_unresolved_titles(apply=True, search_fn=fake_get)

        self.assertEqual(summary["applied"], 0)
        self.assertEqual(summary["ambiguous"], 2)
        self.assertEqual(summary["matched"], 0)
        self.assertEqual(len(catalog_db.get_unresolved_titles()), 2)
        for item in summary["results"]:
            self.assertEqual(item["status"], "ambiguous")
            self.assertIn("identity_collision", item)
            self.assertEqual(item["identity_collision"]["tmdb_id"], "67667")

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

    def test_wikidata_p345_is_used_when_tmdb_has_no_imdb_id(self):
        def fake_get(path, params):
            if path == "/search/tv":
                return {"results": [{
                    "id": 410, "name": "My Cultivator Girlfriend",
                    "original_name": "我的天劫女友", "first_air_date": "2017-09-20",
                }]}
            if path == "/tv/410/external_ids":
                return {"imdb_id": None, "wikidata_id": "Q12345"}
            return {}

        def fake_wikidata(identifier):
            self.assertEqual(identifier, "Q12345")
            return {"entities": {"Q12345": {"claims": {
                "P345": [{"mainsnak": {"datavalue": {"value": "tt16155418"}}}]
            }}}}

        result = find_tmdb_match(
            {"media_type": "series", "provider_title": "My Cultivator Girlfriend",
             "year": 2017},
            search_fn=fake_get, wikidata_lookup_fn=fake_wikidata,
        )
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["imdb_id"], "tt16155418")
        self.assertEqual(result["imdb_id_source"], "wikidata_p345")

    def test_wikidata_fallback_rejects_malformed_imdb_identifiers(self):
        def fake_get(path, params):
            if path == "/search/tv":
                return {"results": [{
                    "id": 411, "name": "Mahjong Soul Pon",
                    "original_name": "じゃんたま PONG☆", "first_air_date": "2022-04-01",
                }]}
            if path == "/tv/411/external_ids":
                return {"wikidata_id": "Q12346"}
            return {}

        def fake_wikidata(identifier):
            return {"entities": {identifier: {"claims": {
                "P345": [{"mainsnak": {"datavalue": {"value": "not-an-imdb-id"}}}]
            }}}}

        result = find_tmdb_match(
            {"media_type": "series", "provider_title": "Mahjong Soul Pon",
             "year": 2022},
            search_fn=fake_get, wikidata_lookup_fn=fake_wikidata,
        )
        self.assertEqual(result["status"], "identified")

    def test_gunma_chan_year_and_aniworld_studio_select_correct_candidate(self):
        def fake_get(path, params):
            if path == "/search/tv":
                return {"results": [
                    {"id": 133306, "name": "Gunma-chan",
                     "original_name": "ぐんまちゃん", "first_air_date": "2021-10-03"},
                    {"id": 999001, "name": "Gunma-chan",
                     "original_name": "ぐんまちゃん", "first_air_date": "2021-10-03"},
                ]}
            if path == "/tv/133306":
                return {"production_companies": [{"name": "Ascension"}]}
            if path == "/tv/999001":
                return {"production_companies": [{"name": "Different Studio"}]}
            if path == "/tv/133306/external_ids":
                return {"imdb_id": None}
            return {"imdb_id": "tt9999999"}

        result = find_tmdb_match(
            {"provider": "aniworld", "media_type": "series",
             "provider_title": "Gunma-chan", "year": 2021,
             "metadata": {"producer": "Ascension"}},
            search_fn=fake_get,
        )
        self.assertEqual(result["status"], "identified")
        self.assertEqual(result["tmdb_id"], 133306)
        self.assertEqual(result["studio_score"], 1.0)
        self.assertEqual(result["candidate_year"], 2021)

    def test_user_verified_imdb_ids_are_supported_by_standard_resolution_flow(self):
        examples = [
            ("You Don't Know Gunma Yet", 2018, "tt37894675"),
            ("My Cultivator Girlfriend", 2017, "tt16155418"),
            ("Mahjong Soul Pon", 2022, "tt37854502"),
            ("I'll Live a Long Life to Dote on My Favorite Stepbrother!", 2026, "tt42728353"),
            ("Gakuen Handsome", 2016, "tt41856393"),
        ]
        for index, (title, year, imdb_id) in enumerate(examples, start=1):
            with self.subTest(title=title):
                def fake_get(path, params):
                    if path == "/search/tv":
                        return {"results": [{
                            "id": 420 + index, "name": title,
                            "original_name": title, "first_air_date": f"{year}-01-01",
                        }]}
                    if path.endswith("/external_ids"):
                        return {"imdb_id": imdb_id}
                    return {}

                result = find_tmdb_match(
                    {"media_type": "series", "provider_title": title, "year": year},
                    search_fn=fake_get,
                )
                self.assertEqual(result["status"], "matched")
                self.assertEqual(result["imdb_id"], imdb_id)

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



    def test_weak_title_can_be_rescued_by_two_independent_metadata_signals(self):
        calls = []

        def fake_get(path, params):
            calls.append(path)
            if path == "/search/tv":
                return {"results": [{
                    "id": 201, "name": "Otome Yokai Zakuro",
                    "original_name": "おとめ妖怪 ざくろ",
                    "first_air_date": "2010-10-01",
                }]}
            if path == "/tv/201/credits":
                return {
                    "cast": [{"name": "Actor One"}],
                    "crew": [{"name": "Director One", "job": "Director"}],
                }
            if path == "/tv/201/external_ids":
                return {"imdb_id": "tt1738078"}
            return {}

        result = find_tmdb_match(
            {
                "provider": "serienstream",
                "media_type": "series",
                "provider_title": "Zakuro",
                "metadata": {
                    "actors": ["Actor One"],
                    "directors": ["Director One"],
                },
            },
            search_fn=fake_get,
        )
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["imdb_id"], "tt1738078")
        self.assertLess(result["title_score"], 0.94)
        self.assertEqual(result["actor_score"], 1.0)
        self.assertEqual(result["director_score"], 1.0)

    def test_weak_title_without_two_independent_signals_is_not_rescued(self):
        def fake_get(path, params):
            if path == "/search/tv":
                return {"results": [{
                    "id": 202, "name": "Otome Yokai Zakuro",
                    "original_name": "おとめ妖怪 ざくろ",
                }]}
            if path == "/tv/202/alternative_titles":
                return {"results": []}
            return {}

        result = find_tmdb_match(
            {
                "provider": "serienstream",
                "media_type": "series",
                "provider_title": "Zakuro",
                "metadata": {"actors": ["Unknown Actor"]},
            },
            search_fn=fake_get,
        )
        self.assertEqual(result["status"], "no_match")
        self.assertEqual(
            result["diagnostics"]["candidates"][0]["rejection"],
            "insufficient_title_or_independent_metadata_evidence",
        )




    def test_genre_mismatch_does_not_penalize_strong_title_match(self):
        def fake_get(path, params):
            if path == "/search/tv":
                return {"results": [{
                    "id": 301, "name": "Butlers: Chitose Momotose Monogatari",
                    "original_name": "Butlers: Chitose Momotose Monogatari",
                    "first_air_date": "2018-01-01", "genre_ids": [18],
                }]}
            if path == "/tv/301/alternative_titles":
                return {"results": [{"title": "Butlers x Battlers"}]}
            if path == "/tv/301/credits":
                return {"cast": [{"name": "Actor One"}], "crew": []}
            if path == "/tv/301":
                return {"production_companies": [{"name": "Studio One"}]}
            if path == "/tv/301/external_ids":
                return {"imdb_id": "tt1234567"}
            if path == "/genre/tv/list":
                return {"genres": [{"id": 18, "name": "Drama"}]}
            return {}

        result = find_tmdb_match(
            {
                "provider": "aniworld", "media_type": "series",
                "provider_title": "Butlers x Battlers", "year": 2018,
                "metadata": {
                    "provider_genres": ["Action"],
                    "actors": ["Actor One"], "producer": "Studio One",
                },
            },
            search_fn=fake_get,
        )
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["genre_score"], 0.0)
        self.assertGreaterEqual(result["score"], 0.94)

    def test_anne_of_green_gables_exact_year_breaks_alias_collision(self):
        def fake_get(path, params):
            if path == "/search/tv":
                return {"results": [
                    {"id": 16047, "name": "Anne mit den roten Haaren",
                     "original_name": "赤毛のアン", "first_air_date": "1979-01-01"},
                    {"id": 5874, "name": "Anne of Green Gables",
                     "original_name": "Anne of Green Gables", "first_air_date": "1985-01-01"},
                ]}
            if path == "/tv/16047/alternative_titles":
                return {"results": [{"title": "Anne of Green Gables"}]}
            if path == "/tv/5874/alternative_titles":
                return {"results": [{"title": "Anne of Green Gables"}]}
            if path.endswith("/external_ids"):
                return {"imdb_id": "tt1234567" if "/16047/" in path else "tt7654321"}
            return {}

        result = find_tmdb_match(
            {"media_type": "series", "provider_title": "Anne of Green Gables",
             "year": 1979}, search_fn=fake_get,
        )
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["tmdb_id"], 16047)

    def test_format_suffix_candidate_needs_independent_confirmation(self):
        def fake_get(path, params):
            if path == "/search/tv":
                return {"results": [{
                    "id": 341826, "name": "Animal Crossing Shorts",
                    "original_name": "どうぶつの森", "first_air_date": "2006-01-01",
                }]}
            if path == "/tv/341826/credits":
                return {"cast": [{"name": "Kazumi Totaka"}], "crew": []}
            if path.endswith("/external_ids"):
                return {"imdb_id": "tt1234567"}
            return {}

        result = find_tmdb_match(
            {"provider": "aniworld", "media_type": "series",
             "provider_title": "Animal Crossing", "year": 2006,
             "metadata": {"actors": ["Kazumi Totaka"]}},
            search_fn=fake_get,
        )
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["tmdb_id"], 341826)
        self.assertLess(result["title_score"], 0.94)

    def test_format_suffix_without_corroboration_is_not_auto_matched(self):
        def fake_get(path, params):
            if path == "/search/tv":
                return {"results": [{
                    "id": 341826, "name": "Animal Crossing Shorts",
                    "original_name": "どうぶつの森", "first_air_date": "2006-01-01",
                }]}
            return {}

        result = find_tmdb_match(
            {"media_type": "series", "provider_title": "Animal Crossing",
             "year": 2006}, search_fn=fake_get,
        )
        self.assertEqual(result["status"], "no_match")
        self.assertEqual(
            result["diagnostics"]["candidates"][0]["rejection"],
            "below_title_similarity_threshold",
        )

    def test_provider_episode_structure_rejects_clear_tmdb_mismatch(self):
        calls = []

        def fake_get(path, params):
            calls.append(path)
            if path == "/search/tv":
                return {"results": [{
                    "id": 341826, "name": "Animal Crossing Shorts",
                    "original_name": "どうぶつの森", "first_air_date": "2006-01-01",
                }]}
            if path == "/tv/341826":
                return {"number_of_seasons": 1, "number_of_episodes": 12,
                        "seasons": [{"season_number": 1, "episode_count": 12}]}
            return {}

        result = find_tmdb_match(
            {
                "media_type": "series", "provider_title": "Animal Crossing", "year": 2006,
                "metadata": {"provider_structure": {
                    "complete": True, "season_count": 1, "episode_count": 1,
                    "seasons": [{"season_number": 1, "episode_count": 1}],
                }},
            },
            search_fn=fake_get,
        )
        self.assertEqual(result["status"], "no_match")
        self.assertEqual(
            result["diagnostics"]["candidates"][0]["rejection"],
            "provider_tmdb_episode_structure_mismatch",
        )
        self.assertIn("/tv/341826", calls)

    def test_structure_check_is_not_fetched_for_clear_exact_match(self):
        calls = []

        def fake_get(path, params):
            calls.append(path)
            if path == "/search/tv":
                return {"results": [{
                    "id": 810, "name": "Exact Series", "original_name": "Exact Series",
                    "first_air_date": "2020-01-01",
                }]}
            if path == "/tv/810/external_ids":
                return {"imdb_id": "tt1234567"}
            return {}

        result = find_tmdb_match(
            {"media_type": "series", "provider_title": "Exact Series", "year": 2020,
             "metadata": {"provider_structure": {
                 "complete": True, "season_count": 1, "episode_count": 10,
                 "seasons": [{"season_number": 1, "episode_count": 10}],
             }}},
            search_fn=fake_get,
        )
        self.assertEqual(result["status"], "matched")
        self.assertNotIn("/tv/810", calls)

    
    def test_candidate_episode_titles_fetches_provider_seasons_and_scores(self):
        calls = []
        def fake_get(path, params):
            calls.append(path)
            if path == "/tv/77/season/1":
                return {"episodes": [{"name": "Episode Alpha"}, {"name": "Episode Beta"}]}
            if path == "/tv/77/season/2":
                return {"episodes": [{"name": "Episode Gamma"}]}
            return {}
        result = _candidate_episode_titles(
            {"id": 77},
            {"seasons": [
                {"season_number": 1, "episode_titles": [{"titles": ["Episode Alpha"]}, {"titles": ["Episode Beta"]}]},
                {"season_number": 2, "episode_titles": [{"titles": ["Episode Gamma"]}]},
            ]},
            fake_get,
        )
        self.assertEqual(result["score"], 1.0)
        self.assertEqual(result["seasons_checked"], [1, 2])
        self.assertEqual(calls, ["/tv/77/season/1", "/tv/77/season/2"])

    def test_candidate_episode_titles_skips_when_provider_titles_missing(self):
        calls = []
        def fake_get(path, params):
            calls.append(path)
            return {}
        result = _candidate_episode_titles(
            {"id": 77}, {"seasons": [{"season_number": 1, "episode_titles": []}]}, fake_get
        )
        self.assertIsNone(result["score"])
        self.assertEqual(calls, [])

    def test_episode_title_comparison_exposes_best_pairs(self):
        result = _episode_title_comparison(
            ["Episode Alpha", "Episode Beta"],
            ["Episode Alpha", "Episode Gamma"],
        )
        self.assertEqual(result["score"], 0.86)
        self.assertEqual(result["matches"][0]["tmdb_title"], "Episode Alpha")
        self.assertEqual(result["matches"][0]["score"], 1.0)
        self.assertEqual(result["matches"][1]["tmdb_title"], "Episode Alpha")\n        self.assertEqual(result["matches"][1]["score"], 0.72)

    def test_episode_title_similarity_matches_provider_bundles_to_any_episode(self):
        score = _episode_title_similarity(
            ["The Fate of Particular Adventurers", "I Got a Girlfriend!"],
            ["The Fate of Particular Adventurers", "A New Threat", "I Got a Girlfriend!"],
        )
        self.assertEqual(score, 1.0)

    def test_episode_title_similarity_handles_english_and_german_variants(self):
        score = _episode_title_similarity(
            ["Das Schicksal der Abenteurer"],
            ["The Fate of Particular Adventurers"],
        )
        self.assertGreater(score, 0.0)
        self.assertLess(score, 1.0)

    def test_episode_title_similarity_is_unavailable_without_titles(self):
        self.assertIsNone(_episode_title_similarity([], ["Episode 1"]))

    def test_structure_comparison_accepts_two_to_one_episode_bundling(self):
        result = _structure_comparison(
            {"complete": True, "season_count": 2, "episode_count": 24,
             "seasons": [{"season_number": 1, "episode_count": 12},
                         {"season_number": 2, "episode_count": 12}]},
            {"number_of_seasons": 2, "number_of_episodes": 48,
             "seasons": [{"season_number": 1, "episode_count": 24},
                         {"season_number": 2, "episode_count": 24}]},
        )
        self.assertEqual(result["status"], "bundled_match")
        self.assertFalse(result["mismatch"])
        self.assertEqual(result["bundle_factor"], 2)
        self.assertEqual(result["score"], 0.75)

    def test_structure_comparison_accepts_reverse_two_to_one_bundling(self):
        result = _structure_comparison(
            {"complete": True, "season_count": 1, "episode_count": 24,
             "seasons": [{"season_number": 1, "episode_count": 24}]},
            {"number_of_seasons": 1, "number_of_episodes": 12,
             "seasons": [{"season_number": 1, "episode_count": 12}]},
        )
        self.assertEqual(result["status"], "bundled_match")
        self.assertFalse(result["mismatch"])

    def test_structure_comparison_rejects_small_false_bundle_ratio(self):
        result = _structure_comparison(
            {"complete": True, "season_count": 1, "episode_count": 1,
             "seasons": [{"season_number": 1, "episode_count": 1}]},
            {"number_of_seasons": 1, "number_of_episodes": 3,
             "seasons": [{"season_number": 1, "episode_count": 3}]},
        )
        self.assertNotEqual(result["status"], "bundled_match")
        self.assertIsNone(result["bundle_factor"])

    def test_structure_comparison_does_not_hide_unrelated_count_mismatch(self):
        result = _structure_comparison(
            {"complete": True, "season_count": 1, "episode_count": 10,
             "seasons": [{"season_number": 1, "episode_count": 10}]},
            {"number_of_seasons": 1, "number_of_episodes": 16,
             "seasons": [{"season_number": 1, "episode_count": 16}]},
        )
        self.assertEqual(result["status"], "mismatch")
        self.assertTrue(result["mismatch"])
        self.assertIsNone(result["bundle_factor"])

    def test_structure_diagnostics_include_tmdb_per_season_counts(self):
        result = _structure_comparison(
            {"complete": True, "season_count": 1, "episode_count": 1,
             "seasons": [{"season_number": 1, "episode_count": 1}]},
            {"number_of_seasons": 1, "number_of_episodes": 12,
             "seasons": [{"season_number": 1, "episode_count": 12}]},
        )
        self.assertEqual(result["tmdb"]["seasons"], {"1": 12})
        self.assertEqual(result["provider"]["seasons"], {"1": 1})
        self.assertTrue(result["season_detail_mismatch"])

    def test_verified_aniworld_movie_type_correction_is_applied_as_movie(self):
        url = "https://aniworld.to/anime/stream/animal-crossing"
        catalog_db.record_unresolved_title(
            "aniworld", url, "Animal Crossing", "series", year=2006,
            metadata={
                "actors": ["Yui Horie", "Fumiko Orikasa"],
                "directors": ["Joji Shimura"],
                "producer": "OLM",
                "provider_structure": {
                    "complete": True, "season_count": 1, "episode_count": 1,
                    "seasons": [{"season_number": 1, "episode_count": 1}],
                },
            },
        )

        def fake_get(path, params):
            if path == "/search/tv":
                return {"results": [{
                    "id": 341826, "name": "Animal Crossing Shorts",
                    "original_name": "どうぶつの森",
                    "first_air_date": "2006-01-01",
                }]}
            if path == "/tv/341826/credits":
                return {"cast": [], "crew": []}
            if path == "/tv/341826":
                return {
                    "number_of_seasons": 1, "number_of_episodes": 12,
                    "seasons": [{"season_number": 1, "episode_count": 12}],
                }
            if path == "/search/movie":
                return {"results": [{
                    "id": 20523, "title": "Animal Crossing: Der Film",
                    "original_title": "どうぶつの森",
                    "release_date": "2006-12-16",
                }]}
            if path == "/movie/20523/alternative_titles":
                return {"titles": [{"title": "Animal Crossing"}]}
            if path == "/movie/20523/credits":
                return {
                    "cast": [{"name": "Yui Horie"}, {"name": "Fumiko Orikasa"}],
                    "crew": [{"name": "Joji Shimura", "job": "Director"}],
                }
            if path == "/movie/20523":
                return {"production_companies": [{"name": "OLM"}]}
            if path == "/movie/20523/external_ids":
                return {"imdb_id": "tt0900000"}
            return {}

        summary = resolve_unresolved_titles(apply=True, search_fn=fake_get)

        self.assertEqual(summary["matched"], 1)
        self.assertEqual(summary["applied"], 1)
        self.assertEqual(catalog_db.get_unresolved_titles(), [])
        canonical = get_title_by_provider_url("aniworld", url)
        self.assertIsNotNone(canonical)
        self.assertEqual(
            get_title_by_external_id("movie", "tmdb", "20523")["title_id"],
            canonical["title_id"],
        )
        self.assertIsNone(get_title_by_external_id("series", "tmdb", "20523"))

    def test_structural_series_rejection_probes_movie_and_reports_type_conflict(self):
        calls = []

        def fake_get(path, params):
            calls.append(path)
            if path == "/search/tv":
                return {"results": [{
                    "id": 341826, "name": "Animal Crossing Shorts",
                    "original_name": "どうぶつの森", "first_air_date": "2006-01-01",
                }]}
            if path == "/tv/341826":
                return {"number_of_seasons": 1, "number_of_episodes": 12,
                        "seasons": [{"season_number": 1, "episode_count": 12}]}
            if path == "/search/movie":
                return {"results": [{
                    "id": 98765, "title": "Animal Crossing: The Movie",
                    "original_title": "劇場版 どうぶつの森",
                    "release_date": "2006-12-16",
                }]}
            if path == "/movie/98765/external_ids":
                return {"imdb_id": "tt1234567"}
            return {}

        result = find_tmdb_match(
            {"media_type": "series", "provider": "aniworld",
             "provider_title": "Animal Crossing", "year": 2006,
             "metadata": {"provider_structure": {
                 "complete": True, "season_count": 1, "episode_count": 1,
                 "seasons": [{"season_number": 1, "episode_count": 1}],
             }}},
            search_fn=fake_get,
        )
        self.assertEqual(result["status"], "ambiguous")
        self.assertTrue(result["media_type_conflict"])
        self.assertEqual(
            result["diagnostics"]["alternate_movie_candidate"]["tmdb_id"], 98765
        )
        self.assertIn("/search/movie", calls)

    def test_aniworld_empty_tv_search_probes_movie_and_corrects_verified_film(self):
        calls = []

        def fake_get(path, params):
            calls.append(path)
            if path == "/search/tv":
                return {"results": []}
            if path == "/search/movie":
                return {"results": [{
                    "id": 11875, "title": "Tokyo Godfathers",
                    "original_title": "東京ゴッドファーザーズ",
                    "release_date": "2003-12-29",
                }]}
            if path == "/movie/11875/credits":
                return {
                    "cast": [{"name": "Toru Emori"}, {"name": "Aya Okamoto"}],
                    "crew": [{"name": "Satoshi Kon", "job": "Director"}],
                }
            if path == "/movie/11875":
                return {"production_companies": [{"name": "Madhouse"}],
                        "production_countries": [{"iso_3166_1": "JP", "name": "Japan"}]}
            if path == "/movie/11875/external_ids":
                return {"imdb_id": "tt0388473"}
            return {}

        result = find_tmdb_match(
            {
                "provider": "aniworld", "media_type": "series",
                "provider_title": "Tokyo Godfathers", "year": 2003,
                "metadata": {
                    "actors": ["Toru Emori", "Aya Okamoto"],
                    "directors": ["Satoshi Kon"], "producer": "Madhouse",
                    "country": "Japan",
                },
            },
            search_fn=fake_get,
        )
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["tmdb_id"], 11875)
        self.assertEqual(result["media_type_correction"]["from"], "series")
        self.assertIn("/search/movie", calls)

    def test_aniworld_empty_tv_search_without_two_signals_is_not_auto_corrected(self):
        def fake_get(path, params):
            if path == "/search/tv":
                return {"results": []}
            if path == "/search/movie":
                return {"results": [{
                    "id": 11875, "title": "Tokyo Godfathers",
                    "original_title": "東京ゴッドファーザーズ",
                    "release_date": "2003-12-29",
                }]}
            if path == "/movie/11875/external_ids":
                return {"imdb_id": "tt0388473"}
            return {}

        result = find_tmdb_match(
            {
                "provider": "aniworld", "media_type": "series",
                "provider_title": "Tokyo Godfathers", "year": 2003,
                "metadata": {},
            },
            search_fn=fake_get,
        )
        self.assertNotEqual(result["status"], "matched")
        self.assertNotEqual(result.get("media_type"), "movie")

    def test_exact_series_match_does_not_probe_movie_endpoint(self):
        calls = []

        def fake_get(path, params):
            calls.append(path)
            if path == "/search/tv":
                return {"results": [{
                    "id": 810, "name": "Exact Series", "original_name": "Exact Series",
                    "first_air_date": "2020-01-01",
                }]}
            if path == "/tv/810/external_ids":
                return {"imdb_id": "tt1234567"}
            return {}

        result = find_tmdb_match(
            {"media_type": "series", "provider_title": "Exact Series", "year": 2020},
            search_fn=fake_get,
        )
        self.assertEqual(result["status"], "matched")
        self.assertNotIn("/search/movie", calls)

    def test_year_is_a_bounded_ranking_signal_not_a_hard_filter(self):
        def fake_get(path, params):
            if path == "/search/tv":
                return {"results": [
                    {"id": 302, "name": "Anne Shirley", "original_name": "Anne Shirley",
                     "first_air_date": "2025-01-01"},
                    {"id": 303, "name": "Anne mit den roten Haaren",
                     "original_name": "赤毛のアン", "first_air_date": "1979-01-01"},
                ]}
            if path == "/tv/302/alternative_titles":
                return {"results": [{"title": "Anne of Green Gables"}]}
            if path == "/tv/303/alternative_titles":
                return {"results": [{"title": "Anne of Green Gables"}]}
            if path == "/tv/303/external_ids":
                return {"imdb_id": "tt1234567"}
            if path == "/tv/302/external_ids":
                return {"imdb_id": "tt7654321"}
            return {}

        result = find_tmdb_match(
            {"media_type": "series", "provider_title": "Anne of Green Gables", "year": 1979},
            search_fn=fake_get,
        )
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["tmdb_id"], 303)
        self.assertEqual(result["candidate_year"], 1979)



    def test_aniworld_language_labels_are_excluded_from_genre_similarity(self):
        score = _genre_similarity(
            ["Abenteuer", "Ger", "GerSub"],
            {"genre_ids": [12]},
            {12: "Adventure"},
        )
        self.assertEqual(score, 1.0)

    def test_country_metadata_breaks_a_close_same_title_candidate_tie(self):
        def fake_get(path, params):
            if path == "/search/movie":
                return {"results": [
                    {"id": 101, "title": "Tokyo Godfathers",
                     "original_title": "Tokyo Godfathers", "release_date": "2003-01-01"},
                    {"id": 102, "title": "Tokyo Godfathers",
                     "original_title": "Tokyo Godfathers", "release_date": "2008-01-01"},
                ]}
            if path == "/movie/101":
                return {"production_countries": [{"iso_3166_1": "JP", "name": "Japan"}]}
            if path == "/movie/102":
                return {"production_countries": [{"iso_3166_1": "US", "name": "United States"}]}
            if path == "/movie/101/external_ids":
                return {"imdb_id": "tt0388473"}
            if path == "/movie/102/external_ids":
                return {"imdb_id": "tt7654321"}
            return {}

        result = find_tmdb_match(
            {
                "provider": "aniworld",
                "media_type": "movie",
                "provider_title": "Tokyo Godfathers",
                "year": 2003,
                "metadata": {"country": "Japan"},
            },
            search_fn=fake_get,
        )
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["tmdb_id"], 101)
        self.assertEqual(result["diagnostics"]["candidates"][0]["country"]["score"], 1.0)
        self.assertEqual(result["diagnostics"]["candidates"][1]["country"]["score"], 0.0)

if __name__ == "__main__":
    unittest.main()
