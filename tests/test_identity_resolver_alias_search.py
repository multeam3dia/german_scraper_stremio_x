"""Focused regression tests for bounded resolver search query discovery."""
import unittest

from identity_resolver import find_tmdb_match


class ProviderAliasSearchTests(unittest.TestCase):
    def test_fourth_provider_alias_survives_derived_query_budget(self):
        provider_title = "Alias Priority: Special (2020) - OVA"
        aliases = ["Alternate One", "Alternate Two", "Alternate Three", "Alternate Four"]
        searched_queries = []

        def fake_get(path, params):
            if path == "/search/tv":
                query = params.get("query")
                searched_queries.append(query)
                if query == "Alternate Four":
                    return {"results": [{
                        "id": 1,
                        "name": "Alternate Four",
                        "original_name": "架空の候補",
                        "first_air_date": "2020-01-01",
                    }]}
                return {"results": []}
            if path == "/tv/1/external_ids":
                return {}
            return {}

        result = find_tmdb_match(
            {
                "media_type": "series",
                "provider_title": provider_title,
                "year": 2020,
                "metadata": {"alternative_titles": aliases},
            },
            search_fn=fake_get,
        )

        self.assertIn("Alternate Four", searched_queries)
        self.assertEqual(result["status"], "identified")
        self.assertEqual(result["tmdb_id"], 1)


    def test_provider_alias_query_can_discover_japanese_candidate_and_confirm_tmdb_alias(self):
        provider_title = "Localized Provider Name"
        provider_alias = "Known English Alias"
        searched_queries = []

        def fake_get(path, params):
            if path == "/search/tv":
                query = params.get("query")
                searched_queries.append(query)
                if query == provider_alias:
                    return {"results": [{
                        "id": 77,
                        "name": "日本語タイトル",
                        "original_name": "日本語タイトル",
                        "first_air_date": "2019-01-01",
                    }]}
                return {"results": []}
            if path == "/tv/77/alternative_titles":
                return {"results": [{"title": provider_alias, "iso_3166_1": "US"}]}
            if path == "/tv/77/external_ids":
                return {"imdb_id": "tt1234567"}
            return {}

        result = find_tmdb_match(
            {
                "media_type": "series",
                "provider_title": provider_title,
                "year": 2019,
                "metadata": {"alternative_titles": [provider_alias]},
            },
            search_fn=fake_get,
        )

        self.assertIn(provider_alias, searched_queries)
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["tmdb_id"], 77)
        self.assertEqual(result["title_score"], 1.0)


    def test_query_budget_deduplicates_aliases_and_never_exceeds_seven(self):
        from identity_resolver import _search_query_variants

        queries = _search_query_variants(
            "Primary Title: Special",
            [
                "", "Primary Title Special", "Alias A", "Alias A",
                "Alias B", "Alias C", "Alias D", "Alias E",
            ],
        )

        self.assertLessEqual(len(queries), 7)
        self.assertEqual(len(queries), len(set(queries)))
        self.assertIn("Alias A", queries)
        self.assertIn("Alias D", queries)
        self.assertNotIn("", queries)

    def test_alias_deduplication_uses_normalized_case_and_punctuation(self):
        from identity_resolver import _search_query_variants

        queries = _search_query_variants(
            "Primary Title",
            ["Alias A", "alias a", "Alias-A", "Alias B"],
        )

        # Normalization collapses case and punctuation variants, preserving
        # the first spelling while leaving room for a genuinely distinct alias.
        self.assertEqual(queries.count("Alias A"), 1)
        self.assertNotIn("alias a", queries)
        self.assertNotIn("Alias-A", queries)
        self.assertIn("Alias B", queries)


    def test_empty_normalized_aliases_do_not_consume_query_budget(self):
        from identity_resolver import _search_query_variants

        queries = _search_query_variants(
            "Main & Title (Subtext) - OVA",
            ["!!!", "Alias A", "Alias B", "Alias C", "Alias D"],
        )

        self.assertNotIn("!!!", queries)
        self.assertIn("Alias D", queries)
        self.assertEqual(len(queries), 7)


if __name__ == "__main__":
    unittest.main()
