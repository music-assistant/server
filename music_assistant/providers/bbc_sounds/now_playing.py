"""Now-playing code for the BBC Sounds provider."""

import asyncio
from typing import TYPE_CHECKING

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

if TYPE_CHECKING:
    from auntie_sounds import SoundsClient

    from music_assistant.mass import MusicAssistant
    from music_assistant.providers.bbc_sounds.provider import BBCSoundsProvider


def station_programme_display(
    station: LiveStation, detailed: bool = False
) -> StreamMetadata | None:
    """Calculate what to show for a radio programme's metadata."""
    if station and station.titles and station.titles.primary:
        title = f"{station.titles.secondary} • {station.titles.primary}"
        if detailed and station.synopses and station.synopses.short:
            title = f"{title}: {station.synopses.short}"
        return StreamMetadata(title=title, artist=None, image_url=station.image_url)
    return None


def _find_segment(segments: list[Segment], elapsed_time: int) -> Segment | None:
    segment = None
    if segments:
        segment = next(
            (
                s
                for s in segments
                if isinstance(s, Segment)
                and s.offset is not None
                and s.offset.end is not None
                and s.offset.start <= elapsed_time < s.offset.end
            ),
            None,
        )
    return segment


def _segment_to_metadata(now_playing: Segment) -> StreamMetadata | None:
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
            stream_details.stream_metadata = _segment_to_metadata(now_playing)
        else:
            self.logger.debug(
                "No song playing on %s, displaying current programme info", station_id
            )
            if metadata := await self._station_current_programme_metadata(station_id):
                stream_details.stream_metadata = metadata

    async def _get_programme_segments(self, vpid: str) -> list[Segment]:
        """Get on demand segments from cache or API."""
        cache_key = f"programme_segments_v3_{vpid}"
        cached = await self.mass.cache.get(provider=self.domain, key=cache_key)
        if isinstance(cached, list):
            self.logger.debug("Cache hit for programme segments for %s", vpid)
            return [Segment.from_dict(item) for item in cached]
        self.logger.debug("No cache for programme segments for %s", vpid)
        segments = await self.client.content.get_show_segments(vpid)
        # The track list can be published after the programme, so an empty one isn't cached
        if segments:
            await self.mass.cache.set(
                provider=self.domain,
                key=cache_key,
                data=[segment.to_dict() for segment in segments],
            )
        return segments

    async def _update_on_demand_stream_metadata(
        self, stream_details: StreamDetails, elapsed_time: int
    ) -> None:
        """
        Get the currently playing segment (song) for on-demand episodes.

        Called by the callback function in StreamDetails.
        """
        self.logger.log(VERBOSE_LOG_LEVEL, "Updating on-demand stream metadata")

        if not stream_details or not stream_details.stream_metadata:
            return

        vpid = stream_details.data.get("vpid")
        if not vpid:
            self.logger.warning("No VPID found")
            return

        # Store the segments here rather than the cache so we don't have to worry about
        # caching a show's segments before they are available
        segments = stream_details.data.get("segments")
        # A show with no segments is valid, so only check for None
        if segments is None:
            segments = await self._get_programme_segments(vpid)
            stream_details.data["segments"] = segments
        if not segments:
            return

        segment = _find_segment(segments, elapsed_time)

        if segment:
            metadata = _segment_to_metadata(segment)
            if metadata:
                stream_details.stream_metadata = metadata
            # As of June 2026, the API currently doesn't return images from this endpoint
            # We fill in missing images with the Spotify API, if any are still blank
            # then use the MA helpers here
            if not stream_details.stream_metadata.image_url:
                try:
                    async with asyncio.timeout(Constants.ARTWORK_TIMEOUT):
                        await self.mass.metadata.update_radio_stream_artwork(stream_details)
                except TimeoutError:
                    self.logger.debug("Timeout while waiting for artwork")
            return

        # No song playing, so show episode metadata
        if title := stream_details.data.get("episode_title"):
            stream_details.stream_title = title
        if metadata := stream_details.data.get("episode_metadata"):
            stream_details.stream_metadata = metadata

    @use_cache(expiration=Constants.DYNAMIC_EXPIRATION, base_class=StreamMetadata)
    async def _station_current_programme_metadata(self, station_id: str) -> StreamMetadata | None:
        """Reduce downstream API calls via simple wrapper for caching."""
        # TODO: we can actually cache this for the remainder of the programme if we
        # calculate that value
        self.logger.debug("Fetching fresh programme result from API")
        station = await self.client.schedules.current_programme(station_id)
        if not station:
            return None
        self.logger.debug("%s(%s)", type(station), station.id)
        self.logger.log(VERBOSE_LOG_LEVEL, station)
        if station_metadata := station_programme_display(station, detailed=True):
            return station_metadata
        return None
