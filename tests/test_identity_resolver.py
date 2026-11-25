"""Tests for confidence-aware TMDB identity matching."""

import json
import os
import tempfile
import unittest
from decimal import Decimal
from datetime import datetime, timezone
from unittest.mock import patch

import catalog_db
from identity_resolver import (
    find_tmdb_match, resolve_unresolved_titles, resolver_retry_due, _structure_comparison,
    _genre_similarity, _search_query_variants, _best_provider_title_match,
    _episode_title_similarity, _metadata_signal_scores, _candidate_season_episode_titles,
    _independent_corroboration_values, _is_short_ambiguous_title, _company_similarity,
    _normalize, _best_title_match, _alternative_title_values,
)
from catalog_identity import get_title_by_provider_url, get_title_by_external_id


class IdentityResolverTests(unittest.TestCase):
    def test_resolver_default_selection_respects_retry_cooldown(self):
        candidates = [
            {"provider": "aniworld", "provider_url": "https://a", "provider_title": "Due",
             "media_type": "series", "cooldown": False},
            {"provider": "aniworld", "provider_url": "https://b", "provider_title": "Deferred",
             "media_type": "series", "cooldown": True},
        ]
        with patch("identity_resolver.get_unresolved_titles", return_value=candidates), \
             patch("identity_resolver.resolver_retry_due", side_effect=lambda row: not row["cooldown"]), \
             patch("identity_resolver.find_tmdb_match", return_value={"status": "no_match", "reason": "test"}), \
             patch("identity_resolver.record_unresolved_attempt"), \
             patch("identity_resolver.purge_expired_tmdb_cache", return_value=0):
            summary = resolve_unresolved_titles(provider="aniworld", limit=500)
        self.assertEqual(summary["scanned"], 1)
        self.assertEqual(summary["deferred_by_retry_cooldown"], 1)
        self.assertEqual(summary["cooldown_bypassed"], 0)

    def test_resolver_force_retry_bypasses_cooldown_for_selected_items(self):
        candidates = [
            {"provider": "aniworld", "provider_url": "https://a", "provider_title": "Due",
             "media_type": "series", "cooldown": False},
            {"provider": "aniworld", "provider_url": "https://b", "provider_title": "Deferred",
             "media_type": "series", "cooldown": True},
        ]
        with patch("identity_resolver.get_unresolved_titles", return_value=candidates), \
             patch("identity_resolver.resolver_retry_due", side_effect=lambda row: not row["cooldown"]), \
             patch("identity_resolver.find_tmdb_match", return_value={"status": "no_match", "reason": "test"}), \
             patch("identity_resolver.record_unresolved_attempt"), \
             patch("identity_resolver.purge_expired_tmdb_cache", return_value=0):
            summary = resolve_unresolved_titles(provider="aniworld", limit=500, force_retry=True)
        self.assertEqual(summary["scanned"], 2)
        self.assertEqual(summary["deferred_by_retry_cooldown"], 0)
        self.assertEqual(summary["cooldown_bypassed"], 1)


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

    def test_resolver_retry_malformed_attempt_counters_are_treated_as_initial(self):
        now = datetime.fromisoformat("2026-11-25T04:40:00+00:00")
        for malformed in (True, 1.5, Decimal("1.5"), "not-a-number"):
            with self.subTest(attempts=malformed):
                record = {
                    "resolver_attempts": malformed,
                    "resolver_last_attempt_at": "2026-11-24T00:00:00+00:00",
                }
                self.assertTrue(resolver_retry_due(record, now=now))

    def test_resolver_retry_boundary_respects_timezone_offset(self):
        record = {
            "resolver_attempts": 1,
            "resolver_last_attempt_at": "2026-11-24T00:00:00+02:00",
        }
        before_due = datetime.fromisoformat("2026-11-24T21:59:00+00:00")
        exactly_due = datetime.fromisoformat("2026-11-24T22:00:00+00:00")
        self.assertFalse(resolver_retry_due(record, now=before_due))
        self.assertTrue(resolver_retry_due(record, now=exactly_due))

    def test_resolver_retry_boundary_overflow_is_not_due(self):
        record = {
            "resolver_attempts": 1,
            "resolver_last_attempt_at": "9999-12-31T23:59:59+00:00",
        }
        now = datetime.fromisoformat("2026-11-25T04:40:00+00:00")
        self.assertFalse(resolver_retry_due(record, now=now))

    def test_resolver_reports_distinct_memory_persistent_and_http_cache_metrics(self):
        url = "https://aniworld.to/anime/stream/cache-stat-test"
        catalog_db.record_unresolved_title(
            provider="aniworld", provider_url=url, title="Cache Stat Test",
            media_type="series",
        )
        cache_key = json.dumps(
            ["/search/tv", {
                "query": "Cache Stat Test",
                "include_adult": "false",
                "language": "de-DE",
            }],
            sort_keys=True, ensure_ascii=False, default=str,
        )
        catalog_db.set_tmdb_cached_response(
            cache_key,
            {"results": [{
                "id": 7001, "name": "Cache Stat Test",
                "original_name": "Cache Stat Test", "first_air_date": "2020-01-01"
            }]},
        )
        def fake_get(path, params):
            if path == "/tv/7001/external_ids":
                return {"imdb_id": "tt1234567"}
            return {}
        summary = resolve_unresolved_titles(search_fn=fake_get)
        self.assertGreaterEqual(summary["tmdb_cache_persistent_hits"], 1)
        self.assertEqual(summary["tmdb_http_requests"], 1)
        self.assertEqual(
            summary["tmdb_cache_avoided_http_requests"], summary["tmdb_cache_hits"]
        )
        self.assertIn("tmdb_cache_hit_rate", summary)
        self.assertIn("tmdb_cache_usage_totals", summary)
        self.assertTrue(any(
            row["cache_key"] == cache_key for row in summary["tmdb_cache_top_keys"]
        ))

    def test_search_second_page_can_recover_exact_title_candidate(self):
        calls = []

        def fake_get(path, params):
            calls.append((path, dict(params)))
            if path == "/search/tv":
                if params.get("page") == 2:
                    return {"results": [{
                        "id": 902, "name": "Page Two Rescue",
                        "original_name": "Page Two Rescue",
                        "first_air_date": "2021-01-01",
                    }]}
                return {"results": [{
                    "id": 901, "name": "Unrelated Popular Result",
                    "original_name": "Unrelated Popular Result",
                    "first_air_date": "2019-01-01",
                }]}
            if path == "/tv/902/external_ids":
                return {"imdb_id": "tt1234567"}
            return {}

        result = find_tmdb_match(
            {"media_type": "series", "provider_title": "Page Two Rescue", "year": 2021},
            search_fn=fake_get,
        )
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["tmdb_id"], 902)
        self.assertTrue(any(path == "/search/tv" and params.get("query") == "Page Two Rescue" and params.get("page") == 2 for path, params in calls))

    def test_same_person_actor_and_director_counts_as_one_signal(self):
        values = _independent_corroboration_values(
            {"actor_score": 1.0, "director_score": 1.0, "studio_score": None},
            {"actors": ["Person A"], "directors": ["Person A"]},
        )
        self.assertEqual(values, [1.0])

    def test_distinct_actor_and_director_count_as_two_signals(self):
        values = _independent_corroboration_values(
            {"actor_score": 0.9, "director_score": 0.95, "studio_score": None},
            {"actors": ["Actor A"], "directors": ["Director B"]},
        )
        self.assertEqual(values, [0.9, 0.95])

    def test_shared_person_overlap_keeps_stronger_role_score(self):
        values = _independent_corroboration_values(
            {"actor_score": 0.88, "director_score": 0.94, "studio_score": 0.91},
            {"actors": ["Person A", "Actor B"], "directors": ["Person A"]},
        )
        self.assertEqual(values, [0.94, 0.91])

    def test_missing_people_signals_are_not_counted(self):
        values = _independent_corroboration_values(
            {"actor_score": None, "director_score": None, "studio_score": None},
            {"actors": [], "directors": []},
        )
        self.assertEqual(values, [])


    def test_user_rejected_candidate_is_not_auto_matched(self):
        """A manually rejected candidate remains rejected by title evidence alone."""
        rejected_candidate = {
            "id": 66221,
            "name": "Urawa no Usagi-chan",
            "original_name": "Urawa no Usagi-chan",
            "first_air_date": "2015-01-01",
        }

        def fake_get(path, params):
            if path == "/search/tv":
                return {"results": [rejected_candidate]}
            if path.endswith("/alternative_titles"):
                return {"results": []}
            return {}

        result = find_tmdb_match(
            {
                "provider": "aniworld",
                "media_type": "series",
                "provider_title": "Musasi-no",
                "year": None,
            },
            search_fn=fake_get,
        )
        self.assertNotEqual(result["status"], "matched")
        self.assertIn(result["status"], {"no_match", "ambiguous"})

    def test_short_single_word_titles_are_flagged(self):
        for title in ("Air", "One", "Dark", "Orange"):
            with self.subTest(title=title):
                self.assertTrue(_is_short_ambiguous_title(title))
        self.assertFalse(_is_short_ambiguous_title("The Dark"))
        self.assertFalse(_is_short_ambiguous_title("Attack on Titan"))

    def test_short_alias_title_collision_is_protected_when_provider_title_is_long(self):
        def fake_get(path, params):
            if path == "/search/tv":
                query = params.get("query", "")
                if query in {"Dark Reboot", "Dark"}:
                    return {"results": [
                        {"id": 777, "name": "Dark", "original_name": "Dark",
                         "first_air_date": "2017-01-01"},
                        {"id": 779, "name": "Dark", "original_name": "Dark",
                         "first_air_date": "2020-01-01"},
                    ]}
            return {}

        result = find_tmdb_match(
            {"media_type": "series", "provider_title": "Dark Reboot",
             "year": None, "metadata": {"alternative_titles": ["Dark"]}},
            search_fn=fake_get,
        )
        self.assertEqual(result["status"], "ambiguous")
        self.assertTrue(result["short_title_safeguard"]["applied"])
        self.assertFalse(result["short_title_safeguard"]["supported"])

    def test_short_title_collision_without_independent_evidence_stays_ambiguous(self):
        def fake_get(path, params):
            if path == "/search/tv":
                return {"results": [
                    {"id": 777, "name": "Dark", "original_name": "Dark",
                     "first_air_date": "2017-01-01"},
                    {"id": 779, "name": "Dark", "original_name": "Dark",
                     "first_air_date": "2020-01-01"},
                ]}
            return {}

        result = find_tmdb_match(
            {"media_type": "series", "provider_title": "Dark", "year": None},
            search_fn=fake_get,
        )
        self.assertEqual(result["status"], "ambiguous")
        self.assertIn("Short single-word title collision", result["reason"])
        self.assertTrue(result["short_title_safeguard"]["applied"])
        self.assertFalse(result["short_title_safeguard"]["supported"])

    def test_short_exact_title_with_year_and_strong_credit_can_match(self):
        def fake_get(path, params):
            if path == "/search/tv":
                return {"results": [{
                    "id": 778, "name": "Dark", "original_name": "Dark",
                    "first_air_date": "2017-01-01",
                }]}
            if path == "/tv/778/credits":
                return {"cast": [{"name": "Actor A"}], "crew": []}
            if path == "/tv/778/external_ids":
                return {"imdb_id": "tt5753856"}
            return {}

        result = find_tmdb_match(
            {"media_type": "series", "provider_title": "Dark", "year": 2017,
             "metadata": {"actors": ["Actor A"]}},
            search_fn=fake_get,
        )
        self.assertEqual(result["status"], "matched")

    def test_aniworld_country_mislabeled_as_studio_is_ignored(self):
        def fake_get(path, params):
            if path == "/tv/123":
                return {
                    "production_companies": [{"name": "Studio X"}],
                    "production_countries": [{"iso_3166_1": "JP", "name": "Japan"}],
                }
            return {}

        scores = _metadata_signal_scores(
            {"id": 123}, "series",
            {"producer": "Japan", "country": "Japan"},
            "aniworld", fake_get,
        )
        self.assertIsNone(scores["studio_score"])
        self.assertEqual(scores["country_score"], 1.0)

    def test_aniworld_company_mislabeled_as_actor_is_ignored(self):
        def fake_get(path, params):
            if path == "/tv/123/credits":
                return {"cast": [{"name": "Real Actor"}], "crew": []}
            if path == "/tv/123":
                return {"production_companies": [{"name": "Toei Animation"}]}
            return {}

        scores = _metadata_signal_scores(
            {"id": 123}, "series",
            {"actors": ["Toei Animation"], "producer": "Toei Animation"},
            "aniworld", fake_get,
        )
        self.assertIsNone(scores["actor_score"])
        self.assertEqual(scores["studio_score"], 1.0)

    def test_title_variants_ignore_malformed_candidate_fields_but_keep_string_fallbacks(self):
        malformed = {
            "name": {"nested": "Malformed Candidate"},
            "original_name": 123,
            "_alternative_titles": [{"title": "Malformed Candidate"}, 456],
        }
        score, matched_title = _best_title_match("Malformed Candidate", malformed, "series")
        self.assertEqual(score, 0.0)
        self.assertIsNone(matched_title)

        fallback = {
            "name": ["Invalid title container"],
            "original_name": "  Valid Fallback Title  ",
            "_alternative_titles": [None, {"title": "Invalid alias"}, 42],
        }
        score, matched_title = _best_title_match("Valid Fallback Title", fallback, "series")
        self.assertEqual(score, 1.0)
        self.assertEqual(matched_title, "Valid Fallback Title")

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

    def test_year_filtered_discovery_uses_two_distinct_provider_aliases(self):
        calls = []

        def fake_get(path, params):
            if path == "/search/tv":
                query = params.get("query")
                year = params.get("first_air_date_year")
                calls.append((query, year))
                if query == "Official Romanized" and year == 2018:
                    return {"results": [{
                        "id": 88018, "name": "Official Romanized",
                        "original_name": "公式ローマ字",
                        "first_air_date": "2018-04-01",
                    }]}
                return {"results": []}
            if path == "/tv/88018/external_ids":
                return {"imdb_id": "tt12345678"}
            return {}

        result = find_tmdb_match(
            {
                "media_type": "series", "provider_title": "Hard to Find Label",
                "year": 2018,
                "metadata": {
                    "alternative_titles": ["First Alias", "Official Romanized"],
                },
            },
            search_fn=fake_get,
        )
        self.assertEqual(result["status"], "matched", result)
        year_filtered = [query for query, year in calls if year is not None]
        self.assertLessEqual(len(year_filtered), 5)
        self.assertIn("Official Romanized", year_filtered)

    def test_year_filtered_discovery_uses_provider_alias_within_request_budget(self):
        calls = []
        def fake_get(path, params):
            if path == "/search/tv":
                calls.append((params.get("query"), params.get("first_air_date_year")))
                if params.get("query") == "Kanon" and params.get("first_air_date_year") == 2006:
                    return {"results": [{
                        "id": 77006, "name": "Kanon", "original_name": "Kanon",
                        "first_air_date": "2006-10-05",
                    }]}
            if path == "/tv/77006/external_ids":
                return {"imdb_id": "tt0850555"}
            return {}
        result = find_tmdb_match(
            {"media_type": "series", "provider_title": "Kanon (2006)",
             "year": 2006, "metadata": {"alternative_titles": ["Kanon"]}},
            search_fn=fake_get,
        )
        self.assertEqual(result["status"], "matched", result)
        self.assertEqual(result["tmdb_id"], 77006)
        year_filtered_calls = [call for call in calls if call[1] is not None]
        self.assertLessEqual(len(year_filtered_calls), 5)
        self.assertIn(("Kanon", 2006), year_filtered_calls)

    def test_exact_year_resolves_same_title_candidates_at_margin_boundary(self):
        def fake_get(path, params):
            if path == "/search/tv":
                return {"results": [
                    {"id": 101, "name": "The Bridge", "original_name": "The Bridge",
                     "first_air_date": "2011-01-01"},
                    {"id": 102, "name": "The Bridge", "original_name": "The Bridge",
                     "first_air_date": "2013-01-01"},
                ]}
            if path == "/tv/101/external_ids":
                return {"imdb_id": "tt1234567"}
            return {}

        result = find_tmdb_match(
            {"media_type": "series", "provider_title": "The Bridge", "year": 2011},
            search_fn=fake_get,
        )
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["tmdb_id"], 101)
        self.assertEqual(result["candidate_year"], 2011)
        self.assertGreaterEqual(result["margin"], 0.08)

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

    def test_no_match_without_promising_candidates_refreshes_missing_evidence(self):
        catalog_db.record_unresolved_title(
            "aniworld", "https://aniworld.to/anime/stream/no-evidence",
            "Weak Provider Label", "series",
        )
        initial_no_match = {
            "status": "no_match",
            "diagnostics": {"candidates": [{"title_score": 0.42}]},
        }
        refreshed_match = {
            "status": "matched", "tmdb_id": 23456, "imdb_id": "tt2345678",
            "score": 0.96, "margin": 0.10,
        }
        refreshed_record = {
            "provider": "aniworld",
            "provider_url": "https://aniworld.to/anime/stream/no-evidence",
            "provider_title": "Weak Provider Label",
            "media_type": "series",
            "metadata": {
                "provider_structure": {"complete": True, "seasons": []},
                "alternative_titles": ["Useful International Title"],
            },
        }
        with patch("identity_resolver.find_tmdb_match", side_effect=[initial_no_match, refreshed_match]) as matcher, \
             patch("identity_resolver._refresh_provider_evidence", return_value=refreshed_record) as refresh:
            summary = resolve_unresolved_titles(provider="aniworld", apply=False)
        self.assertEqual(refresh.call_count, 1)
        self.assertEqual(matcher.call_count, 2)
        self.assertEqual(summary["matched"], 1)
        self.assertEqual(summary["results"][0]["status"], "matched")

    def test_alternative_titles_only_refresh_triggers_resolver_rerun(self):
        catalog_db.record_unresolved_title(
            "aniworld", "https://aniworld.to/anime/stream/alias-only",
            "Provider Name", "series",
        )
        initial = {
            "status": "ambiguous",
            "diagnostics": {"candidates": [{"title_score": 0.75}]},
        }
        refreshed_match = {
            "status": "matched", "tmdb_id": 12345, "imdb_id": "tt1234567",
            "score": 0.96, "margin": 0.10,
        }
        refreshed_record = {
            "provider": "aniworld",
            "provider_url": "https://aniworld.to/anime/stream/alias-only",
            "provider_title": "Provider Name",
            "media_type": "series",
            "metadata": {"alternative_titles": ["International Alias"]},
        }
        with patch("identity_resolver.find_tmdb_match", side_effect=[initial, refreshed_match]) as matcher, \
             patch("identity_resolver._refresh_provider_evidence", return_value=refreshed_record):
            summary = resolve_unresolved_titles(provider="aniworld", apply=False)
        self.assertEqual(matcher.call_count, 2)
        self.assertEqual(summary["matched"], 1)
        self.assertEqual(summary["results"][0]["status"], "matched")

    def test_batch_cache_reuses_duplicate_tmdb_search_requests(self):
        catalog_db.record_unresolved_title(
            "aniworld", "https://aniworld.to/anime/stream/example-one",
            "Example Anime", "series", year=2024,
        )
        catalog_db.record_unresolved_title(
            "serienstream", "https://serienstream.to/serie/example-one",
            "Example Anime", "series", year=2024,
        )
        calls = []

        def fake_get(path, params):
            calls.append((path, dict(params or {}))))
            if path == "/search/tv":
                return {"results": [
                    {"id": 555, "name": "Example Anime", "original_name": "Example Anime",
                     "first_air_date": "2024-01-01"}
                ]}
            return {}

        summary = resolve_unresolved_titles(apply=False, search_fn=fake_get)
        self.assertEqual(summary["scanned"], 2)
        search_calls = [call for call in calls if call[0] == "/search/tv"]
        self.assertEqual(len(search_calls), 1)

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
        catalog_db.record_unresolved_title(            "aniworld", url, "Example Anime", "series", year=2024,        )

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

    def test_large_year_gap_is_diagnostic_not_hard_exclusion(self):
        """Year mismatch ranks an exact-title candidate but does not exclude it."""
        def fake_get(path, params):
            if path == "/search/tv":
                return {"results": [{
                    "id": 220, "name": "Example Series",
                    "original_name": "Example Series",
                    "first_air_date": "2010-01-01",
                }]}
            return {"imdb_id": "tt1234567"}

        result = find_tmdb_match(
            {"media_type": "series", "provider_title": "Example Series", "year": 2017},
            search_fn=fake_get,
        )
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["candidate_year_delta"], 7)
        self.assertGreaterEqual(result["score"], 0.94)
        self.assertEqual(result["diagnostics"]["candidates"][0]["year"]["status"], "mismatch")
        self.assertFalse(result["diagnostics"]["candidates"][0]["year"]["used_for_exclusion"])

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
                    {"id": 43, "name": "Example", "original_name": "Example",                     "first_air_date": "2020-01-01"},
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


    def test_alias_query_candidate_outside_top_twenty_gets_alternative_title_lookup(self):
        unrelated = [
            {"id": 200 + i, "name": f"Unrelated Candidate {i}",
             "original_name": f"Unrelated Candidate {i}",
             "first_air_date": f"{1990 + i}-01-01"}
            for i in range(25)
        ]
        alias_candidate = {
            "id": 999, "name": "完全に別のタイトル",
            "original_name": "完全に別のタイトル", "first_air_date": "2020-01-01",
        }
        calls = []
        def fake_get(path, params):
            calls.append((path, params))
            if path == "/search/tv":
                if params.get("query") == "Alias Rescue Show":
                    return {"results": [alias_candidate]}
                return {"results": unrelated}
            if path == "/tv/999/alternative_titles":
                return {"results": [{"title": "Alias Rescue Show"}]}
            if path == "/tv/999/external_ids":
                return {"imdb_id": "tt1234567"}
            return {}
        find_tmdb_match(
            {"media_type": "series", "provider_title": "Unmatched Main Title",
             "year": 2020, "metadata": {"alternative_titles": ["Alias Rescue Show"]}},
            search_fn=fake_get,
        )
        self.assertIn("/tv/999/alternative_titles", [path for path, _ in calls])

    def test_alternative_title_shortlist_expands_beyond_initial_ten(self):
        candidates = [
            {"id": 200 + i, "name": f"Alias Rescue Candidate {i}",
             "original_name": f"Alias Rescue Candidate {i}",
             "first_air_date": f"{2000 + i}-01-01"}
            for i in range(1, 11)
        ]
        candidates.append({            "id": 999, "name": "完全に別のタイトル",
            "original_name": "完全に別のタイトル", "first_air_date": "2020-01-01",        })
        calls = []

        def fake_get(path, params):
            calls.append(path)
            if path == "/search/tv":
                return {"results": candidates}
            if path == "/tv/999/alternative_titles":
                return {"results": [{"title": "Alias Rescue Show"}]}
            if path == "/tv/999/external_ids":
                return {"imdb_id": "tt1234567"}
            return {}

        result = find_tmdb_match(
            {"media_type": "series", "provider_title": "Alias Rescue Show", "year": 2020},
            search_fn=fake_get,
        )
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["tmdb_id"], 999)
        self.assertIn("/tv/999/alternative_titles", calls)

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
                    "first_air_date": "2020-01-01",                }]}
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



    def test_page_two_exact_match_prevents_unnecessary_english_fallback(self):
        calls = []

        def fake_get(path, params):
            if path != "/search/tv":
                return {"imdb_id": "tt1234567"}
            calls.append((params.get("language"), params.get("page", 1)))
            if params.get("language") == "de-DE" and params.get("page", 1) == 1:
                return {"results": [{"id": 610, "name": "Unrelated Show",
                                     "original_name": "Unrelated Show"}]}
            if params.get("language") == "de-DE" and params.get("page") == 2:
                return {"results": [{"id": 611, "name": "Yuri Is My Job!",
                                     "original_name": "私の百合はお仕事です！"}]}
            return {"results": []}

        result = find_tmdb_match(
            {"media_type": "series", "provider_title": "Yuri Is My Job!"},
            search_fn=fake_get,
        )
        self.assertEqual(result["tmdb_id"], 611)
        self.assertNotIn(("en-US", 1), calls)

    def test_page_two_expands_second_query_variant_when_primary_is_weak(self):
        page_two_queries = []

        def fake_get(path, params):
            if path != "/search/tv":
                if path == "/tv/812/external_ids":
                    return {"imdb_id": "tt8123456"}
                return {}
            query = params.get("query", "")
            page = params.get("page", 1)
            lang = params.get("language")
            if lang == "de-DE" and page == 1:
                return {"results": [{"id": 801, "name": "Unrelated Result",
                                     "original_name": "Unrelated Result"}]}
            if lang == "de-DE" and page == 2:
                page_two_queries.append(query)
                if query == "Alpha Beta":
                    return {"results": [{"id": 812, "name": "Alpha Beta",
                                        "original_name": "Alpha Beta",
                                        "first_air_date": "2020-01-01"}]}
                return {"results": []}
            return {"results": []}

        result = find_tmdb_match(
            {"media_type": "series", "provider_title": "Alpha Beta (2020)", "year": 2020},
            search_fn=fake_get,
        )
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["tmdb_id"], 812)
        self.assertIn("Alpha Beta", page_two_queries)
        self.assertLessEqual(len(page_two_queries), 2)

    def test_page_two_prioritizes_provider_alias_over_cleanup_variant(self):
        page_two_queries = []
        alias = "Alpha Beta Romanized"

        def fake_get(path, params):
            if path != "/search/tv":
                if path == "/tv/812/external_ids":
                    return {"imdb_id": "tt8123456"}
                return {}
            query = params.get("query", "")
            page = params.get("page", 1)
            lang = params.get("language")
            if lang == "de-DE" and page == 1:
                return {"results": [{"id": 801, "name": "Unrelated Result",
                                     "original_name": "Unrelated Result"}]}
            if lang == "de-DE" and page == 2:
                page_two_queries.append(query)
                if query == alias:
                    return {"results": [{"id": 812, "name": alias,
                                        "original_name": alias,
                                        "first_air_date": "2020-01-01"}]}
                return {"results": []}
            return {"results": []}

        result = find_tmdb_match(
            {"media_type": "series", "provider_title": "Alpha Beta (2020)",
             "year": 2020, "metadata": {"alternative_titles": [alias]}},
            search_fn=fake_get,
        )
        self.assertEqual(result["status"], "matched", result)
        self.assertEqual(result["tmdb_id"], 812)
        self.assertIn(alias, page_two_queries)
        self.assertLessEqual(len(page_two_queries), 2)

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



    def test_episode_titles_alone_do_not_rescue_weak_title_match(self):
        def fake_get(path, params):
            if path == "/search/tv":
                return {"results": [{
                    "id": 209, "name": "Otome Yokai Zakuro",
                    "original_name": "おとめ妖怪 ざくろ",
                    "first_air_date": "2010-10-01",
                }]}
            if path == "/tv/209":
                return {
                    "number_of_seasons": 1, "number_of_episodes": 13,
                    "seasons": [{"season_number": 1, "episode_count": 13}],
                }
            if path == "/tv/209/season/1":
                return {"episodes": [
                    {"episode_number": 1, "name": "The First Meeting"},
                    {"episode_number": 2, "name": "The Strange Visitor"},
                    {"episode_number": 3, "name": "A Hidden Secret"},
                ]}
            return {}

        result = find_tmdb_match(
            {
                "provider": "serienstream",
                "media_type": "series",
                "provider_title": "Zakuro",
                "year": 2010,
                "metadata": {"provider_structure": {
                    "complete": True, "season_count": 1, "episode_count": 13,
                    "seasons": [{"season_number": 1, "episode_count": 13, "episodes": [
                        {"episode_number": 1, "title": "The First Meeting"},
                        {"episode_number": 2, "title": "The Strange Visitor"},
                        {"episode_number": 3, "title": "A Hidden Secret"},
                    ]}],
                }},
            },
            search_fn=fake_get,
        )
        self.assertNotEqual(result["status"], "matched")
        self.assertNotEqual(result["status"], "identified")


    def test_company_similarity_ignores_legal_entity_suffixes(self):
        score = _company_similarity(
            ["Studio Example GmbH"],
            ["Studio Example"],
        )
        self.assertEqual(score, 1.0)

    def test_country_score_does_not_count_as_second_independent_signal(self):
        values = _independent_corroboration_values(
            {"actor_score": 0.95, "director_score": None,
             "studio_score": None, "country_score": 1.0},
            {"actors": ["Actor A"], "directors": [], "country": "Japan"},
        )
        self.assertEqual(values, [0.95])

    def test_country_match_alone_is_not_independent_corroboration(self):
        def fake_get(path, params):
            if path == "/search/tv":
                return {"results": [{
                    "id": 220, "name": "Otome Yokai Zakuro",
                    "original_name": "おとめ妖怪 ざくろ",
                    "first_air_date": "2010-10-01",
                }]}
            if path == "/tv/220":
                return {"production_countries": [{"name": "Japan", "iso_3166_1": "JP"}]}
            return {}

        result = find_tmdb_match(
            {
                "provider": "serienstream", "media_type": "series",
                "provider_title": "Zakuro",
                "metadata": {"country": "Japan"},
            },
            search_fn=fake_get,
        )
        # Country agreement is diagnostic, but cannot replace independent
        # actor/director/studio corroboration.
        self.assertNotIn(result["status"], {"matched", "identified"})

    def test_candidate_outside_metadata_shortlist_can_be_rescued_by_credits(self):
        def fake_get(path, params):
            if path == "/search/tv":
                return {"results": [
                    {"id": 410, "name": "Zakuro Special 1", "original_name": "Zakuro Special 1", "first_air_date": "2024-01-01"},
                    {"id": 411, "name": "Zakuro Special 2", "original_name": "Zakuro Special 2", "first_air_date": "2024-01-01"},
                    {"id": 412, "name": "Zakuro Special 3", "original_name": "Zakuro Special 3", "first_air_date": "2024-01-01"},
                    {"id": 413, "name": "Zakuro Special 4", "original_name": "Zakuro Special 4", "first_air_date": "2024-01-01"},
                    {"id": 414, "name": "Zakuro Special 5", "original_name": "Zakuro Special 5", "first_air_date": "2024-01-01"},
                    {"id": 415, "name": "Otome Yokai Zakuro", "original_name": "おとめ妖怪 ざくろ", "first_air_date": "2010-10-01"},
                ]}
            if path == "/tv/415/credits":
                return {"cast": [{"name": "Actor One"}], "crew": [{"name": "Director One", "job": "Director"}]}
            if path == "/tv/415/external_ids":
                return {"imdb_id": "tt1738078"}
            return {}

        result = find_tmdb_match(
            {
                "provider": "serienstream", "media_type": "series",
                "provider_title": "Zakuro", "year": 2010,
                "metadata": {"actors": ["Actor One"], "directors": ["Director One"]},
            },
            search_fn=fake_get,
        )
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["tmdb_id"], 415)
        self.assertEqual(result["actor_score"], 1.0)
        self.assertEqual(result["director_score"], 1.0)

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

    def test_description_similarity_alone_does_not_rescue_weak_title(self):
        def fake_get(path, params):
            if path == "/search/tv":
                return {"results": [{
                    "id": 203, "name": "Otome Yokai Zakuro",
                    "original_name": "おとめ妖怪 ざくろ",
                }]}
            if path == "/tv/203/alternative_titles":
                return {"results": []}
            if path == "/tv/203":
                return {"overview": "A mysterious supernatural adventure involving spirits, friendship, romance, and a young heroine."}
            return {}

        result = find_tmdb_match(
            {
                "provider": "serienstream",
                "media_type": "series",
                "provider_title": "Zakuro",
                "description": "A mysterious supernatural adventure involving spirits, friendship, romance, and a young heroine.",
                "metadata": {},
            },
            search_fn=fake_get,
        )
        self.assertEqual(result["status"], "no_match")
        self.assertEqual(
            result["diagnostics"]["candidates"][0]["rejection"],
            "below_title_similarity_threshold",
        )

    def test_weak_title_without_two_independent_signals_is_not_rescued(self):
        def fake_get(path, params):
            if path == "/search/tv":
                return {"results": [{
                    "id": 202, "name": "Otome Yokai Zakuro",                    "original_name": "おとめ妖怪 ざくろ",
                }]}
            if path == "/tv/202/alternative_titles":                return {"results": []}
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

    def test_candidate_season_fetch_uses_numeric_first_and_last_when_unsorted(self):
        calls = []

        def fake_get(path, params):
            calls.append(path)
            return {"episodes": []}

        provider_structure = {"seasons": [
            {"season_number": 4, "episodes": []},
            {"season_number": 2, "episodes": []},
            {"season_number": 1, "episodes": []},
            {"season_number": 3, "episodes": []},
            {"season_number": 4, "episodes": []},
        ]}
        result = _candidate_season_episode_titles(
            {"id": 123}, provider_structure, fake_get
        )
        self.assertEqual(calls, [
            "/tv/123/season/1",
            "/tv/123/season/4",
        ])
        self.assertEqual(result, {"1": [], "4": []})

    def test_candidate_season_fetch_ignores_boolean_and_fractional_season_numbers(self):
        calls = []

        def fake_get(path, params):
            calls.append(path)
            return {"episodes": []}

        provider_structure = {"seasons": [
            {"season_number": True, "episodes": []},
            {"season_number": 1.5, "episodes": []},
            {"season_number": "2.5", "episodes": []},
            {"season_number": Decimal("1.5"), "episodes": []},
            {"season_number": 2.0, "episodes": []},
            {"season_number": "3", "episodes": []},
            {"season_number": Decimal("4.0"), "episodes": []},
        ]}
        result = _candidate_season_episode_titles(
            {"id": 123}, provider_structure, fake_get
        )
        self.assertEqual(calls, [
            "/tv/123/season/2",
            "/tv/123/season/4",
        ])
        self.assertEqual(result, {"2": [], "4": []})

    def test_verified_aniworld_movie_type_correction_is_applied_as_movie(self):
        url = "https://aniworld.to/anime/stream/animal-crossing"
        catalog_db.record_unresolved_title(
            "aniworld", url, "Animal Crossing", "series", year=2006,
            metadata={                "actors": ["Yui Horie", "Fumiko Orikasa"],
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

    def test_aniworld_rejected_tv_search_still_probes_movie_for_type_conflict(self):
        calls = []

        def fake_get(path, params):
            calls.append(path)
            if path == "/search/tv":
                return {"results": [{
                    "id": 777, "name": "Unrelated TV Result",
                    "original_name": "Unrelated TV Result", "first_air_date": "2003-01-01",
                }]}
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
        self.assertIn("/search/movie", calls)
        self.assertEqual(result["status"], "ambiguous")
        self.assertTrue(result.get("media_type_conflict"))
        self.assertEqual(
            result["diagnostics"]["alternate_movie_candidate"]["tmdb_id"], 11875
        )

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
                    "directors": ["Satoshi Kon"], "producer": "MADHOUSE Inc.",
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

    def test_kanon_year_distinguishes_2002_and_2006_versions(self):
        candidates = [
            {"id": 2002, "name": "Kanon", "original_name": "Kanon",
             "first_air_date": "2002-01-31"},
            {"id": 2006, "name": "Kanon", "original_name": "Kanon",
             "first_air_date": "2006-10-05"},
        ]

        def fake_get(path, params):
            if path == "/search/tv":
                return {"results": candidates}
            if path.endswith("/external_ids"):
                return {"imdb_id": "tt0409591" if "/2006/" in path else "tt0339955"}
            return {}

        for year, expected_id in ((2002, 2002), (2006, 2006)):
            with self.subTest(year=year):
                result = find_tmdb_match(
                    {"media_type": "series", "provider_title": "Kanon", "year": year},                    search_fn=fake_get,
                )
                self.assertEqual(result["status"], "matched")
                self.assertEqual(result["tmdb_id"], expected_id)
                self.assertEqual(result["candidate_year"], year)
                self.assertGreaterEqual(result["margin"], 0.08)

    def test_aniworld_parenthesized_year_is_not_counted_as_part_of_title(self):
        item = {"name": "Kanon", "original_name": "カノン"}
        score, matched = _best_provider_title_match(
            "Kanon (2006)", [], item, "series"
        )
        self.assertEqual(score, 1.0)
        self.assertEqual(matched, "Kanon")

    def test_aniworld_year_window_finds_candidates_with_one_or_two_year_date_drift(self):
        calls = []

        def fake_get(path, params):
            calls.append((path, dict(params)))
            if path == "/search/tv" and params.get("first_air_date_year") == 2002:
                return {"results": [{
                    "id": 2002, "name": "Kanon", "original_name": "Kanon",
                    "first_air_date": "2002-01-31",
                }]}
            if path == "/search/tv" and params.get("first_air_date_year") == 2006:
                return {"results": [{
                    "id": 2006, "name": "Kanon", "original_name": "Kanon",
                    "first_air_date": "2006-10-05",
                }]}
            if path.endswith("/external_ids"):
                return {"imdb_id": "tt0339955" if "/2002/" in path else "tt0409591"}
            return {}

        result = find_tmdb_match(
            {"provider": "aniworld", "media_type": "series",
             "provider_title": "Kanon", "year": 2004},
            search_fn=fake_get,
        )
        self.assertEqual(result["status"], "ambiguous")
        self.assertEqual(result["diagnostics"]["search"]["tmdb_result_count"], 2)
        self.assertTrue(any(params.get("first_air_date_year") == 2002 for _, params in calls))
        self.assertTrue(any(params.get("first_air_date_year") == 2006 for _, params in calls))

    def test_kanon_aniworld_display_year_selects_matching_tmdb_version(self):
        candidates = [
            {"id": 2002, "name": "Kanon", "original_name": "カノン",
             "first_air_date": "2002-01-31"},
            {"id": 2006, "name": "Kanon", "original_name": "カノン",
             "first_air_date": "2006-10-05"},
        ]

        def fake_get(path, params):
            if path == "/search/tv":
                return {"results": candidates}
            if path == "/tv/2002/credits":
                return {"cast": [{"name": "A"}], "crew": []}
            if path == "/tv/2006/credits":
                return {"cast": [{"name": "A"}], "crew": []}
            if path == "/tv/2002":
                return {}
            if path == "/tv/2006":
                return {}
            if path.endswith("/external_ids"):
                return {"imdb_id": "tt0339955" if "/2002/" in path else "tt0409591"}
            return {}

        for year, expected_id in ((2002, 2002), (2006, 2006)):
            with self.subTest(year=year):
                result = find_tmdb_match(
                    {"provider": "aniworld", "media_type": "series",
                     "provider_title": f"Kanon ({year})", "year": year},
                    search_fn=fake_get,
                )
                self.assertEqual(result["status"], "matched")
                self.assertEqual(result["tmdb_id"], expected_id)
                self.assertEqual(result["title_score"], 1.0)

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


    def test_normalization_preserves_japanese_voicing_marks_but_folds_latin_accents(self):
        # Synthetic strings: voicing marks distinguish Japanese characters, while
        # Latin accent folding remains useful for alternate-title matching.
        self.assertNotEqual(_normalize("か"), _normalize("が"))
        self.assertNotEqual(_normalize("カ"), _normalize("ガ"))
        self.assertEqual(_normalize("Cafe"), _normalize("Café"))

        score, matched = _best_title_match(
            "が", {"name": "か", "original_name": "か"}, "series"
        )
        self.assertLess(score, 1.0)
        self.assertEqual(matched, "か")

    def test_search_variants_prioritize_parenthetical_year_cleanup(self):
        variants = _search_query_variants(
            "Kanon (2006)", ["カノン", "Kanon", "Kanon 2006", "Alias Four", "Alias Five"]
        )
        self.assertEqual(variants[:3], ["Kanon (2006)", "Kanon", "カノン"])
        self.assertIn("カノン", variants)
        self.assertIn("Alias Four", variants)
        self.assertLessEqual(len(variants), 7)

    def test_aniworld_alternate_japanese_title_can_match_tmdb_original_title(self):
        variants = _search_query_variants(
            "The Space Between (2012)", ["間の楔", "Ai no Kusabi"]
        )
        self.assertIn("間の楔", variants)
        score, matched = _best_provider_title_match(
            "The Space Between (2012)", ["間の楔", "Ai no Kusabi"],
            {"name": "The Space Between", "original_name": "間の楔"},
            "series",
        )
        self.assertEqual(score, 1.0)
        self.assertEqual(matched, "間の楔")

    def test_aniworld_format_suffix_adds_clean_base_title_query(self):
        variants = _search_query_variants("Loveless: SD Theatre")
        self.assertEqual(variants[0], "Loveless: SD Theatre")
        self.assertIn("Loveless", variants)

    def test_alternative_title_extraction_rejects_malformed_values_and_deduplicates(self):
        payload = {
            "results": [
                {"title": "  Synthetic Alias  "},
                {"title": {"nested": "bad"}, "name": "Valid fallback"},
                {"title": 123},
                {"name": ["Not a string"]},
                {"title": "!!!"},
                {"title": "synthetic alias"},
            ],
            "titles": [{"name": "Alternate Alias"}, {"title": None}],
        }
        self.assertEqual(
            _alternative_title_values(payload),
            ["Synthetic Alias", "Valid fallback", "Alternate Alias"],
        )
        self.assertEqual(_alternative_title_values(None), [])
        self.assertEqual(_alternative_title_values(["bad payload"]), [])

    def test_search_variants_ignore_empty_and_duplicate_aliases(self):
        aliases = [
            "!!!", "Alias A", "alias-a", "Alias B", "Alias C",
            "Alias D", "Alias E", "Alias F", "Alias G",
        ]
        variants = _search_query_variants("!!!", aliases)
        self.assertEqual(
            variants,
            ["Alias A", "Alias B", "Alias C", "Alias D", "Alias E", "Alias F", "Alias G"],
        )
        self.assertEqual(len(variants), 7)

    def test_resolver_search_ignores_non_string_provider_aliases(self):
        calls = []

        def fake_get(path, params):
            calls.append((path, dict(params or {}))
            if path == "/search/tv":
                return {"results": []}
            return {}

        result = find_tmdb_match(
            {
                "provider": "fixture",
                "media_type": "series",
                "provider_title": "Alpha Show",
                "year": None,
                "metadata": {
                    "alternative_titles": [
                        {"title": "Injected Alias"}, 73, None, " Valid Alias ",
                        ["Nested Alias"], "!!!",
                    ],
                },
            },
            search_fn=fake_get,
        )
        self.assertEqual(result["status"], "no_match")
        queries = {
            params.get("query")
            for path, params in calls
            if path == "/search/tv"
        }
        self.assertEqual(queries, {"Alpha Show", "Valid Alias"})

    def test_invalid_primary_title_can_find_candidate_from_alias_search(self):
        calls = []
        candidate = {
            "id": 990000001,
            "name": "Fixture Alias Rescue",
            "original_name": "Fixture Alias Rescue",
        }

        def fake_get(path, params):
            calls.append((path, dict(params or {})))
            if path == "/search/tv" and params.get("query") == "Fixture Alias Rescue":
                return {"results": [candidate]}
            return {}

        result = find_tmdb_match(
            {
                "provider": "fixture",
                "media_type": "series",
                "provider_title": "!!!",
                "year": None,
                "metadata": {"alternative_titles": ["Fixture Alias Rescue"]},
            },
            search_fn=fake_get,
        )
        self.assertEqual(result["status"], "identified")
        self.assertEqual(result["tmdb_id"], 990000001)
        self.assertTrue(any(
            path == "/search/tv" and params.get("query") == "Fixture Alias Rescue"
            for path, params in calls
        ))

    def test_episode_title_score_is_invariant_to_german_english_variant_order(self):
        provider_structure = {
            "seasons": [{
                "season_number": 1,
                "episodes": [
                    {"title": "Der Anfang", "title_original": "The Beginning"},
                    {"title": "Die Reise", "title_original": "The Journey"},
                    {"title": "Das Ende", "title_original": "The End"},
                ],
            }],
        }
        candidate_titles = {"1": [
            {"episode_number": 1, "name": "The Beginning", "original_name": "Der Anfang"},
            {"episode_number": 2, "name": "The Journey", "original_name": "Die Reise"},
            {"episode_number": 3, "name": "The End", "original_name": "Das Ende"},
        ]}
        baseline = _episode_title_similarity(provider_structure, candidate_titles)
        # Swapping which language is listed first must not change the score:
        # all cross-language title variants are compared, not positional pairs.
        swapped_provider = {
            "seasons": [{
                "season_number": 1,
                "episodes": [
                    {"title": episode["title_original"], "title_original": episode["title"]}
                    for episode in provider_structure["seasons"][0]["episodes"]
                ],
            }],
        }
        swapped_candidate = {"1": [
            {**episode, "name": episode["original_name"], "original_name": episode["name"]}
            for episode in candidate_titles["1"]
        ]}
        swapped = _episode_title_similarity(swapped_provider, swapped_candidate)
        self.assertEqual(baseline, 1.0)
        self.assertEqual(swapped, baseline)

    def test_merged_episode_title_comparison_uses_candidate_language_variants(self):
        provider_structure = {
            "seasons": [{
                "season_number": 1,
                "episodes": [
                    {"episode_number": 1, "title": "Der erste Teil / Der zweite Teil"},
                    {"episode_number": 2, "title": "Der dritte Teil"},
                    {"episode_number": 3, "title": "Der vierte Teil"},
                ],
            }],
        }
        candidate_titles = {"1": [
            {"episode_number": 1, "name": "First Part", "original_name": "Der erste Teil"},
            {"episode_number": 2, "name": "Second Part", "original_name": "Der zweite Teil"},
            {"episode_number": 3, "name": "Third Part", "original_name": "Der dritte Teil"},
            {"episode_number": 4, "name": "Fourth Part", "original_name": "Der vierte Teil"},
        ]}
        score = _episode_title_similarity(provider_structure, candidate_titles)
        self.assertGreaterEqual(score, 0.99)

    def test_duplicate_provider_seasons_are_excluded_from_episode_title_evidence(self):
        season_one = {
            "season_number": 1,
            "episodes": [
                {"title": "Synthetic Alpha"},
                {"title": "Synthetic Bravo"},
                {"title": "Synthetic Charlie"},
            ],
        }
        provider_structure = {"seasons": [
            season_one,
            {**season_one},
            {
                "season_number": 2,
                "episodes": [
                    {"title": "Provider Omega"},
                    {"title": "Provider Delta"},
                    {"title": "Provider Sigma"},
                ],
            },
        ]}
        candidate_titles = {
            "1": [
                {"name": "Synthetic Alpha"},
                {"name": "Synthetic Bravo"},
                {"name": "Synthetic Charlie"},
            ],
            "2": [
                {"name": "Candidate Kappa"},
                {"name": "Candidate Theta"},
                {"name": "Candidate Lambda"},
            ],
        }
        score = _episode_title_similarity(provider_structure, candidate_titles)
        # A duplicate season must not let its perfect matches dominate the
        # unrelated season's lack of corroboration.
        self.assertEqual(score, 0.0)

    def test_episode_title_similarity_does_not_reuse_candidate_episode(self):
        provider_structure = {
            "seasons": [{
                "season_number": 1,
                "episodes": [
                    {"title": "Alpha"},
                    {"title": "Alpha"},
                    {"title": "Alpha"},
                ],
            }],
        }
        candidate_titles = {"1": [
            {"name": "Alpha"},
            {"name": "Bravo"},
            {"name": "Charlie"},
        ]}
        score = _episode_title_similarity(provider_structure, candidate_titles)
        # One candidate episode must not corroborate multiple provider episodes.
        self.assertAlmostEqual(score, 1 / 3, places=4)

    def test_episode_title_matching_uses_augmenting_paths_to_avoid_greedy_misses(self):
        provider_structure = {"seasons": [{
            "season_number": 1,
            "episodes": [
                {"title": "rainfxll at dawn"},
                {"title": "rainfall at dark"},
                {"title": "Gamma"},
            ],
        }]}
        candidate_titles = {"1": [
            {"name": "rainfxll at dawn"},
            {"name": "rainfall dawn"},
            {"name": "Gamma"},
        ]}
        # The first provider episode's exact match would block the second
        # episode's only viable match in a greedy assignment. Reassignment
        # yields three distinct one-to-one episode matches.
        score = _episode_title_similarity(provider_structure, candidate_titles)
        self.assertEqual(score, 1.0)

    def test_episode_title_comparison_tolerates_one_merged_provider_episode(self):
        provider_structure = {
            "seasons": [{
                "season_number": 1,
                "episodes": [
                    {"episode_number": 1, "title": "The First Part / The Second Part"},
                    {"episode_number": 2, "title": "The Third Episode"},
                    {"episode_number": 3, "title": "The Fourth Episode"},
                ],
            }],
        }
        candidate_titles = {"1": [
            {"episode_number": 1, "name": "The First Part"},
            {"episode_number": 2, "name": "The Second Part"},
            {"episode_number": 3, "name": "The Third Episode"},
            {"episode_number": 4, "name": "The Fourth Episode"},
        ]}
        score = _episode_title_similarity(provider_structure, candidate_titles)
        self.assertGreaterEqual(score, 0.75)

    def test_greedy_merged_match_does_not_block_two_direct_matches(self):
        provider_structure = {"seasons": [{
            "season_number": 1,
            "episodes": [
                {"title": "Rainfall at dawn / Copper lanterns"},
                {"title": "Rainfall at dawn extra"},
                {"title": "Copper lanterns extra"},
            ],
        }]}
        candidate_titles = {"1": [
            {"name": "Rainfall at dawn"},
            {"name": "Copper lanterns"},
            {"name": "Winter signals"},
        ]}
        # The merged perfect match would block two direct matches (0.84 and
        # 0.83). The direct-first strategy has better balanced coverage (2/3).
        score = _episode_title_similarity(provider_structure, candidate_titles)
        self.assertAlmostEqual(score, 2 / 3, places=4)


    def test_exact_year_and_one_strong_credit_can_rescue_translated_title(self):
        def fake_get(path, params):
            if path == "/search/tv":
                return {"results": [{
                    "id": 36406, "name": "Yu-Gi-Oh! Season Zero",
                    "original_name": "遊☆戯☆王", "first_air_date": "1998-04-04",
                }]}
            if path == "/tv/36406/credits":
                return {"cast": [{"name": "Yugi Muto"}], "crew": []}
            if path == "/tv/36406/external_ids":
                return {"imdb_id": "tt4834194"}
            return {}

        result = find_tmdb_match(
            {
                "provider": "aniworld", "media_type": "series",
                "provider_title": "Yu-Gi-Oh! Zero", "year": 1998,
                "metadata": {"actors": ["Yugi Muto"]},
            },
            search_fn=fake_get,
        )
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["imdb_id"], "tt4834194")


    def test_multi_movie_aniworld_collection_is_not_assigned_one_identity(self):
        calls = []

        def fake_get(path, params):
            calls.append(path)
            return {}

        result = find_tmdb_match(
            {
                "provider": "aniworld", "media_type": "series",
                "provider_title": "Persona 3: The Movie", "year": 2013,
                "metadata": {
                    "multi_movie_collection": True,
                    "movie_collection_count": 4,
                    "provider_structure": {"complete": True, "episode_count": 4},
                },
            },
            search_fn=fake_get,
        )
        self.assertEqual(result["status"], "ambiguous")
        self.assertTrue(result["multi_movie_collection"])
        self.assertIn("multiple independent film parts", result["reason"])
        self.assertEqual(calls, [])


    def test_tenchi_muyo_exact_year_breaks_identical_title_tie(self):
        def fake_get(path, params):
            if path == "/search/tv":
                return {"results": [
                    {"id": 43004, "name": "Tenchi Muyo!", "original_name": "Tenchi Muyo!",
                     "first_air_date": "1992-04-02"},
                    {"id": 20451, "name": "Tenchi Muyo!", "original_name": "Tenchi Muyo!",
                     "first_air_date": "1995-04-02"},
                ]}
            if path == "/tv/43004/external_ids":
                return {"imdb_id": "tt0105865"}
            return {}

        result = find_tmdb_match(
            {"media_type": "series", "provider_title": "Tenchi Muyo!", "year": 1992},
            search_fn=fake_get,
        )
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["tmdb_id"], 43004)
        self.assertEqual(result["candidate_year"], 1992)


    def test_episode_title_similarity_ignores_invalid_season_numbers(self):
        invalid_season_numbers = (True, 1.5, Decimal("1.5"), float("inf"))
        provider_structure = {
            "seasons": [
                {
                    "season_number": season_number,
                    "episodes": [
                        {"episode_number": 1, "title": "Synthetic episode alpha"},
                        {"episode_number": 2, "title": "Synthetic episode beta"},
                        {"episode_number": 3, "title": "Synthetic episode gamma"},
                    ],
                }
                for season_number in invalid_season_numbers
            ],
        }
        candidate_titles = {"1": [
            {"episode_number": 1, "name": "Synthetic episode alpha"},
            {"episode_number": 2, "name": "Synthetic episode beta"},
            {"episode_number": 3, "name": "Synthetic episode gamma"},
        ]}
        self.assertIsNone(_episode_title_similarity(provider_structure, candidate_titles))

        integral_float_structure = {"seasons": [{
            "season_number": 1.0,
            "episodes": [
                {"episode_number": 1, "title": "Synthetic episode alpha"},
                {"episode_number": 2, "title": "Synthetic episode beta"},
                {"episode_number": 3, "title": "Synthetic episode gamma"},
            ],
        }]}
        self.assertEqual(
            _episode_title_similarity(integral_float_structure, candidate_titles), 1.0
        )


    def test_episode_title_comparison_does_not_cross_match_different_seasons(self):
        provider_structure = {
            "seasons": [
                {"season_number": 1, "episodes": [
                    {"episode_number": 1, "title": "Season One Alpha"},
                    {"episode_number": 2, "title": "Season One Beta"},
                    {"episode_number": 3, "title": "Season One Gamma"},
                ]},
                {"season_number": 2, "episodes": [
                    {"episode_number": 1, "title": "Other Arc Delta"},
                    {"episode_number": 2, "title": "Other Arc Epsilon"},
                    {"episode_number": 3, "title": "Other Arc Zeta"},
                ]},
            ],
        }
        candidate_titles = {
            "1": [
                {"episode_number": 1, "name": "Unrelated One"},
                {"episode_number": 2, "name": "Unrelated Two"},
                {"episode_number": 3, "name": "Unrelated Three"},
            ],
            "2": [
                {"episode_number": 1, "name": "Season One Alpha"},
                {"episode_number": 2, "name": "Season One Beta"},
                {"episode_number": 3, "name": "Season One Gamma"},
            ],
        }
        score = _episode_title_similarity(provider_structure, candidate_titles)
        self.assertIsNotNone(score)
        self.assertEqual(score, 0.0)

    def test_german_generic_episode_titles_do_not_provide_identity_evidence(self):
        provider_structure = {
            "seasons": [{
                "season_number": 1,
                "episodes": [
                    {"episode_number": 1, "title": "Folge 1"},
                    {"episode_number": 2, "title": "Folge 2"},
                    {"episode_number": 3, "title": "Folge 3"},
                    {"episode_number": 4, "title": "Folge 4"},
                ],
            }],
        }
        candidate_titles = {"1": [
            {"episode_number": 1, "name": "Folge 1"},
            {"episode_number": 2, "name": "Folge 2"},
            {"episode_number": 3, "name": "Folge 3"},
            {"episode_number": 4, "name": "Folge 4"},        ]}
        score = _episode_title_similarity(provider_structure, candidate_titles)
        self.assertIsNone(score)

    def test_japanese_season_chapter_part_and_volume_labels_are_generic(self):
        generic_titles = ["第1期", "第二章", "第3部", "第4巻", "第〇期", "第零章", "第廿部"]
        provider_structure = {"seasons": [{"season_number": 1, "episodes": [
            {"episode_number": index, "title": title}
            for index, title in enumerate(generic_titles, 1)
        ] + [
            {"episode_number": 8, "title": "Rainfall at dawn"},
            {"episode_number": 9, "title": "Copper lanterns"},
            {"episode_number": 10, "title": "Silent orchard"},
        ]}]}
        candidate_titles = {"1": [
            {"episode_number": index, "name": title}
            for index, title in enumerate(generic_titles, 1)
        ] + [
            {"episode_number": 8, "name": "Quantum drift"},
            {"episode_number": 9, "name": "Blue velvet"},
            {"episode_number": 10, "name": "Winter signals"},
        ]}
        self.assertEqual(_episode_title_similarity(provider_structure, candidate_titles), 0.0)


    def test_numbered_prefix_with_short_generic_residue_is_not_identity_evidence(self):
        provider_structure = {"seasons": [{
            "season_number": 1,
            "episodes": [
                {"episode_number": 1, "title": "Episode 1: A"},
                {"episode_number": 2, "title": "Episode 2: The"},
                {"episode_number": 3, "title": "Episode 3: Yes"},
            ],
        }]}
        candidate_titles = {"1": [
            {"episode_number": 1, "name": "A"},
            {"episode_number": 2, "name": "The"},
            {"episode_number": 3, "name": "Yes"},
        ]}
        # Removing episode numbers must not turn short generic residues into
        # three perfect identity matches.
        self.assertIsNone(_episode_title_similarity(provider_structure, candidate_titles))
    def test_numbered_prefix_with_non_latin_episode_titles_keeps_identity_evidence(self):
        provider_titles = ["雨の朝の駅", "青い空の下", "静かな森の道"]
        provider_structure = {"seasons": [{
            "season_number": 1,
            "episodes": [
                {"episode_number": index, "title": f"第{index}話: {title}"}
                for index, title in enumerate(provider_titles, 1)
            ],
        }]}
        candidate_titles = {"1": [
            {"episode_number": index, "name": title}
            for index, title in enumerate(provider_titles, 1)
        ]}
        # Meaningful CJK titles must not be dropped by an ASCII-only length check.
        self.assertEqual(_episode_title_similarity(provider_structure, candidate_titles), 1.0)


    def test_numbered_episode_prefixes_are_ignored_for_title_similarity(self):
        provider_structure = {"seasons": [{
            "season_number": 1,
            "episodes": [
                {"episode_number": 1, "title": "Episode 01: Rainfall at dawn"},
                {"episode_number": 2, "title": "第2話：Copper lanterns"},
                {"episode_number": 3, "title": "제3화: Silent orchard"},
                {"episode_number": 4, "title": "제4회: Amber bridge"},
            ],
        }]}
        candidate_titles = {"1": [
            {"episode_number": 1, "name": "Rainfall at dawn"},
            {"episode_number": 2, "name": "Copper lanterns"},
            {"episode_number": 3, "name": "Silent orchard"},
            {"episode_number": 4, "name": "Amber bridge"},
        ]}
        self.assertEqual(
            _episode_title_similarity(provider_structure, candidate_titles),
            1.0,
        )

    def test_simplified_chinese_and_formal_japanese_episode_prefixes_are_ignored(self):
        provider_structure = {"seasons": [{
            "season_number": 1,
            "episodes": [
                {"episode_number": 1, "title": "第1话: Rainfall at dawn"},
                {"episode_number": 2, "title": "第壱話: Copper lanterns"},
                {"episode_number": 3, "title": "第弐集: Silent orchard"},
            ],
        }]}
        candidate_titles = {"1": [
            {"episode_number": 1, "name": "Rainfall at dawn"},
            {"episode_number": 2, "name": "Copper lanterns"},
            {"episode_number": 3, "name": "Silent orchard"},
        ]}
        self.assertEqual(
            _episode_title_similarity(provider_structure, candidate_titles),
            1.0,
        )

    def test_generic_episode_titles_do_not_provide_identity_evidence(self):
        # Numbering-only Japanese, Chinese, and Korean episode labels are not identity evidence.
        generic_titles = [
            "第1話", "第二話", "第3集", "第4回", "第5話", "第6集",
            "第7話", "第8回", "第9話", "第10集", "第十一話", "第〇話", "第零集",
            "第两回", "第廿話", "第壱話", "第弐集", "第参回", "第拾話", "第1话", "第二话", "第两集", "第廿话", "제12화", "13편", "1회", "제2회", "3회",
        ]
        provider_structure = {
            "seasons": [{
                "season_number": 1,
                "episodes": [
                    {"episode_number": index, "title": title}
                    for index, title in enumerate(generic_titles, 1)
                ] + [
                    {"episode_number": 14, "title": "Rainfall at dawn"},
                    {"episode_number": 15, "title": "Copper lanterns"},
                    {"episode_number": 16, "title": "Silent orchard"},
                ],
            }],
        }
        candidate_titles = {"1": [
            {"episode_number": index, "name": title}
            for index, title in enumerate(generic_titles, 1)
        ] + [
            {"episode_number": 14, "name": "Quantum drift"},
            {"episode_number": 15, "name": "Blue velvet"},
            {"episode_number": 16, "name": "Winter signals"},
        ]}
        score = _episode_title_similarity(provider_structure, candidate_titles)
        # Without filtering, 13/16 identical labels would exceed the 0.78 threshold.
        # The 3 meaningful titles do not match, so the filtered score must be zero.
        self.assertEqual(score, 0.0)

    def test_structure_comparison_rejects_boolean_and_fractional_summary_counts(self):
        provider_structure = {"complete": True, "season_count": True, "episode_count": 12}
        candidate_details = {"number_of_seasons": 1, "number_of_episodes": 12}
        self.assertEqual(
            _structure_comparison(provider_structure, candidate_details)["status"],
            "not_available",
        )
        provider_structure = {"complete": True, "season_count": 1, "episode_count": 12.5}
        candidate_details = {"number_of_seasons": 1, "number_of_episodes": 12}
        self.assertEqual(
            _structure_comparison(provider_structure, candidate_details)["status"],
            "not_available",
        )
        provider_structure = {"complete": True, "season_count": 1, "episode_count": 12}
        candidate_details = {"number_of_seasons": 1.5, "number_of_episodes": 12}
        self.assertEqual(
            _structure_comparison(provider_structure, candidate_details)["status"],
            "not_available",
        )

    def test_structure_comparison_rejects_fractional_decimal_counts_and_season_details(self):
        provider_structure = {
            "complete": True, "season_count": 1, "episode_count": Decimal("12.5")
        }
        candidate_details = {
            "number_of_seasons": 1, "number_of_episodes": 12
        }
        self.assertEqual(
            _structure_comparison(provider_structure, candidate_details)["status"],
            "not_available",
        )

        provider_structure = {
            "complete": True, "season_count": 2, "episode_count": 24,
            "seasons": [
                {"season_number": Decimal("1.5"), "episode_count": Decimal("12.5")},
                {"season_number": 2, "episode_count": 12},
            ],
        }
        candidate_details = {
            "number_of_seasons": 2, "number_of_episodes": 24,
            "seasons": [
                {"season_number": Decimal("1.5"), "episode_count": Decimal("12.5")},
                {"season_number": 2, "episode_count": 12},
            ],
        }
        result = _structure_comparison(provider_structure, candidate_details)
        self.assertEqual(result["status"], "match")
        self.assertEqual(result["provider"]["seasons"], {"2": 12})
        self.assertEqual(result["tmdb"]["seasons"], {"2": 12})

        provider_structure = {
            "complete": True, "season_count": Decimal("1.0"),
            "episode_count": Decimal("12.0"),
            "seasons": [
                {"season_number": Decimal("1.0"), "episode_count": Decimal("12.0")}
            ],
        }
        candidate_details = {
            "number_of_seasons": Decimal("1.0"),
            "number_of_episodes": Decimal("12.0"),
            "seasons": [
                {"season_number": Decimal("1.0"), "episode_count": Decimal("12.0")}
            ],
        }
        result = _structure_comparison(provider_structure, candidate_details)
        self.assertEqual(result["status"], "match")
        self.assertEqual(result["provider"]["seasons"], {"1": 12})
        self.assertEqual(result["tmdb"]["seasons"], {"1": 12})

    def test_structure_comparison_rejects_boolean_and_fractional_season_values(self):
        provider_structure = {
            "complete": True, "season_count": 1, "episode_count": 12,
            "seasons": [
                {"season_number": True, "episode_count": 12},
                {"season_number": 1.5, "episode_count": 12},
                {"season_number": 1, "episode_count": 2.5},
            ],
        }
        candidate_details = {
            "number_of_seasons": 1, "number_of_episodes": 12,
            "seasons": [
                {"season_number": True, "episode_count": 12},
                {"season_number": 1.5, "episode_count": 12},
                {"season_number": 1, "episode_count": 2.5},
            ],
        }
        result = _structure_comparison(provider_structure, candidate_details)
        self.assertEqual(result["provider"]["seasons"], {})
        self.assertEqual(result["tmdb"]["seasons"], {})

    def test_structure_comparison_ignores_duplicate_season_numbers(self):
        provider_structure = {
            "complete": True,
            "season_count": 2,
            "episode_count": 24,
            "seasons": [
                {"season_number": 1, "episode_count": 12},
                {"season_number": 1, "episode_count": 11},
                {"season_number": 2, "episode_count": 12},
            ],
        }
        candidate_details = {
            "number_of_seasons": 2,
            "number_of_episodes": 24,
            "seasons": [
                {"season_number": 1, "episode_count": 12},
                {"season_number": 1, "episode_count": 12},
                {"season_number": 2, "episode_count": 12},
            ],
        }
        result = _structure_comparison(provider_structure, candidate_details)
        self.assertEqual(result["status"], "match")
        self.assertFalse(result["mismatch"])
        # Duplicated season 1 data is ambiguous and must not be used for
        # corroboration; the unique season 2 record remains comparable.
        self.assertEqual(result["provider"]["seasons"], {"2": 12})
        self.assertEqual(result["tmdb"]["seasons"], {"2": 12})


    def test_structure_comparison_ignores_malformed_per_season_counts(self):
        provider_structure = {
            "complete": True,
            "season_count": 2,
            "episode_count": 24,
            "seasons": [
                {"season_number": 1, "episode_count": "bad"},
                {"season_number": None, "episode_count": 12},
                {"season_number": 2, "episode_count": 12},
                "not-a-season",
            ],
        }
        candidate_details = {
            "number_of_seasons": 2,
            "number_of_episodes": 24,
            "seasons": [
                {"season_number": 1, "episode_count": None},
                {"season_number": "invalid", "episode_count": 12},
                {"season_number": 2, "episode_count": 12},
                {"not-a-season"},
            ],
        }
        result = _structure_comparison(provider_structure, candidate_details)
        self.assertEqual(result["status"], "match")
        self.assertFalse(result["mismatch"])
        self.assertEqual(result["provider"]["seasons"], {"2": 12})
        self.assertEqual(result["tmdb"]["seasons"], {"2": 12})


    def test_verified_cross_provider_identity_benchmark_exact_title_cases(self):
        """Regression benchmark based on verified cross-provider IMDb mappings."""
        verified_cases = [
            (".hack//Sign", 2000, 361, "tt0361140"),
            ("07-Ghost", 2009, 362, "tt1424033"),
            (("11eyes"), 2009, 363, "tt1515976"),
            ("16bit Sensation: Another Layer", 2023, 364, "tt29143189"),
            (("18if"), 2017, 365, "tt8020634"),
            (("22/7"), 2020, 366, "tt12024510"),
            (("7 Seeds"), 2019, 367, "tt9348718"),
            (("86: Eighty Six"), 2021, 368, "tt13718450"),
            (("91 Days"), 2016, 369, "tt5839706"),
            (("ARP Backstage Pass"), 2020, 370, "tt13752200"),
            (("ATRI -My Dear Moments-"), 2024, 371, "tt30326044"),
            (("AYAKA"), 2023, 372, "tt28266753"),
        ]
        for title, year, tmdb_id, imdb_id in verified_cases:
            with self.subTest(title=title):
                def fake_get(path, params):
                    if path == "/search/tv":
                        if params.get("query", "").casefold() == title.casefold():
                            return {"results": [{
                                "id": tmdb_id, "name": title,
                                "original_name": title,
                                "first_air_date": f"{year}-01-01",
                            }]}
                        return {"results": []}
                    if path == f"/tv/{tmdb_id}/external_ids":
                        return {"imdb_id": imdb_id}
                    return {}
                result = find_tmdb_match(
                    {"media_type": "series", "provider_title": title, "year": year},
                    search_fn=fake_get,
                )
                self.assertEqual(result["status"], "matched", result)
                self.assertEqual(result["tmdb_id"], tmdb_id)
                self.assertEqual(result["imdb_id"], imdb_id)
