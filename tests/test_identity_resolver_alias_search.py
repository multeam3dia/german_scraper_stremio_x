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


if __name__ == "__main__":
    unittest.main()
