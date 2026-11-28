"""Source-neutral playback event contract and client adapters.

Client adapters translate their native player callbacks into PlaybackEvent.
Tracking providers consume PlaybackEvent and never parse client-specific IDs.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Mapping, Optional

IMDB_ID_RE = re.compile(r"^tt\d{7,10}$")
SOURCE_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
SUPPORTED_ID_TYPES = {
    "imdb", "tmdb", "tvdb", "trakt", "simkl",
    "anidb", "mal", "anilist", "kitsu",
}
ID_PRIORITY = ("imdb", "tmdb", "tvdb", "trakt", "simkl", "anidb", "mal", "anilist", "kitsu")
SUPPORTED_MEDIA_TYPES = {"movie", "series"}
SUPPORTED_ACTIONS = {"start", "pause", "stop"}


class PlaybackEventError(ValueError):
    """Invalid client event with a stable, safe-to-return reason."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class PlaybackEvent:
    """Normalized event shared by Stremio, GMCA and future playback clients."""

    source: str
    media_type: str
    ids: Mapping[str, str]
    action: str
    progress: Optional[float]
    season: Optional[int] = None
    episode: Optional[int] = None
    title: Optional[str] = None
    year: Optional[int] = None

    @property
    def media_key(self) -> str:
        """Stable cross-client key used to coalesce duplicate player states."""
        id_type = next(
            (candidate for candidate in ID_PRIORITY if self.ids.get(candidate)),
            None,
        )
        if id_type is None:
            id_type = sorted(self.ids)[0]
        key = f"{self.media_type}:{id_type}:{self.ids[id_type]}"
        if self.media_type == "series":
            key += f":s{self.season}:e{self.episode}"
        return key


def progress_from_ms(current_time_ms: int, duration_ms: int) -> Optional[float]:
    """Convert a player's millisecond position/duration into a percentage."""
    try:
        current = max(0, int(current_time_ms))
        duration = int(duration_ms)
    except (TypeError, ValueError):
        return None
    if duration <= 0:
        return None
    return round(min(100.0, max(0.0, current / duration * 100.0)), 2)


def _normalize_ids(raw_ids: Any) -> dict[str, str]:
    if not isinstance(raw_ids, Mapping):
        return {}
    ids: dict[str, str] = {}
    for raw_key, raw_value in raw_ids.items():
        key = str(raw_key or "").strip().lower()
        if key not in SUPPORTED_ID_TYPES or raw_value is None:
            continue
        value = str(raw_value).strip()
        if not value or len(value) > 128:
            continue
        if key == "imdb" and not IMDB_ID_RE.fullmatch(value):
            continue
        ids[key] = value
    return ids


def playback_event_from_payload(payload: Any) -> PlaybackEvent:
    """Validate the stable JSON contract used by non-Stremio clients such as GMCA."""
    if not isinstance(payload, Mapping):
        raise PlaybackEventError("invalid_payload")

    source = str(payload.get("source") or "").strip().lower()
    if not SOURCE_RE.fullmatch(source):
        raise PlaybackEventError("invalid_source")

    action = str(payload.get("action") or "").strip().lower()
    if action not in SUPPORTED_ACTIONS:
        raise PlaybackEventError("unsupported_action")

    media_type = str(payload.get("media_type") or "").strip().lower()
    if media_type not in SUPPORTED_MEDIA_TYPES:
        raise PlaybackEventError("unsupported_media_type")

    ids = _normalize_ids(payload.get("ids"))
    if not ids:
        raise PlaybackEventError("missing_media_identity")

    season = episode = None
    if media_type == "series":
        try:
            season = int(payload.get("season"))
            episode = int(payload.get("episode"))
        except (TypeError, ValueError):
            raise PlaybackEventError("invalid_episode_identity")
        if season < 0 or episode < 1:
            raise PlaybackEventError("invalid_episode_identity")

    progress = payload.get("progress")
    if progress is not None:
        try:
            progress = float(progress)
        except (TypeError, ValueError):
            raise PlaybackEventError("invalid_progress")
        if not math.isfinite(progress) or not 0.0 <= progress <= 100.0:
            raise PlaybackEventError("invalid_progress")

    title = str(payload.get("title") or "").strip()[:300] or None
    year = payload.get("year")
    if year is not None:
        try:
            year = int(str(year)[:4])
        except (TypeError, ValueError):
            year = None
        if year is not None and not 1800 <= year <= 2200:
            year = None

    return PlaybackEvent(
        source=source,
        media_type=media_type,
        ids=ids,
        action=action,
        progress=progress,
        season=season,
        episode=episode,
        title=title,
        year=year,
    )


def stremio_player_event(
    media_type: str,
    video_id: str,
    action: str,
    current_time_ms: int,
    duration_ms: int,
) -> PlaybackEvent:
    """Adapt Stremio's native player route into the shared event contract."""
    media_type = str(media_type or "").strip().lower()
    action = str(action or "").strip().lower()
    if action not in SUPPORTED_ACTIONS:
        raise PlaybackEventError("unsupported_action")
    if media_type not in SUPPORTED_MEDIA_TYPES:
        raise PlaybackEventError("unsupported_media_type")

    parts = str(video_id or "").split(":")
    imdb_id = parts[0].strip()
    if not IMDB_ID_RE.fullmatch(imdb_id):
        raise PlaybackEventError("unsupported_media_id")

    season = episode = None
    if media_type == "movie":
        if len(parts) != 1:
            raise PlaybackEventError("unsupported_media_id")
    else:
        if len(parts) != 3:
            raise PlaybackEventError("invalid_episode_identity")
        try:
            season, episode = int(parts[1]), int(parts[2])
        except (TypeError, ValueError):
            raise PlaybackEventError("invalid_episode_identity")
        if season < 0 or episode < 1:
            raise PlaybackEventError("invalid_episode_identity")

    progress = progress_from_ms(current_time_ms, duration_ms)
    if progress is None:
        if action == "start":
            progress = 0.0
        else:
            raise PlaybackEventError("duration_unknown")

    return PlaybackEvent(
        source="stremio",
        media_type=media_type,
        ids={"imdb": imdb_id},
        action=action,
        progress=progress,
        season=season,
        episode=episode,
    )
