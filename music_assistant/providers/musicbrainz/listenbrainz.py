"""Client for the most listened recordings of an artist, as ListenBrainz counts them."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from mashumaro import DataClassDictMixin
from music_assistant_models.errors import RateLimited

from music_assistant.constants import MASS_USER_AGENT
from music_assistant.helpers.compare import compare_track_title
from music_assistant.helpers.json import json_loads
from music_assistant.helpers.throttle_retry import (
    ThrottlerManager,
    parse_retry_after,
    throttle_with_retries,
)
from music_assistant.providers.coverartarchive import CAA_BASE_URL

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

LB_BASE_URL: Final = "https://api.listenbrainz.org/1"
# the number of an artist's most listened recordings that is kept, one per title
TOP_TRACKS_LIMIT: Final = 50
TOP_RECORDINGS_EXPIRATION: Final = 7 * 24 * 3600
# how long it is kept that ListenBrainz has no list for an artist; an artist it does not
# know is answered with the same 401 that asks for a token
NO_LIST_EXPIRATION: Final = 24 * 3600


@dataclass(frozen=True, kw_only=True)
class TopRecording(DataClassDictMixin):
    """One of an artist's most listened recordings."""

    recording_mbid: str
    name: str
    # in seconds
    duration: int | None
    # the MusicBrainz ids of the credited artists, in credit order
    artist_mbids: tuple[str, ...]
    release_mbid: str | None
    release_name: str | None
    cover_url: str | None


@dataclass(frozen=True, kw_only=True)
class TopRecordings:
    """
    An artist's most listened recordings, most listened first.

    :param recordings: The recordings; empty when ListenBrainz has no list for the artist.
    :param fetched: Whether ListenBrainz was asked for them just now, rather than the list
        kept from an earlier answer being returned.
    """

    recordings: list[TopRecording]
    fetched: bool


class ListenBrainzClient:
    """Thin HTTP client for the popularity data of ListenBrainz."""

    domain = "listenbrainz"
    # a rate limit is not retried by the call it hits, the cooldown holds back the next one
    throttler = ThrottlerManager(rate_limit=1, period=1, retry_attempts=1)

    def __init__(self, mass: MusicAssistant) -> None:
        """Initialize the client."""
        self.mass = mass
        self.logger = logging.getLogger(__name__)

    async def get_top_recordings(self, artist_mbid: str) -> TopRecordings:
        """
        Return the most listened recordings of an artist, one per title.

        At most TOP_TRACKS_LIMIT recordings are returned. A list is kept for a week, the
        outcome that ListenBrainz has none for the artist for a day.

        :param artist_mbid: The MusicBrainz id of the artist.
        :raises RetriesExhausted: ListenBrainz rate limited the request.
        :raises aiohttp.ClientError: ListenBrainz could not be asked, or answered with an error.
        """
        key = f"top_recordings.{artist_mbid}"
        if (kept := await self.mass.cache.get(key, provider=self.domain)) is not None:
            return TopRecordings(
                recordings=[TopRecording.from_dict(raw) for raw in kept], fetched=False
            )
        answer = await self._get_top_recordings(artist_mbid)
        recordings = [] if answer is None else _top_recordings(answer)
        await self.mass.cache.set(
            key,
            [recording.to_dict() for recording in recordings],
            expiration=NO_LIST_EXPIRATION if answer is None else TOP_RECORDINGS_EXPIRATION,
            provider=self.domain,
        )
        return TopRecordings(recordings=recordings, fetched=True)

    @throttle_with_retries
    async def _get_top_recordings(self, artist_mbid: str) -> list[dict[str, Any]] | None:
        """Fetch the ranking of an artist's recordings, None when ListenBrainz asks for a token."""
        # the endpoint answers any query parameter with a 401, so the whole list is fetched
        url = f"{LB_BASE_URL}/popularity/top-recordings-for-artist/{artist_mbid}"
        headers = {"User-Agent": MASS_USER_AGENT}
        async with self.mass.http_session.get(url, headers=headers) as response:
            if response.status == 429:
                backoff_time = parse_retry_after(response.headers.get("X-RateLimit-Reset-In"))
                raise RateLimited("ListenBrainz rate limit", backoff_time=backoff_time)
            if response.status == 401:
                return None
            response.raise_for_status()
            answer: list[dict[str, Any]] = await response.json(loads=json_loads)
            return answer


def _top_recordings(answer: list[dict[str, Any]]) -> list[TopRecording]:
    """Return the first recording of each title in a ranking, up to TOP_TRACKS_LIMIT."""
    recordings: list[TopRecording] = []
    for entry in answer:
        if any(compare_track_title(kept.name, entry["recording_name"]) for kept in recordings):
            continue
        recordings.append(_top_recording(entry))
        if len(recordings) == TOP_TRACKS_LIMIT:
            break
    return recordings


def _top_recording(entry: dict[str, Any]) -> TopRecording:
    """Return one entry of a ListenBrainz ranking, its cover as a Cover Art Archive address."""
    cover_url = None
    if (release_id := entry.get("caa_release_mbid")) and (image_id := entry.get("caa_id")):
        cover_url = f"{CAA_BASE_URL}/release/{release_id}/{image_id}-500.jpg"
    length = entry.get("length")
    return TopRecording(
        recording_mbid=entry["recording_mbid"],
        name=entry["recording_name"],
        duration=round(length / 1000) if length else None,
        artist_mbids=tuple(entry["artist_mbids"]),
        release_mbid=entry.get("release_mbid"),
        release_name=entry.get("release_name"),
        cover_url=cover_url,
    )
