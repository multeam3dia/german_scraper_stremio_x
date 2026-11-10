"""Tests for client adapters and the normalized playback event contract."""

import unittest

from playback_events import (
    PlaybackEventError,
    playback_event_from_payload,
    stremio_player_event,
)


class PlaybackEventTests(unittest.TestCase):
    def test_stremio_movie_event_becomes_normalized_event(self):
        event = stremio_player_event(
            "movie", "tt1234567", "start", 30_000, 60_000
        )
        self.assertEqual(event.source, "stremio")
        self.assertEqual(event.media_type, "movie")
        self.assertEqual(event.ids, {"imdb": "tt1234567"})
        self.assertEqual(event.progress, 50.0)
        self.assertEqual(event.media_key, "movie:imdb:tt1234567")

    def test_stremio_series_event_preserves_episode_identity(self):
        event = stremio_player_event(
            "series", "tt1234567:2:4", "pause", 45_000, 60_000
        )
        self.assertEqual(event.season, 2)
        self.assertEqual(event.episode, 4)
        self.assertEqual(event.progress, 75.0)
        self.assertEqual(event.media_key, "series:imdb:tt1234567:s2:e4")

    def test_stremio_pause_without_duration_is_invalid(self):
        with self.assertRaises(PlaybackEventError) as caught:
            stremio_player_event("movie", "tt1234567", "pause", 10_000, 0)
        self.assertEqual(caught.exception.reason, "duration_unknown")

    def test_generic_gmca_payload_accepts_multiple_identifiers(self):
        event = playback_event_from_payload({
            "source": "gmca",
            "action": "stop",
            "media_type": "series",
            "ids": {"imdb": "tt1234567", "tmdb": 9876},
            "season": 1,
            "episode": 3,
            "progress": 82.5,
            "title": "Example series",
            "year": 2024,
        })
        self.assertEqual(event.source, "gmca")
        self.assertEqual(event.ids, {"imdb": "tt1234567", "tmdb": "9876"})
        self.assertEqual(event.media_key, "series:imdb:tt1234567:s1:e3")
        self.assertEqual(event.progress, 82.5)

    def test_generic_payload_requires_a_stable_media_identity(self):
        with self.assertRaises(PlaybackEventError) as caught:
            playback_event_from_payload({
                "source": "gmca",
                "action": "start",
                "media_type": "movie",
                "ids": {},
                "progress": 0,
            })
        self.assertEqual(caught.exception.reason, "missing_media_identity")


if __name__ == "__main__":
    unittest.main()
