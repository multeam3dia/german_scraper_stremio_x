"""Tests for Simkl AUTH V2 and Stremio player-event scrobbling."""

import os
import tempfile
import unittest
from unittest.mock import patch

import catalog_db
import simkl_tracking


class _FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self.payload = payload or {}

    def json(self):
        return self.payload


class SimklTrackingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.previous = {
            name: os.environ.get(name)
            for name in (
                "CATALOG_DB_PATH",
                "SIMKL_CLIENT_ID",
                "SIMKL_TOKEN_ENCRYPTION_KEY",
            )
        }
        os.environ["CATALOG_DB_PATH"] = os.path.join(self.tmp.name, "catalog.sqlite3")
        os.environ["SIMKL_CLIENT_ID"] = "test-simkl-client"
        os.environ.pop("SIMKL_TOKEN_ENCRYPTION_KEY", None)
        catalog_db.init_db()
        simkl_tracking.init_db()
        simkl_tracking._save_token_response({
            "access_token": "simkl_at_test_access_token",
            "refresh_token": "simkl_rt_test_refresh_token",
            "expires_in": 604800,
            "scope": "media:read media:write",
        })

    def tearDown(self):
        for name, value in self.previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self.tmp.cleanup()

    def _record_title(self, media_type="movie"):
        catalog_db.record_discovery(
            media_type,
            "tt1234567",
            "testprovider",
            "Provider title",
            year=2020,
            metadata={},
        )
        catalog_db.update_title_metadata(
            media_type,
            "tt1234567",
            {"tmdb": {"title": "Canonical title", "year": 2021}},
        )

    def test_movie_payload_uses_canonical_metadata_and_imdb_id(self):
        self._record_title("movie")
        payload = simkl_tracking._build_media_payload("movie", "tt1234567")

        self.assertEqual(payload["movie"]["ids"]["imdb"], "tt1234567")
        self.assertEqual(payload["movie"]["title"], "Canonical title")
        self.assertEqual(payload["movie"]["year"], 2021)

    def test_series_payload_uses_stremio_season_and_episode(self):
        self._record_title("series")
        payload = simkl_tracking._build_media_payload("series", "tt1234567:2:4")

        self.assertEqual(payload["show"]["ids"]["imdb"], "tt1234567")
        self.assertEqual(payload["episode"], {"season": 2, "number": 4})

    def test_start_event_sends_percentage_and_coalesces_seek_events(self):
        self._record_title("movie")
        with patch(
            "simkl_tracking.niquests.post",
            return_value=_FakeResponse(200, {"action": "start"}),
        ) as post:
            first = simkl_tracking.scrobble_player_event(
                "movie", "tt1234567", "start", 30_000, 60_000
            )
            second = simkl_tracking.scrobble_player_event(
                "movie", "tt1234567", "start", 35_000, 60_000
            )

        self.assertEqual(first["status"], "sent")
        self.assertEqual(first["progress"], 50.0)
        self.assertEqual(second, {"status": "ignored", "reason": "duplicate_state"})
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.kwargs["json"]["progress"], 50.0)
        self.assertTrue(post.call_args.args[0].endswith("/scrobble/start"))

    def test_pause_and_stop_are_forwarded_as_distinct_actions(self):
        self._record_title("series")
        with patch(
            "simkl_tracking.niquests.post",
            return_value=_FakeResponse(200, {"action": "pause"}),
        ) as post:
            paused = simkl_tracking.scrobble_player_event(
                "series", "tt1234567:1:2", "pause", 45_000, 60_000
            )
            stopped = simkl_tracking.scrobble_player_event(
                "series", "tt1234567:1:2", "stop", 51_000, 60_000
            )

        self.assertEqual(paused["status"], "sent")
        self.assertEqual(stopped["status"], "sent")
        self.assertEqual(post.call_count, 2)
        self.assertTrue(post.call_args_list[0].args[0].endswith("/scrobble/pause"))
        self.assertTrue(post.call_args_list[1].args[0].endswith("/scrobble/stop"))
        self.assertEqual(post.call_args_list[1].kwargs["json"]["episode"], {
            "season": 1,
            "number": 2,
        })
        self.assertEqual(post.call_args_list[1].kwargs["json"]["progress"], 85.0)

    def test_pause_or_stop_without_duration_is_not_scrobbled(self):
        result = simkl_tracking.scrobble_player_event(
            "movie", "tt1234567", "stop", 50_000, 0
        )
        self.assertEqual(result, {"status": "ignored", "reason": "duration_unknown"})


if __name__ == "__main__":
    unittest.main()
