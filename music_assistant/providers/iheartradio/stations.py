"""
Artist radio batches for the iHeartRadio provider.

iHeartRadio serves an artist radio station in batches of a few tracks whose audio urls are
short-lived, so a batch is retained only long enough to play and re-resolve its tracks.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any

from .constants import BATCH_URL_TTL, MAX_ACTIVE_STATIONS, MAX_RETAINED_BATCHES


@dataclass
class ArtistRadioBatch:
    """One batch of tracks whose audio urls are live together."""

    station_id: str
    fetched_at: float
    # the raw batch items keyed by track id; the audio url only ever leaves here as a
    # StreamDetails path
    items: dict[str, dict[str, Any]]

    def find(self, track_id: str) -> dict[str, Any] | None:
        """Return the raw item of a track, if this batch holds it."""
        return self.items.get(track_id)

    def urls_expired(self, now: float) -> bool:
        """Return whether this batch has outlived the life of its audio urls."""
        return (now - self.fetched_at) > BATCH_URL_TTL

    def seconds_left(self, now: float) -> int:
        """Return how long the audio urls of this batch are still served for, at least one second."""
        return max(1, int(BATCH_URL_TTL - (now - self.fetched_at)))


@dataclass
class ArtistRadioStation:
    """The batches fetched for one artist radio station."""

    artist_id: str
    # the id iHeartRadio gave the station; the same artist always gets the same one
    station_id: str
    batches: deque[ArtistRadioBatch] = field(
        default_factory=lambda: deque(maxlen=MAX_RETAINED_BATCHES)
    )
    last_accessed: float = 0.0

    def add_batch(self, items: dict[str, dict[str, Any]], now: float) -> ArtistRadioBatch:
        """Retain a freshly fetched batch."""
        batch = ArtistRadioBatch(station_id=self.station_id, fetched_at=now, items=items)
        self.batches.append(batch)
        return batch


class ArtistRadioStore:
    """Hold the batches of the artist radio stations played recently."""

    def __init__(self) -> None:
        """Initialize the store."""
        self._stations: dict[str, ArtistRadioStation] = {}

    def get(self, artist_id: str, now: float) -> ArtistRadioStation | None:
        """
        Return the station of an artist, if it has been played.

        :param artist_id: The seed artist id.
        :param now: Current wall-clock time, to keep the station from being evicted.
        """
        if (station := self._stations.get(artist_id)) is not None:
            station.last_accessed = now
        return station

    def register(self, artist_id: str, station_id: str, now: float) -> ArtistRadioStation:
        """
        Start holding batches for an artist's station, dropping the oldest one past the cap.

        :param artist_id: The seed artist id.
        :param station_id: The station id iHeartRadio gave the artist.
        :param now: Current wall-clock time.
        """
        if len(self._stations) >= MAX_ACTIVE_STATIONS:
            oldest = min(self._stations.values(), key=lambda station: station.last_accessed)
            del self._stations[oldest.artist_id]
        station = ArtistRadioStation(artist_id=artist_id, station_id=station_id)
        station.last_accessed = now
        self._stations[artist_id] = station
        return station

    def find(self, track_id: str) -> tuple[ArtistRadioBatch, dict[str, Any]] | None:
        """
        Return the freshest retained batch holding a track, with the track's raw item.

        Stations overlap, so the same song can sit in several batches; the freshest one is the
        most recent answer iHeartRadio gave for it.

        :param track_id: The iHeartRadio track id.
        """
        holders = [
            (batch, item)
            for station in self._stations.values()
            for batch in station.batches
            if (item := batch.find(track_id)) is not None
        ]
        return max(holders, key=lambda holder: holder[0].fetched_at, default=None)
