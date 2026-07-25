"""Radio Paradise Music Provider for Music Assistant."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

import aiohttp
from music_assistant_models.enums import MediaType, StreamType
from music_assistant_models.errors import MediaNotFoundError, UnplayableMediaError
from music_assistant_models.media_items import (
    AudioFormat,
    BrowseFolder,
    ItemMapping,
    MediaItemType,
    Radio,
    SearchResults,
)
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.models.music_provider import MusicProvider

from . import parsers
from .constants import (
    API_TIMEOUT,
    NOWPLAYING_API_URL,
    PLAY_API_URL,
    RADIO_PARADISE_CHANNELS,
    STREAM_METADATA_UPDATE_INTERVAL,
)
from .helpers import find_current_song, get_current_block_position, get_next_song

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry


class RadioParadiseProvider(MusicProvider):
    """Radio Paradise Music Provider for Music Assistant."""

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return Config entries to setup this provider."""
        return (
            # we (currently) don't have any config entries to set up
        )

    @property
    def is_streaming_provider(self) -> bool:
        """Return True if the provider is a streaming provider."""
        return True

    async def get_radio(self, prov_radio_id: str) -> Radio:
        """Get full radio details by id."""
        if prov_radio_id not in RADIO_PARADISE_CHANNELS:
            raise MediaNotFoundError("Station not found")
        return self._parse_radio(prov_radio_id)

    async def search(
        self,
        search_query: str,
        media_types: list[MediaType],
        limit: int = 5,
    ) -> SearchResults:
        """Perform search on Radio Paradise channels."""
        results = SearchResults()
        if MediaType.RADIO not in media_types:
            return results
        search_query_lower = search_query.lower().strip()
        if not search_query_lower:
            return results
        radios: list[Radio] = []
        for channel_id, channel_info in RADIO_PARADISE_CHANNELS.items():
            if search_query_lower in channel_info["name"].lower():
                radios.append(self._parse_radio(channel_id))
                if len(radios) >= limit:
                    break
        results.radio = radios
        return results

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Get streamdetails for a radio station."""
        if media_type != MediaType.RADIO:
            raise UnplayableMediaError(f"Unsupported media type: {media_type}")
        if item_id not in RADIO_PARADISE_CHANNELS:
            raise MediaNotFoundError(f"Unknown radio channel: {item_id}")

        channel_info = RADIO_PARADISE_CHANNELS[item_id]
        stream_url = channel_info["stream_url"]
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
            stream_metadata_update_interval=STREAM_METADATA_UPDATE_INTERVAL,
        )

        # Set initial metadata if available so the first frame the listener sees
        # is the live track rather than an empty banner.
        metadata = await self._get_channel_metadata(item_id)
        if metadata and metadata.get("current"):
            stream_details.stream_metadata = parsers.build_stream_metadata(
                metadata["current"], metadata
            )

        return stream_details

    async def browse(self, path: str) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """Browse this provider's items."""
        return [self._parse_radio(channel_id) for channel_id in RADIO_PARADISE_CHANNELS]

    def _parse_radio(self, channel_id: str) -> Radio:
        """Create a Radio object from cached channel information."""
        return parsers.parse_radio(channel_id, self.instance_id, self.domain)

    async def _fetch_json(self, url: str, channel_id: str) -> dict[str, Any] | None:
        """
        Fetch JSON from a Radio Paradise endpoint, returning None on any failure.

        :param url: Fully-qualified API URL to GET.
        :param channel_id: Channel id, used for log context.
        """
        try:
            async with self.mass.http_session.get(url, timeout=API_TIMEOUT) as response:
                if response.status != 200:
                    self.logger.debug(
                        "Radio Paradise API returned status %s for channel %s",
                        response.status,
                        channel_id,
                    )
                    return None
                data: dict[str, Any] = await response.json()
                return data or None
        except aiohttp.ClientError as exc:
            self.logger.debug(
                "Radio Paradise API request failed for channel %s: %s", channel_id, exc
            )
            return None
        except (KeyError, ValueError, TypeError) as exc:
            self.logger.debug(
                "Error parsing Radio Paradise API response for channel %s: %s", channel_id, exc
            )
            return None

    async def _get_channel_metadata(self, channel_id: str) -> dict[str, Any] | None:
        """
        Get current track and upcoming tracks from Radio Paradise's API.

        Tries the enriched play API first, falls back to simple now_playing API if it fails.

        :param channel_id: Radio Paradise channel ID (0-5).
        """
        if channel_id not in RADIO_PARADISE_CHANNELS:
            return None

        result = await self._get_play_api_metadata(channel_id)
        if result:
            return result

        self.logger.debug("Falling back to now_playing API for channel %s", channel_id)
        return await self._get_nowplaying_api_metadata(channel_id)

    async def _get_play_api_metadata(self, channel_id: str) -> dict[str, Any] | None:
        """
        Get metadata from the enriched play API with upcoming track info.

        :param channel_id: Radio Paradise channel ID (0-5).
        """
        data = await self._fetch_json(f"{PLAY_API_URL}{channel_id}", channel_id)
        if not data or "song" not in data:
            return None

        songs = data.get("song", {})
        current_time_ms = get_current_block_position(data)
        current_song = find_current_song(songs, current_time_ms)
        if not current_song:
            self.logger.debug("No current song found for channel %s", channel_id)
            return None

        return {
            "current": current_song,
            "next": get_next_song(songs, current_song),
            "block_data": data,
        }

    async def _get_nowplaying_api_metadata(self, channel_id: str) -> dict[str, Any] | None:
        """
        Get metadata from the simple now_playing API (fallback).

        :param channel_id: Radio Paradise channel ID (0-5).
        """
        data = await self._fetch_json(f"{NOWPLAYING_API_URL}{channel_id}", channel_id)
        if not data:
            return None
        # now_playing returns flat song data; no next song or block data is available.
        return {"current": data, "next": None, "block_data": None}

    async def _update_stream_metadata(
        self, stream_details: StreamDetails, elapsed_time: int
    ) -> None:
        """
        Update stream metadata callback called by player queue controller.

        Fetches current track info from Radio Paradise's API and updates
        StreamDetails with track metadata. Alternates between showing the artist
        and upcoming track info every interval.

        :param stream_details: StreamDetails object to update with metadata.
        :param elapsed_time: Elapsed playback time in seconds (unused for Radio Paradise).
        """
        item_id = stream_details.item_id
        if stream_details.data is None:
            stream_details.data = {}

        metadata = await self._get_channel_metadata(item_id)
        if not metadata or not metadata.get("current"):
            return

        current_song = metadata["current"]
        current_event = current_song.get("event", "")

        # On track change, restart the artist/upcoming alternation from "artist".
        if stream_details.data.get("last_event") != current_event:
            stream_details.data["last_event"] = current_event
            stream_details.data["show_upcoming"] = False

        show_upcoming = stream_details.data.get("show_upcoming", False)
        stream_metadata = parsers.build_stream_metadata(
            current_song, metadata, show_upcoming=show_upcoming
        )

        self.logger.debug(
            "Updating stream metadata for %s: %s - %s",
            item_id,
            stream_metadata.artist,
            stream_metadata.title,
        )
        stream_details.stream_metadata = stream_metadata
        stream_details.data["show_upcoming"] = not show_upcoming
