"""Tests for the resumable catalog crawl batch runner."""

import unittest

from providers.crawl_batches import run_drain


def _result(*, status="partial", detail_limit=True, index_complete=True, blocked=False):
    return {
        "status": status,
        "detail_limit_reached": detail_limit,
        "index_complete": index_complete,
        "blocked": blocked,
    }


class CrawlBatchRunnerTests(unittest.TestCase):
    def test_drains_each_provider_sequentially_and_pauses_between_batches(self):
        responses = {
            "aniworld": [
                _result(),
                _result(),
                _result(status="completed", detail_limit=False),
            ],
            "serienstream": [
                _result(),
                _result(status="completed", detail_limit=False),
            ],
        }
        calls = []
        pauses = []
        events = []

        def crawl(provider_id, *, max_details=None):
            calls.append((provider_id, max_details))
            return responses[provider_id].pop(0)

        pause_values = iter([3.5, 4.0, 5.5, 7.0])
        summary = run_drain(
            ("aniworld", "serienstream"),
            crawl,
            max_details=200,
            pause_min_minutes=3,
            pause_max_minutes=8,
            sleep=pauses.append,
            uniform=lambda minimum, maximum: next(pause_values),
            emit=events.append,
        )

        self.assertEqual(
            calls,
            [
                ("aniworld", 200),
                ("aniworld", 200),
                ("aniworld", 200),
                ("serienstream", 200),
                ("serienstream", 200),
            ],
        )
        self.assertEqual(pauses, [210.0, 240.0, 330.0, 420.0])
        self.assertEqual(summary["status"], "completed")
        self.assertEqual(summary["batches"], 5)
        self.assertEqual(summary["completed_providers"], ["aniworld", "serienstream"])
        self.assertEqual(summary["remaining_providers"], [])
        self.assertEqual(summary["exit_code"], 0)
        self.assertEqual(events[-1]["event"], "drain_finished")

    def test_block_stops_immediately_without_pause_or_next_provider(self):
        calls = []
        pauses = []
        events = []

        def crawl(provider_id, *, max_details=None):
            calls.append(provider_id)
            return _result(
                status="partial",
                detail_limit=False,
                index_complete=False,
                blocked=True,
            )

        summary = run_drain(
            ("aniworld", "serienstream"),
            crawl,
            sleep=pauses.append,
            emit=events.append,
        )

        self.assertEqual(calls, ["aniworld"])
        self.assertEqual(pauses, [])
        self.assertEqual(summary["status"], "stopped")
        self.assertEqual(summary["stop_reason"], "blocked:aniworld")
        self.assertEqual(summary["remaining_providers"], ["aniworld", "serienstream"])
        self.assertEqual(summary["exit_code"], 1)

    def test_incomplete_index_stops_instead_of_spinning(self):
        calls = []
        pauses = []

        def crawl(provider_id, *, max_details=None):
            calls.append(provider_id)
            return _result(detail_limit=False, index_complete=False)

        summary = run_drain(
            ("aniworld",),
            crawl,
            sleep=pauses.append,
            emit=lambda event: None,
        )

        self.assertEqual(calls, ["aniworld"])
        self.assertEqual(pauses, [])
        self.assertEqual(summary["stop_reason"], "provider_incomplete:aniworld")
        self.assertEqual(summary["exit_code"], 1)

    def test_max_batches_is_a_clean_resumable_stop(self):
        calls = []
        pauses = []

        def crawl(provider_id, *, max_details=None):
            calls.append(provider_id)
            return _result()

        summary = run_drain(
            ("aniworld",),
            crawl,
            max_batches=2,
            sleep=pauses.append,
            uniform=lambda minimum, maximum: 3,
            emit=lambda event: None,
        )

        self.assertEqual(calls, ["aniworld", "aniworld"])
        self.assertEqual(pauses, [180.0])
        self.assertEqual(summary["status"], "paused")
        self.assertEqual(summary["stop_reason"], "max_batches_reached")
        self.assertEqual(summary["remaining_providers"], ["aniworld"])
        self.assertEqual(summary["exit_code"], 0)

    def test_invalid_pause_range_is_rejected(self):
        with self.assertRaises(ValueError):
            run_drain(
                ("aniworld",),
                lambda provider_id, *, max_details=None: _result(),
                pause_min_minutes=8,
                pause_max_minutes=3,
                emit=lambda event: None,
            )


if __name__ == "__main__":
    unittest.main()
