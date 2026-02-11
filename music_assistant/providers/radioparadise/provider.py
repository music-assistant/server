"""Radio Paradise Music Provider for Music Assistant."""

from __future__ import annotations

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
from .constants import NOWPLAYING_API_URL, PLAY_API_URL, RADIO_PARADISE_CHANNELS
from .helpers import find_current_song, get_current_block_position, get_next_song


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

        # Get stream URL from channel configuration
        channel_info = RADIO_PARADISE_CHANNELS[item_id]
        stream_url = channel_info.get("stream_url")
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
            stream_metadata_update_callback=self._update_stream_metadata,
            stream_metadata_update_interval=10,  # Check every 10 seconds
        )

        # Set initial metadata if available
        metadata = await self._get_channel_metadata(item_id)
        if metadata and metadata.get("current"):
            current_song = metadata["current"]
            stream_details.stream_metadata = parsers.build_stream_metadata(current_song, metadata)

        return stream_details

    async def browse(self, path: str) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """Browse this provider's items."""
        return [self._parse_radio(channel_id) for channel_id in RADIO_PARADISE_CHANNELS]

    def _parse_radio(self, channel_id: str) -> Radio:
        """Create a Radio object from cached channel information."""
        return parsers.parse_radio(channel_id, self.instance_id, self.domain)

    async def _get_channel_metadata(self, channel_id: str) -> dict[str, Any] | None:
        """Get current track and upcoming tracks from Radio Paradise's API.

        Tries the enriched play API first, falls back to simple now_playing API if it fails.

        :param channel_id: Radio Paradise channel ID (0-5).
        """
        if channel_id not in RADIO_PARADISE_CHANNELS:
            return None

        # Try enriched play API first
        result = await self._get_play_api_metadata(channel_id)
        if result:
            return result

        # Fallback to simple now_playing API
        self.logger.debug(f"Falling back to now_playing API for channel {channel_id}")
        return await self._get_nowplaying_api_metadata(channel_id)

    async def _get_play_api_metadata(self, channel_id: str) -> dict[str, Any] | None:
        """Get metadata from the enriched play API with upcoming track info.

        :param channel_id: Radio Paradise channel ID (0-5).
        """
        try:
            api_url = f"{PLAY_API_URL}{channel_id}"
            timeout = aiohttp.ClientTimeout(total=10)

            async with self.mass.http_session.get(api_url, timeout=timeout) as response:
                if response.status != 200:
                    self.logger.debug(f"Play API call failed with status {response.status}")
                    return None

                data = await response.json()

                if not data or "song" not in data:
                    self.logger.debug(f"No song data in play API response for channel {channel_id}")
                    return None

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
            self.logger.debug(f"Play API request failed for channel {channel_id}: {exc}")
            return None
        except (KeyError, ValueError, TypeError) as exc:
            self.logger.debug(f"Error parsing play API response for channel {channel_id}: {exc}")
            return None

    async def _get_nowplaying_api_metadata(self, channel_id: str) -> dict[str, Any] | None:
        """Get metadata from the simple now_playing API (fallback).

        :param channel_id: Radio Paradise channel ID (0-5).
        """
        try:
            api_url = f"{NOWPLAYING_API_URL}{channel_id}"
            timeout = aiohttp.ClientTimeout(total=10)

            async with self.mass.http_session.get(api_url, timeout=timeout) as response:
                if response.status != 200:
                    self.logger.debug(f"Now playing API failed with status {response.status}")
                    return None

                data = await response.json()

                if not data:
                    self.logger.debug(f"No data from now_playing API for channel {channel_id}")
                    return None

                # now_playing API returns flat song data, no next song or block data
                return {"current": data, "next": None, "block_data": None}

        except aiohttp.ClientError as exc:
            self.logger.debug(f"Now playing API request failed for channel {channel_id}: {exc}")
            return None
        except (KeyError, ValueError, TypeError) as exc:
            self.logger.debug(f"Error parsing now_playing response for channel {channel_id}: {exc}")
            return None

    async def _update_stream_metadata(
        self, stream_details: StreamDetails, elapsed_time: int
    ) -> None:
        """Update stream metadata callback called by player queue controller.

        Fetches current track info from Radio Paradise's API and updates
        StreamDetails with track metadata. Alternates between showing the artist
        and upcoming track info every 10 seconds.

        :param stream_details: StreamDetails object to update with metadata.
        :param elapsed_time: Elapsed playback time in seconds (unused for Radio Paradise).
        """
        item_id = stream_details.item_id

        # Initialize data dict if needed
        if stream_details.data is None:
            stream_details.data = {}

        try:
            metadata = await self._get_channel_metadata(item_id)
            if metadata and metadata.get("current"):
                current_song = metadata["current"]
                current_event = current_song.get("event", "")

                # Track changed - reset to show artist first
                if stream_details.data.get("last_event") != current_event:
                    stream_details.data["last_event"] = current_event
                    stream_details.data["show_upcoming"] = False

                # Toggle between artist and upcoming info
                show_upcoming = stream_details.data.get("show_upcoming", False)

                # Create StreamMetadata object with full track info
                stream_metadata = parsers.build_stream_metadata(
                    current_song, metadata, show_upcoming=show_upcoming
                )

                self.logger.debug(
                    f"Updating stream metadata for {item_id}: "
                    f"{stream_metadata.artist} - {stream_metadata.title}"
                )
                stream_details.stream_metadata = stream_metadata

                # Toggle for next update
                stream_details.data["show_upcoming"] = not show_upcoming

        except aiohttp.ClientError as exc:
            self.logger.debug(f"Network error updating metadata for {item_id}: {exc}")
