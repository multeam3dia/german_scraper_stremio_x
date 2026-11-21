"""Tests for MDBList connection gating and playback scrobbling."""

import os
import tempfile
import unittest
from unittest.mock import patch

import mdblist_tracking
from playback_events import PlaybackEvent


class MDBListTrackingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.previous = {
            name: os.environ.get(name)
            for name in ("CATALOG_DB_PATH", "MDBLIST_API_KEY")
        }
        os.environ["CATALOG_DB_PATH"] = os.path.join(self.tmp.name, "catalog.sqlite3")
        os.environ["MDBLIST_API_KEY"] = "test-mdblist-key"
        mdblist_tracking._STATUS_CACHE.update(
            api_key=None, checked_at=0, result=None
        )
        mdblist_tracking.init_db()

    def tearDown(self):
        for name, value in self.previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        mdblist_tracking._STATUS_CACHE.update(
            api_key=None, checked_at=0, result=None
        )
        self.tmp.cleanup()

    def _event(self):
        return PlaybackEvent(
            source="stremio",
            media_type="movie",
            ids={"imdb": "tt1234567"},
            action="start",
            progress=0.0,
        )

    def test_status_validation_is_cached_for_same_api_key(self):
        with patch(
            "mdblist_tracking._api_request",
            return_value=(200, {"username": "test"}, {}),
        ) as request:
            first = mdblist_tracking.get_mdblist_status()
            second = mdblist_tracking.get_mdblist_status()

        self.assertTrue(first["connected"])
        self.assertTrue(second["connected"])
        self.assertEqual(request.call_count, 1)

    def test_unreachable_status_is_cached_briefly(self):
        with patch(
            "mdblist_tracking._api_request",
            side_effect=mdblist_tracking.MDBListTrackingError("offline"),
        ) as request:
            first = mdblist_tracking.get_mdblist_status()
            second = mdblist_tracking.get_mdblist_status()

        self.assertEqual(first["reason"], "api_unreachable")
        self.assertEqual(second["reason"], "api_unreachable")
        self.assertEqual(request.call_count, 1)

    def test_scrobble_is_skipped_when_api_key_is_missing(self):
        os.environ.pop("MDBLIST_API_KEY", None)
        with patch("mdblist_tracking._api_request") as request:
            result = mdblist_tracking.scrobble_event(self._event())

        self.assertEqual(result["status"], "not_configured")
        request.assert_not_called()

    def test_unauthorized_scrobble_marks_connection_disconnected(self):
        with patch(
            "mdblist_tracking._api_request",
            side_effect=[
                (200, {}, {}),
                (401, {"error": "invalid_api_key"}, {}),
            ],
        ) as request:
            result = mdblist_tracking.scrobble_event(self._event())
            status = mdblist_tracking.get_mdblist_status()

        self.assertEqual(result["status"], "not_connected")
        self.assertFalse(status["connected"])
        self.assertEqual(status["status"], "disconnected")
        self.assertEqual(request.call_count, 2)


if __name__ == "__main__":
    unittest.main()
