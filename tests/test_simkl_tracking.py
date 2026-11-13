"""Tests for Simkl tracking using the client-neutral playback event contract."""

import os
import tempfile
import unittest
from unittest.mock import patch

import catalog_db
import simkl_tracking
from playback_events import PlaybackEvent


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

    def _event(
        self,
        media_type="movie",
        action="start",
        progress=0.0,
        source="stremio",
        season=None,
        episode=None,
    ):
        return PlaybackEvent(
            source=source,
            media_type=media_type,
            ids={"imdb": "tt1234567"},
            action=action,
            progress=progress,
            season=season,
            episode=episode,
        )

    def test_movie_payload_uses_canonical_metadata_and_multiple_ids(self):
        self._record_title("movie")
        event = PlaybackEvent(
            source="gmca",
            media_type="movie",
            ids={"imdb": "tt1234567", "tmdb": "98765"},
            action="start",
            progress=0.0,
        )
        payload = simkl_tracking._build_media_payload(event)

        self.assertEqual(payload["movie"]["ids"]["imdb"], "tt1234567")
        self.assertEqual(payload["movie"]["ids"]["tmdb"], "98765")
        self.assertEqual(payload["movie"]["title"], "Canonical title")
        self.assertEqual(payload["movie"]["year"], 2021)

    def test_series_payload_is_independent_of_client_id_format(self):
        self._record_title("series")
        event = self._event(
            media_type="series",
            source="gmca",
            season=2,
            episode=4,
        )
        payload = simkl_tracking._build_media_payload(event)

        self.assertEqual(payload["show"]["ids"]["imdb"], "tt1234567")
        self.assertEqual(payload["episode"], {"season": 2, "number": 4})

    def test_start_event_sends_percentage_and_coalesces_cross_client_duplicates(self):
        self._record_title("movie")
        first_event = self._event(progress=50.0, source="stremio")
        second_event = self._event(progress=58.0, source="gmca")
        with patch(
            "simkl_tracking.niquests.post",
            return_value=_FakeResponse(201, {"action": "start"}),
        ) as post:
            first = simkl_tracking.scrobble_event(first_event)
            second = simkl_tracking.scrobble_event(second_event)

        self.assertEqual(first["status"], "sent")
        self.assertEqual(first["http_status"], 201)
        self.assertEqual(first["progress"], 50.0)
        self.assertEqual(second, {"status": "ignored", "reason": "duplicate_state"})
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.kwargs["json"]["progress"], 50.0)
        self.assertEqual(post.call_args.kwargs["params"]["app-name"], "german-scraper-x")
        self.assertEqual(post.call_args.kwargs["headers"]["User-Agent"], "german-scraper-x/1.4.0")
        self.assertTrue(post.call_args.args[0].endswith("/scrobble/start"))

    def test_pause_and_stop_are_forwarded_as_distinct_actions(self):
        self._record_title("series")
        paused_event = self._event(
            media_type="series", action="pause", progress=75.0,
            source="gmca", season=1, episode=2,
        )
        stopped_event = self._event(
            media_type="series", action="stop", progress=85.0,
            source="gmca", season=1, episode=2,
        )
        with patch(
            "simkl_tracking.niquests.post",
            side_effect=[
                _FakeResponse(201, {"action": "pause"}),
                _FakeResponse(201, {"action": "scrobble"}),
            ],
        ) as post:
            paused = simkl_tracking.scrobble_event(paused_event)
            stopped = simkl_tracking.scrobble_event(stopped_event)

        self.assertEqual(paused["status"], "sent")
        self.assertEqual(stopped["status"], "sent")
        self.assertEqual(paused["action"], "pause")
        self.assertEqual(stopped["action"], "scrobble")
        self.assertEqual(stopped["http_status"], 201)
        self.assertEqual(post.call_count, 2)
        self.assertTrue(post.call_args_list[0].args[0].endswith("/scrobble/pause"))
        self.assertTrue(post.call_args_list[1].args[0].endswith("/scrobble/stop"))
        self.assertEqual(post.call_args_list[1].kwargs["json"]["episode"], {
            "season": 1,
            "number": 2,
        })
        self.assertEqual(post.call_args_list[1].kwargs["json"]["progress"], 85.0)

    def test_stop_below_watched_threshold_returns_pause_action(self):
        event = self._event(action="stop", progress=15.9)
        with patch(
            "simkl_tracking.niquests.post",
            return_value=_FakeResponse(201, {"action": "pause", "progress": 15.9}),
        ):
            result = simkl_tracking.scrobble_event(event)

        self.assertEqual(result["status"], "sent")
        self.assertEqual(result["http_status"], 201)
        self.assertEqual(result["action"], "pause")
        self.assertEqual(result["progress"], 15.9)

    def test_pause_or_stop_without_progress_is_not_scrobbled(self):
        result = simkl_tracking.scrobble_event(
            self._event(action="stop", progress=None)
        )
        self.assertEqual(result, {"status": "ignored", "reason": "duration_unknown"})


if __name__ == "__main__":
    unittest.main()
