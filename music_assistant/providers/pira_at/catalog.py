"""Validation and normalisation for the public PIRA.AT station API."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse


def _text(value: Any) -> str:
    """Return compact, display-safe text from untrusted API data."""
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _listeners(value: Any) -> int:
    """Coerce an untrusted listener count to a non-negative integer."""
    try:
        return max(0, int(value))
    except TypeError, ValueError:
        return 0


def _stream_url(value: Any) -> str:
    """Return a usable public stream URL, otherwise an empty string."""
    url = _text(value)
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return url


@dataclass(frozen=True, slots=True)
class Station:
    """One normalized, playable PIRA.AT station."""

    item_id: str
    source: str
    name: str
    stream_url: str
    frequency: str
    region: str
    listeners: int
    now_playing: str
    timestamp: int


def parse_catalog(payload: Any) -> dict[str, Station]:
    """Parse the source-grouped PIRA.AT response into stable station IDs."""
    if not isinstance(payload, Mapping):
        raise TypeError("PIRA.AT API response is not a JSON object")

    stations: dict[str, Station] = {}
    for source_name, source_data in payload.items():
        if not isinstance(source_data, Mapping):
            continue
        fallback_source = _text(source_name)
        for fallback_id, raw_station in source_data.items():
            if not isinstance(raw_station, Mapping):
                continue

            source = _text(raw_station.get("bron")) or fallback_source
            station_id = _text(raw_station.get("id")) or _text(fallback_id)
            stream_url = _stream_url(raw_station.get("mp3link"))
            if not source or not station_id or not stream_url:
                continue

            item_id = f"{source}:{station_id}"
            if item_id in stations:
                continue
            stations[item_id] = Station(
                item_id=item_id,
                source=source,
                name=_text(raw_station.get("station")) or "Unknown station",
                stream_url=stream_url,
                frequency=_text(raw_station.get("freq")),
                region=_text(raw_station.get("locatie")) or "Unknown",
                listeners=_listeners(raw_station.get("luisteraars")),
                now_playing=_text(raw_station.get("nowPlaying")),
                timestamp=_listeners(raw_station.get("timestamp")),
            )

    return stations
