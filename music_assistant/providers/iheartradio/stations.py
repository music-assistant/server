"""Artist radio support for the iHeartRadio provider."""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from music_assistant_models.errors import (
    InvalidDataError,
    LoginFailed,
    MediaNotFoundError,
    MusicAssistantError,
)

from .api import json_items
from .constants import (
    BATCH_URL_TTL,
    MAX_ACTIVE_STATIONS,
    MAX_RETAINED_BATCHES,
    PATH_ARTIST_STATION,
    PATH_PLAYBACK_REPORTING,
    PATH_PLAYBACK_STREAMS,
    PLAYED_FROM,
    STATION_OUT_OF_SONGS_CODE,
    STATION_TYPE_RADIO,
)
from .parsers import parse_track, split_artist_radio_item_id

if TYPE_CHECKING:
    from music_assistant_models.media_items import Track

    from .provider import IHeartRadioProvider


@dataclass
class ArtistRadioBatch:
    """One batch of tracks whose audio urls are live together."""

    station_id: str
    fetched_at: float
    # the raw batch items keyed by track id; the audio url only ever leaves here as a
    # StreamDetails path
    items: dict[str, dict[str, Any]]
    # the tracks whose start has been reported, so a re-resolved stream reports it once
    started: set[str] = field(default_factory=set)

    def find(self, track_id: str) -> dict[str, Any] | None:
        """Return the raw item of a track, if this batch holds it."""
        return self.items.get(track_id)

    def mark_started(self, track_id: str) -> bool:
        """Record that a track started playing and return whether this is its first start."""
        if track_id in self.started:
            return False
        self.started.add(track_id)
        return True

    def urls_expired(self, now: float) -> bool:
        """Return whether this batch has outlived the life of its audio urls."""
        return (now - self.fetched_at) > BATCH_URL_TTL

    def seconds_left(self, now: float) -> int:
        """Return for how many more seconds this batch's audio urls are served."""
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


class IHeartRadioStationManager:
    """Play artist radio stations and hold the batches of the ones played recently."""

    def __init__(self, provider: IHeartRadioProvider) -> None:
        """Initialize the station manager."""
        self.provider = provider
        self.logger = provider.logger
        self._stations: dict[str, ArtistRadioStation] = {}

    async def get_dynamic_radio_tracks(self, prov_radio_id: str) -> list[Track]:
        """
        Return a fresh batch of tracks for an artist radio.

        :param prov_radio_id: The artist radio id.
        """
        if (artist_id := split_artist_radio_item_id(prov_radio_id)) is None:
            raise MediaNotFoundError(f"Not an artist radio: {prov_radio_id}")
        now = time.time()
        station = self.get(artist_id, now) or await self._create_station(artist_id, now)
        payload = await self.provider.api.request(
            "POST",
            PATH_PLAYBACK_STREAMS,
            json={
                "contentIds": [],
                "hostName": self.provider.api.host_name,
                "playedFrom": PLAYED_FROM,
                "stationId": station.station_id,
                "stationType": STATION_TYPE_RADIO,
            },
        )
        if not isinstance(payload, dict):
            raise InvalidDataError("iHeartRadio returned no tracks for the station")
        if isinstance(error := payload.get("error"), dict):
            if error.get("code") == STATION_OUT_OF_SONGS_CODE:
                raise MediaNotFoundError("This station has run out of songs to play")
            raise InvalidDataError(f"iHeartRadio refused the station: {error.get('description')}")
        items = {
            str(content["id"]): item
            for item in json_items(payload.get("items"))
            if isinstance(content := item.get("content"), dict)
            and content.get("id")
            and item.get("streamUrl")
        }
        if not items:
            raise MediaNotFoundError("iHeartRadio returned no playable tracks for the station")
        station.add_batch(items, now)
        return [
            track
            for item in items.values()
            if (
                track := parse_track(
                    item["content"], self.provider.instance_id, self.provider.domain
                )
            )
        ]

    async def report_play(self, track_id: str, status: str, seconds_played: int) -> None:
        """
        Report to iHeartRadio how far an artist radio track got.

        Does nothing for a track that is no longer retained.

        :param track_id: The iHeartRadio track id.
        :param status: The playback status to report.
        :param seconds_played: How many seconds of the track were played.
        """
        if (found := self.find(track_id)) is None:
            return
        batch, item = found
        if not (report_payload := item.get("reportPayload")):
            return
        try:
            result = await self.provider.api.request(
                "POST",
                PATH_PLAYBACK_REPORTING,
                json={
                    "modes": [],
                    "offline": False,
                    "playedDate": int(time.time() * 1000),
                    "replay": False,
                    "reportPayload": report_payload,
                    "secondsPlayed": seconds_played,
                    "stationId": batch.station_id,
                    "stationType": STATION_TYPE_RADIO,
                    "status": status,
                },
            )
        except MusicAssistantError as err:
            self.logger.debug("Could not report %s of track %s: %s", status, track_id, err)
            return
        if isinstance(result, dict):
            self.logger.debug(
                "Reported %s of track %s, skips remaining: %s this hour, %s today",
                status,
                track_id,
                result.get("hourSkipsRemaining"),
                result.get("daySkipsRemaining"),
            )

    def find(self, track_id: str) -> tuple[ArtistRadioBatch, dict[str, Any]] | None:
        """
        Return the freshest retained batch holding a track, with the track's raw item.

        :param track_id: The iHeartRadio track id.
        """
        # stations overlap, so the same song can sit in several batches
        holders = [
            (batch, item)
            for station in self._stations.values()
            for batch in station.batches
            if (item := batch.find(track_id)) is not None
        ]
        return max(holders, key=lambda holder: holder[0].fetched_at, default=None)

    def get(self, artist_id: str, now: float) -> ArtistRadioStation | None:
        """
        Return the station of an artist, if it has been played.

        :param artist_id: The seed artist id.
        :param now: Current wall-clock time.
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

    async def _create_station(self, artist_id: str, now: float) -> ArtistRadioStation:
        """
        Register the artist radio station of an artist and start holding its batches.

        :param artist_id: The seed artist id.
        :param now: Current wall-clock time.
        """
        if (auth := self.provider.auth) is None:
            raise LoginFailed("Not signed in to iHeartRadio")
        payload = await self.provider.api.request(
            "POST",
            PATH_ARTIST_STATION.format(profile_id=auth.profile_id, artist_id=artist_id),
            form={"playedFrom": str(PLAYED_FROM)},
        )
        station_id = payload.get("id") if isinstance(payload, dict) else None
        if not station_id:
            raise MediaNotFoundError(f"iHeartRadio has no radio station for artist {artist_id}")
        return self.register(artist_id, str(station_id), now)
