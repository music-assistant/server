"""Helpers for the Hermes Agent Radio provider."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from music_assistant.helpers.datetime import utc

from .constants import BASE_URL


@dataclass(frozen=True, slots=True)
class Station:
    """One broadcast station of the lineup."""

    id: str
    name: str
    stream_url: str
    tagline: str = ""
    songs: int | None = None


@dataclass(frozen=True, slots=True)
class NowPlaying:
    """The item a mount says is on the air right now."""

    title: str
    artist: str | None = None
    duration: int | None = None
    elapsed_time: int | None = None


def parse_stations(data: Any) -> dict[str, Station]:
    """
    Return the usable stations of the feed's answer, keyed by their id.

    :param data: The decoded answer, a mapping holding a list under "stations".
    """
    if not isinstance(data, dict):
        return {}
    stations: dict[str, Station] = {}
    for entry in data.get("stations") or []:
        if not isinstance(entry, dict):
            continue
        # an entry without id, name or mount is not a station we can list or play
        station_id = entry.get("id")
        name = entry.get("name")
        mount = entry.get("stream")
        if not station_id or not name or not mount:
            continue
        songs = entry.get("songs")
        stations[str(station_id)] = Station(
            id=str(station_id),
            name=str(name),
            stream_url=f"{BASE_URL}{mount}",
            tagline=str(entry.get("tagline") or ""),
            songs=int(songs) if songs else None,
        )
    return stations


def parse_now_playing(data: Any) -> NowPlaying | None:
    """
    Return what the now-playing feed says is on the air, None when it says nothing.

    :param data: The decoded answer, a mapping holding the current item under "now".
    """
    if not isinstance(data, dict):
        return None
    current = data.get("now")
    if not isinstance(current, dict):
        return None
    title = str(current.get("title") or "").strip()
    if not title:
        return None
    artist = str(current.get("artist") or "").strip() or None
    duration_s = current.get("duration_s")
    return NowPlaying(
        title=title,
        artist=artist,
        duration=int(duration_s) if duration_s else None,
        elapsed_time=_elapsed_time(current.get("started_at"), data.get("station_at")),
    )


def _elapsed_time(started_at: Any, station_at: Any) -> int | None:
    """
    Return the seconds of the current item already played.

    Measured against the feed's own timestamp, so the station's clock decides, not ours.

    :param started_at: When the current item began, an ISO 8601 string.
    :param station_at: When the feed was written, an ISO 8601 string.
    """
    started = _to_datetime(started_at)
    if started is None:
        return None
    now = _to_datetime(station_at) or utc()
    return max(0, int((now - started).total_seconds()))


def _to_datetime(value: Any) -> datetime | None:
    """Return the parsed timestamp, treated as UTC when it carries no offset."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed
