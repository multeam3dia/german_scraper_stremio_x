"""Integration tests for independent Simkl and MDBList dispatch."""

import unittest
from unittest.mock import patch

import app
from playback_events import PlaybackEvent


class TrackingDispatchTests(unittest.TestCase):
    def setUp(self):
        self.event = PlaybackEvent(
            source="stremio",
            media_type="movie",
            ids={"imdb": "tt1234567"},
            action="start",
            progress=0.0,
        )

    @patch("app.get_mdblist_status", return_value={"connected": True, "status": "connected"})
    @patch("app.get_simkl_status", return_value={"connected": True, "status": "connected"})
    @patch("app.mdblist_scrobble_event", return_value={"status": "sent", "http_status": 200})
    @patch("app.scrobble_event", side_effect=app.SimklError("Simkl unavailable"))
    def test_mdblist_still_receives_event_when_simkl_fails(
        self, simkl_scrobble, mdblist_scrobble, simkl_status, mdblist_status
    ):
        result = app._dispatch_tracking_event(self.event)

        simkl_scrobble.assert_called_once_with(self.event)
        mdblist_scrobble.assert_called_once_with(self.event)
        self.assertEqual(result["status"], "sent")
        self.assertEqual(result["providers"]["simkl"]["status"], "error")
        self.assertEqual(result["providers"]["mdblist"]["status"], "sent")

    @patch("app.get_mdblist_status", return_value={"connected": True, "status": "connected"})
    @patch("app.get_simkl_status", return_value={"connected": True, "status": "connected"})
    @patch("app.mdblist_scrobble_event", side_effect=RuntimeError("MDBList unavailable"))
    @patch("app.scrobble_event", return_value={"status": "sent", "http_status": 201})
    def test_simkl_still_receives_event_when_mdblist_fails(
        self, simkl_scrobble, mdblist_scrobble, simkl_status, mdblist_status
    ):
        result = app._dispatch_tracking_event(self.event)

        simkl_scrobble.assert_called_once_with(self.event)
        mdblist_scrobble.assert_called_once_with(self.event)
        self.assertEqual(result["status"], "sent")
        self.assertEqual(result["providers"]["simkl"]["status"], "sent")
        self.assertEqual(result["providers"]["mdblist"]["status"], "error")

    @patch("app.get_mdblist_status", return_value={"connected": False, "status": "not_connected"})
    @patch("app.get_simkl_status", return_value={"connected": False, "status": "not_connected"})
    @patch("app.mdblist_scrobble_event")
    @patch("app.scrobble_event")
    def test_disconnected_trackers_are_not_called(
        self, simkl_scrobble, mdblist_scrobble, simkl_status, mdblist_status
    ):
        result = app._dispatch_tracking_event(self.event)

        simkl_scrobble.assert_not_called()
        mdblist_scrobble.assert_not_called()
        self.assertEqual(result["status"], "not_connected")
        self.assertEqual(result["providers"]["simkl"]["status"], "not_connected")
        self.assertEqual(result["providers"]["mdblist"]["status"], "not_connected")


if __name__ == "__main__":
    unittest.main()
