"""
Now-playing metadata functionality for BBC Sounds streams.

Handles checking what is meant to be playing at a given point in time on the various
playback types (live stream, live rewind stream, playback content).
"""

import asyncio
import copy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from auntie_sounds import SoundsException
from auntie_sounds.history import Track
from auntie_sounds.models import PlayableItem, Segment
from music_assistant_models.streamdetails import StreamDetails, StreamMetadata

import music_assistant.helpers.datetime as dt
from music_assistant.controllers.streams.audio import STREAMDETAILS_HLS_START_SECONDS_KEY
from music_assistant.controllers.streams.constants import HLS_RADIO_LIVE_START_SECONDS
from music_assistant.providers.bbc_sounds.constants import Constants
from music_assistant.providers.bbc_sounds.live_rewind import LIVE_EDGE_TOLERANCE, LiveProgrammeId
from music_assistant.providers.bbc_sounds.naming import (
    TitleParts,
    programme_title,
)

if TYPE_CHECKING:
    from auntie_sounds import SoundsClient

    from music_assistant.mass import MusicAssistant
    from music_assistant.providers.bbc_sounds.provider import BBCSoundsProvider

# A miss is cached too, so a track with no artwork anywhere is only looked up once a day
SEGMENT_IMAGE_EXPIRATION = 60 * 60 * 24

# The HLS live edge plus the player's buffer, measured at about 7 seconds behind broadcast
LIVE_EDGE_LATENCY = 7


@dataclass(slots=True)
class NowPlaying:
    """What one stream shows: its fallback between tracks, and the track shown now."""

    fallback: StreamMetadata | None
    album: str | None = None
    segment_id: str | None = None
    generation: int = 0


def programme_metadata(
    item: PlayableItem, station: str | None, parts: TitleParts = TitleParts.STATION
) -> StreamMetadata:
    """Return what a stream shows for its programme when no track is playing."""
    return StreamMetadata(
        title=programme_title(item, parts), album=station, image_url=item.image_url
    )


def _segments_cache_key(vpid: str) -> str:
    return f"programme_segments_v3_{vpid}"


def _find_segment(segments: list[Segment], elapsed_time: int) -> Segment | None:
    return next(
        (
            s
            for s in segments
            if s.offset
            and s.offset.end is not None
            and s.offset.start <= elapsed_time < s.offset.end
        ),
        None,
    )


def _bbc_image(segment: Segment) -> str | None:
    """Return the segment's own image, unless it is the BBC's blank placeholder."""
    url = segment.image_url
    return None if url is None or Constants.BLANK_IMAGE_NAME in url else url


def _track_metadata(segment: Segment, album: str | None, image_url: str | None) -> StreamMetadata:
    """Return the metadata shown for a track."""
    return StreamMetadata(
        title=segment.titles.secondary or "",
        artist=segment.titles.primary or "",
        album=album,
        image_url=image_url,
    )


class NowPlayingManager:
    """Finds the track at the listener's position and shows it, or the stream's fallback."""

    def __init__(self, provider: BBCSoundsProvider) -> None:
        """Initialise the now playing manager."""
        self.provider: BBCSoundsProvider = provider
        self.client: SoundsClient = provider.client
        self.mass: MusicAssistant = provider.mass
        self.domain = provider.domain
        self.logger = provider.logger

    async def update_station(
        self, state: NowPlaying, stream_details: StreamDetails, _elapsed_time: int
    ) -> None:
        """Show the track the listener hears on a live station, which is behind live."""
        station_id = stream_details.item_id

        # MA records how far behind live the stream started, which is unset before playback
        data = stream_details.data or {}
        delay = data.get(STREAMDETAILS_HLS_START_SECONDS_KEY)
        if delay is None:
            delay = HLS_RADIO_LIVE_START_SECONDS

        segment = await self._station_track(
            station_id, dt.utc(), timedelta(seconds=delay + LIVE_EDGE_LATENCY)
        )

        if segment is None:
            if programme := await self.client.schedules.on_air(station_id):
                state.fallback = programme_metadata(programme, state.album)

        self._show_now_playing(stream_details, state, segment)

    async def update_live_programme(
        self,
        state: NowPlaying,
        programme_id: LiveProgrammeId,
        programme: PlayableItem,
        stream_details: StreamDetails,
        elapsed_time: int,
    ) -> None:
        """
        Show the track at the listener's point in a programme played from live rewind.

        :param state: The stream's now-playing state.
        :param programme_id: The programme being played.
        :param programme: The programme, from the station's schedule.
        :param stream_details: Stream details of the programme.
        :param elapsed_time: Seconds played since the start of the programme.
        """
        at = datetime.fromtimestamp(programme_id.start + elapsed_time, UTC)
        segment = await self._station_track(programme_id.station_id, at)

        if segment is None:
            at_live_edge = dt.utc_timestamp() - at.timestamp() <= LIVE_EDGE_TOLERANCE
            parts = TitleParts.LIVE_EDGE if at_live_edge else TitleParts.LIVE_REWIND
            state.fallback = programme_metadata(programme, state.album, parts)
        self._show_now_playing(stream_details, state, segment)

    async def update_on_demand(
        self,
        state: NowPlaying,
        segments: list[Segment],
        stream_details: StreamDetails,
        elapsed_time: int,
    ) -> None:
        """Show the track at elapsed_time in an on-demand programme."""
        self._show_now_playing(stream_details, state, _find_segment(segments, elapsed_time))

    async def programme_segments(self, vpid: str) -> list[Segment]:
        """Get an on-demand programme's tracks from the cache or the API."""
        key = _segments_cache_key(vpid)
        cached = await self.mass.cache.get(key, provider=self.domain)
        if isinstance(cached, list):
            return [Segment.from_dict(item) for item in cached]
        segments = await self.client.content.get_show_segments(vpid)
        # The track list can be published after the programme, so an empty one isn't cached
        if segments:
            await self.mass.cache.set(
                key, [segment.to_dict() for segment in segments], provider=self.domain
            )
        return segments

    async def _station_track(
        self, station_id: str, at: datetime, delay: timedelta = timedelta(0)
    ) -> Segment | None:
        """Return the track a station had on air at a time, polling as the BBC asks."""
        history = await self.client.schedules.refresh_history(station_id)
        result = history.lookup(at, delay)
        return result.segment if isinstance(result, Track) else None

    def _show_now_playing(
        self, stream_details: StreamDetails, state: NowPlaying, segment: Segment | None
    ) -> None:
        """
        Show a track's now playing details in the player.

        It starts a background task to look up its track image, updating if/when it
        arrives.
        """
        if segment is None:
            # Leaving a track invalidates any artwork lookup still running for it
            if state.segment_id is not None:
                state.generation += 1
                self.logger.debug(
                    "%s: track %s ended, showing fallback",
                    stream_details.item_id,
                    state.segment_id,
                )

            state.segment_id = None
            self._set_metadata(stream_details, state.fallback)
            return

        # This runs each time, but a track that is already showing is all good
        if segment.id == state.segment_id:
            return

        state.generation += 1
        generation = state.generation
        state.segment_id = segment.id

        own_image = _bbc_image(segment)
        self.logger.debug(
            "%s: now playing %s - %s (%s), BBC track image: %s",
            stream_details.item_id,
            segment.titles.primary,
            segment.titles.secondary,
            segment.id,
            own_image is not None,
        )

        # Show the track straight away, track image arrives when it's ready
        self._set_metadata(stream_details, _track_metadata(segment, state.album, own_image))

        if own_image is None:
            self.mass.create_task(
                self._show_segment_image(stream_details, state, segment, generation),
                task_name="bbc_sounds_segment_image",
            )

    async def _show_segment_image(
        self, stream_details: StreamDetails, state: NowPlaying, segment: Segment, generation: int
    ) -> None:
        """
        Look up a track's artwork and show it, unless the listener has moved on.

        state.generation counts track changes. A lookup notes the generation it started
        in, and its result is only applied if the generation is unchanged, so slow
        artwork for an earlier track never overwrites what is showing now.
        """
        image = await self._segment_image(stream_details, segment)

        # The listener may have moved on while the lookup was running
        if state.generation != generation:
            self.logger.debug(
                "Ignoring found track image for %s, track changed during lookup", segment.id
            )
            return

        if image is None:
            self.logger.debug("No track image found for %s", segment.id)
            return

        self._set_metadata(stream_details, _track_metadata(segment, state.album, image))

    async def _segment_image(self, stream_details: StreamDetails, segment: Segment) -> str | None:
        """Look up artwork for a track the BBC has none for, from Spotify then MA."""
        key = f"segment_image_{segment.id}"
        cached = await self.mass.cache.get(key, provider=self.domain)
        if isinstance(cached, str):
            return cached or None

        image: str | None = None

        try:
            async with asyncio.timeout(Constants.ARTWORK_TIMEOUT):
                image = await self._spotify_image(segment) or await self._ma_image(
                    stream_details, segment
                )
        except TimeoutError:
            self.logger.debug("Timed out looking up artwork for %s", segment.id)
            return None
        await self.mass.cache.set(
            key, image or "", provider=self.domain, expiration=SEGMENT_IMAGE_EXPIRATION
        )
        return image

    async def _spotify_image(self, segment: Segment) -> str | None:
        """Return the track's Spotify artwork, without changing the library's segment."""
        lookup = copy.copy(segment)
        try:
            await self.client.content.ensure_segment_image(lookup)
        except SoundsException as err:
            self.logger.debug("Error fetching artwork from Spotify: %s", err)
        if lookup.image_url != segment.image_url:
            self.logger.debug("Found a track image from Spotify.")
        return _bbc_image(lookup)

    async def _ma_image(self, stream_details: StreamDetails, segment: Segment) -> str | None:
        """Return the artwork MA's radio artwork lookup finds for the track."""
        # MA fills in the metadata it is given, so give it a copy
        lookup = copy.copy(stream_details)
        lookup.stream_metadata = _track_metadata(segment, None, None)
        await self.mass.metadata.update_radio_stream_artwork(lookup)
        return lookup.stream_metadata.image_url if lookup.stream_metadata else None

    def _set_metadata(self, stream_details: StreamDetails, metadata: StreamMetadata | None) -> None:
        """Show new metadata at once, instead of at the queue's next update."""
        if metadata == stream_details.stream_metadata:
            return

        self.logger.debug("Now playing on %s: %s", stream_details.item_id, metadata)
        stream_details.stream_metadata = metadata
        if stream_details.queue_id:
            self.mass.player_queues.signal_update(stream_details.queue_id)
