"""Now-playing code for the BBC Sounds provider."""

import asyncio
import copy
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from auntie_sounds import SoundsException
from auntie_sounds.history import Track
from auntie_sounds.models import (
    LiveStation,
    Segment,
)
from music_assistant_models.streamdetails import StreamDetails, StreamMetadata

from music_assistant.constants import (
    VERBOSE_LOG_LEVEL,
)
from music_assistant.controllers.cache import use_cache
from music_assistant.providers.bbc_sounds.constants import Constants
from music_assistant.providers.bbc_sounds.live_rewind import LiveProgrammeId

if TYPE_CHECKING:
    from auntie_sounds import SoundsClient

    from music_assistant.mass import MusicAssistant
    from music_assistant.providers.bbc_sounds.provider import BBCSoundsProvider


def live_station_to_metadata(station: LiveStation, detailed: bool = False) -> StreamMetadata | None:
    """Calculate what to show for a radio programme's metadata."""
    if station and station.titles and station.titles.primary:
        # Filter out any None values before
        title = " • ".join(filter(None, (station.titles.secondary, station.titles.primary)))
        if detailed and station.synopses and station.synopses.short:
            title = f"{title}: {station.synopses.short}"
        return StreamMetadata(title=title, artist=None, image_url=station.image_url)
    return None


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


def _segment_to_metadata(now_playing: Segment) -> StreamMetadata:
    """Convert a now-playing segment to StreamMetadata."""
    title = now_playing.titles.secondary or ""
    artist = now_playing.titles.primary or ""
    image_url = now_playing.image_url
    if image_url and Constants.BLANK_IMAGE_NAME in image_url:
        image_url = None
    return StreamMetadata(title=title, artist=artist, image_url=image_url)


class NowPlayingManager:
    """Manages the now-playing/segment/live stream metadata."""

    def __init__(self, provider: BBCSoundsProvider) -> None:
        """Initialise the now playing manager."""
        self.provider: BBCSoundsProvider = provider
        self.client: SoundsClient = provider.client
        self.mass: MusicAssistant = self.provider.mass
        self.domain = self.provider.domain
        self.logger = self.provider.logger

    async def update_live_stream_metadata(
        self, stream_details: StreamDetails, elapsed_time: int
    ) -> None:
        """Get the currently playing song for live radio streams."""
        self.logger.log(VERBOSE_LOG_LEVEL, "Updating live stream metadata")
        if not stream_details or not stream_details.stream_metadata:
            return

        station_id = stream_details.item_id
        if not station_id:
            return

        now_playing = await self.client.schedules.currently_playing_song(station_id)
        if now_playing:
            self.logger.log(VERBOSE_LOG_LEVEL, "Now playing for %s: %s", station_id, now_playing)
            self._set_metadata(stream_details, _segment_to_metadata(now_playing))
        else:
            self.logger.log(
                VERBOSE_LOG_LEVEL,
                "No song playing on %s, displaying current programme info",
                station_id,
            )
            if metadata := await self._current_programme_metadata(station_id):
                stream_details.stream_metadata = metadata

    async def update_on_demand_stream_metadata(
        self, stream_details: StreamDetails, elapsed_time: int
    ) -> None:
        """Show the segment playing at elapsed_time, or the episode between segments."""
        self.logger.log(VERBOSE_LOG_LEVEL, "Updating on-demand stream metadata")

        data = stream_details.data
        if (segments := data.get("segments")) is None:
            self.logger.debug("No segments found on stream_details.data, loading them...")
            segments = data["segments"] = await self._load_programme_segments(data["vpid"])

        segment = _find_segment(segments, elapsed_time)
        segment_id = segment.id if segment else None

        # We're either showing the correct segment, or we're correctly showing the episode metadata
        if segment_id == data.get("segment_id"):
            self.logger.log(
                VERBOSE_LOG_LEVEL, "No need to update metadata, segment_id=%s", segment_id
            )
            return
        data["segment_id"] = segment_id

        if segment is None:
            self.logger.debug("Between segments, showing episode metadata")
            self._set_metadata(stream_details, data["episode_metadata"])
            return

        self._set_metadata(stream_details, _segment_to_metadata(segment))
        image_before = segment.image_url
        await self._fill_segment_image(stream_details, segment)
        if segment.image_url == image_before:
            return
        await self._save_programme_segments(data["vpid"], segments)
        # Check we don't try and set an old segment's metadata if we took too long
        if data["segment_id"] == segment.id:
            self._set_metadata(stream_details, _segment_to_metadata(segment))

    async def update_live_programme_metadata(
        self, stream_details: StreamDetails, elapsed_time: int
    ) -> None:
        """Show the track on air at the listener's point in a live programme."""
        programme_id = LiveProgrammeId.parse(stream_details.item_id)
        if programme_id is None:
            return
        history = await self.client.schedules.refresh_history(programme_id.station_id)
        self.logger.debug("Live programme history: %s", history._entries)
        at = datetime.fromtimestamp(programme_id.start + elapsed_time, UTC)
        match history.lookup(at):
            case Track(segment=segment):
                self.logger.debug("Track found in history: %s", str(history.lookup(at)))
                stream_details.stream_metadata = _segment_to_metadata(segment)
            case _:
                # None falls back to the queue item, which describes the programme
                self.logger.debug("No track found, or no song playing")
                stream_details.stream_metadata = None

    @use_cache(expiration=Constants.DYNAMIC_EXPIRATION, base_class=StreamMetadata)
    async def _current_programme_metadata(self, station_id: str) -> StreamMetadata | None:
        """Reduce downstream API calls via simple wrapper for caching."""
        # TODO: we can actually cache this for the remainder of the programme if we
        # calculate that value
        self.logger.debug("Fetching fresh programme result from API")
        station = await self.client.schedules.current_programme(station_id)
        if not station:
            return None
        self.logger.debug("%s(%s)", type(station), station.id)
        self.logger.log(VERBOSE_LOG_LEVEL, station)
        return live_station_to_metadata(station, detailed=True)

    async def _load_programme_segments(self, vpid: str) -> list[Segment]:
        """Get on demand segments from cache or API."""
        cached = await self.mass.cache.get(provider=self.domain, key=_segments_cache_key(vpid))
        if isinstance(cached, list):
            self.logger.debug("Cache hit for programme segments for %s", vpid)
            return [Segment.from_dict(item) for item in cached]
        self.logger.debug("No cache for programme segments for %s", vpid)
        segments = await self.client.content.get_show_segments(vpid)
        # The track list can be published after the programme, so an empty one isn't cached
        if segments:
            await self._save_programme_segments(vpid, segments)
        return segments

    async def _save_programme_segments(self, vpid: str, segments: list[Segment]) -> None:
        """To cache segments initially, but also after any segment is updated."""
        self.logger.debug("Saving segments to cache")
        await self.mass.cache.set(
            provider=self.domain,
            key=_segments_cache_key(vpid),
            data=[segment.to_dict() for segment in segments],
        )

    async def _fill_segment_image(self, stream_details: StreamDetails, segment: Segment) -> None:
        """Fill in a segment's missing image from Spotify, then from MA's radio artwork lookup."""
        if _segment_to_metadata(segment).image_url:
            self.logger.debug("Segment already has an image, skipping.")
            return
        try:
            async with asyncio.timeout(Constants.ARTWORK_TIMEOUT):
                try:
                    await self.client.content.ensure_segment_image(segment)
                except SoundsException as err:
                    self.logger.debug("Error when fetching segment image: %s", err)

                metadata = _segment_to_metadata(segment)

                if metadata.image_url:
                    self.logger.debug("Segment image fetched from Spotify successfully.")
                    return

                self.logger.debug("No Spotify image, falling back to MA")

                # Use a copy of the StreamDetails to update so we avoid races
                lookup = copy.copy(stream_details)
                lookup.stream_metadata = metadata
                await self.mass.metadata.update_radio_stream_artwork(lookup)
        except TimeoutError:
            self.logger.debug("Timeout while waiting for artwork")
            return

        # MA updates the metadata directly, so grab it back out for the segment
        if (found := lookup.stream_metadata) and found.image_url:
            self.logger.debug("MA found an image, using that.")
            segment.image_url = found.image_url
        else:
            self.logger.debug("No images found from any sources for the current song.")

    def _set_metadata(self, stream_details: StreamDetails, metadata: StreamMetadata) -> None:
        """Force a metadata refresh instead of waiting."""
        if metadata == stream_details.stream_metadata:
            return
        stream_details.stream_metadata = metadata
        if stream_details.queue_id:
            self.mass.player_queues.signal_update(stream_details.queue_id)
