"""Radio Paradise Music Provider for Music Assistant."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncGenerator, Sequence
from typing import Any

import aiohttp
from music_assistant_models.enums import MediaType, StreamType
from music_assistant_models.errors import MediaNotFoundError, UnplayableMediaError
from music_assistant_models.media_items import (
    AudioFormat,
    BrowseFolder,
    ItemMapping,
    MediaItemType,
    Radio,
)
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.controllers.cache import use_cache
from music_assistant.models.music_provider import MusicProvider

from . import parsers
from .constants import RADIO_PARADISE_CHANNELS
from .helpers import build_stream_url, find_current_song, get_current_block_position, get_next_song


class RadioParadiseProvider(MusicProvider):
    """Radio Paradise Music Provider for Music Assistant."""

    @property
    def is_streaming_provider(self) -> bool:
        """Return True if the provider is a streaming provider."""
        return True

    async def get_library_radios(self) -> AsyncGenerator[Radio, None]:
        """Retrieve library/subscribed radio stations from the provider."""
        for channel_id in RADIO_PARADISE_CHANNELS:
            yield self._parse_radio(channel_id)

    @use_cache(3600 * 3)  # Cache for 3 hours
    async def get_radio(self, prov_radio_id: str) -> Radio:
        """Get full radio details by id."""
        if prov_radio_id not in RADIO_PARADISE_CHANNELS:
            raise MediaNotFoundError("Station not found")

        return self._parse_radio(prov_radio_id)

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Get streamdetails for a radio station."""
        if media_type != MediaType.RADIO:
            raise UnplayableMediaError(f"Unsupported media type: {media_type}")
        if item_id not in RADIO_PARADISE_CHANNELS:
            raise MediaNotFoundError(f"Unknown radio channel: {item_id}")

        stream_url = build_stream_url(item_id)
        if not stream_url:
            raise UnplayableMediaError(f"No stream URL found for channel {item_id}")

        # Get content type from channel configuration
        channel_info = RADIO_PARADISE_CHANNELS[item_id]
        content_type = channel_info["content_type"]

        stream_details = StreamDetails(
            item_id=item_id,
            provider=self.instance_id,
            audio_format=AudioFormat(
                content_type=content_type,
                channels=2,
            ),
            media_type=MediaType.RADIO,
            stream_type=StreamType.HTTP,
            path=stream_url,
            allow_seek=False,
            can_seek=False,
            duration=0,
        )

        # Set initial metadata if available
        metadata = await self._get_channel_metadata(item_id)
        if metadata and metadata.get("current"):
            current_song = metadata["current"]
            stream_details.stream_metadata = parsers.build_stream_metadata(current_song, metadata)

        # Store the monitoring task in streamdetails.data for cleanup in on_streamed
        monitor_task = self.mass.create_task(self._monitor_stream_metadata(stream_details))
        stream_details.data = {"monitor_task": monitor_task}

        return stream_details

    async def on_streamed(self, streamdetails: StreamDetails) -> None:
        """Handle callback when given streamdetails completed streaming."""
        self.logger.debug(
            f"Radio Paradise channel {streamdetails.item_id} streamed for "
            f"{streamdetails.seconds_streamed} seconds"
        )

        # Cancel and clean up the monitoring task
        if "monitor_task" in streamdetails.data:
            monitor_task = streamdetails.data["monitor_task"]
            if not monitor_task.done():
                monitor_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await monitor_task
            del streamdetails.data["monitor_task"]

    async def browse(self, path: str) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """Browse this provider's items."""
        return [self._parse_radio(channel_id) for channel_id in RADIO_PARADISE_CHANNELS]

    def _parse_radio(self, channel_id: str) -> Radio:
        """Create a Radio object from cached channel information."""
        return parsers.parse_radio(channel_id, self.instance_id, self.domain)

    async def _get_channel_metadata(self, channel_id: str) -> dict[str, Any] | None:
        """Get current track and upcoming tracks from Radio Paradise's block API.

        Args:
            channel_id: Radio Paradise channel ID (0-5)

        Returns:
            Dict with current song, next song, and block data, or None if API fails
        """
        if channel_id not in RADIO_PARADISE_CHANNELS:
            return None

        try:
            # Use block API for much richer data
            api_url = (
                f"https://api.radioparadise.com/api/get_block?bitrate=4&info=true&chan={channel_id}"
            )
            timeout = aiohttp.ClientTimeout(total=10)

            async with self.mass.http_session.get(api_url, timeout=timeout) as response:
                if response.status != 200:
                    self.logger.debug(f"Block API call failed with status {response.status}")
                    return None

                data = await response.json()

                # Find currently playing song based on elapsed time
                current_time_ms = get_current_block_position(data)
                current_song = find_current_song(data.get("song", {}), current_time_ms)

                if not current_song:
                    self.logger.debug(f"No current song found for channel {channel_id}")
                    return None

                # Get next song
                next_song = get_next_song(data.get("song", {}), current_song)

                return {"current": current_song, "next": next_song, "block_data": data}

        except aiohttp.ClientError as exc:
            self.logger.debug(f"Failed to get block metadata for channel {channel_id}: {exc}")
            return None
        except Exception as exc:
            self.logger.debug(
                f"Unexpected error getting block metadata for channel {channel_id}: {exc}"
            )
            return None

    async def _monitor_stream_metadata(self, stream_details: StreamDetails) -> None:
        """Monitor and update stream metadata in real-time during playback.

        Fetches current track info from Radio Paradise's API every 10 seconds
        and updates StreamDetails with track metadata and upcoming songs.

        Args:
            stream_details: StreamDetails object to update with metadata
        """
        last_track_event = ""
        item_id = stream_details.item_id

        try:
            while True:
                metadata = await self._get_channel_metadata(item_id)
                if metadata and metadata.get("current"):
                    current_song = metadata["current"]
                    current_event = current_song.get("event", "")

                    if current_event != last_track_event:
                        # Create StreamMetadata object with full track info
                        stream_metadata = parsers.build_stream_metadata(current_song, metadata)

                        self.logger.debug(
                            f"Updating stream metadata for {item_id}: "
                            f"{stream_metadata.artist} - {stream_metadata.title}"
                        )
                        stream_details.stream_metadata = stream_metadata

                        last_track_event = current_event

                await asyncio.sleep(15)
        except asyncio.CancelledError:
            self.logger.debug(f"Monitor task cancelled for {item_id}")
        except aiohttp.ClientError as exc:
            self.logger.debug(f"Network error while monitoring {item_id}: {exc}")
        except Exception as exc:
            self.logger.warning(f"Unexpected error monitoring {item_id}: {exc}")
