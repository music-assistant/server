"""Mother Earth Radio Music Provider for Music Assistant."""

from __future__ import annotations

from collections.abc import Sequence
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
    SearchResults,
)
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.models.music_provider import MusicProvider

from . import parsers
from .constants import MER_CHANNELS, NOWPLAYING_API_URL


class MotherEarthRadioProvider(MusicProvider):
    """Mother Earth Radio Music Provider for Music Assistant."""

    @property
    def is_streaming_provider(self) -> bool:
        """Return True if the provider is a streaming provider."""
        return True

    async def get_radio(self, prov_radio_id: str) -> Radio:
        """Get full radio details by id.

        :param prov_radio_id: Channel key, e.g. "motherearth_jazz".
        """
        if prov_radio_id not in MER_CHANNELS:
            raise MediaNotFoundError("Station not found")
        return self._parse_radio(prov_radio_id)

    async def search(
        self,
        search_query: str,
        media_types: list[MediaType],
        limit: int = 5,
    ) -> SearchResults:
        """Perform search on Mother Earth Radio channels.

        :param search_query: User-entered search string.
        :param media_types: List of media types to include.
        :param limit: Maximum number of results.
        """
        results = SearchResults()
        if MediaType.RADIO not in media_types:
            return results
        search_query_lower = search_query.lower().strip()
        if not search_query_lower:
            return results
        radios: list[Radio] = []
        for channel_id, channel_info in MER_CHANNELS.items():
            channel_name = channel_info.get("name", "").lower()
            channel_desc = channel_info.get("description", "").lower()
            if search_query_lower in channel_name or search_query_lower in channel_desc:
                radios.append(self._parse_radio(channel_id))
                if len(radios) >= limit:
                    break
        results.radio = radios
        return results

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Get streamdetails for a radio station.

        :param item_id: Channel key, e.g. "motherearth_klassik".
        :param media_type: Must be MediaType.RADIO.
        """
        if media_type != MediaType.RADIO:
            raise UnplayableMediaError(f"Unsupported media type: {media_type}")
        if item_id not in MER_CHANNELS:
            raise MediaNotFoundError(f"Unknown radio channel: {item_id}")

        channel_info = MER_CHANNELS[item_id]
        stream_url = channel_info.get("stream_url")
        if not stream_url:
            raise UnplayableMediaError(f"No stream URL found for channel {item_id}")

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
            stream_metadata_update_interval=15,
        )

        # Set initial metadata if available
        nowplaying = await self._get_nowplaying(item_id)
        if nowplaying and nowplaying.get("now_playing"):
            stream_details.stream_metadata = parsers.build_stream_metadata(
                nowplaying["now_playing"],
                nowplaying.get("playing_next"),
            )

        return stream_details

    async def browse(self, path: str) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
        """Browse this provider's items."""
        return [self._parse_radio(channel_id) for channel_id in MER_CHANNELS]

    def _parse_radio(self, channel_id: str) -> Radio:
        """Create a Radio object from channel configuration."""
        return parsers.parse_radio(channel_id, self.instance_id, self.domain)

    async def _get_nowplaying(self, channel_id: str) -> dict[str, Any] | None:
        """Get current now-playing data from the AzuraCast API.

        :param channel_id: Channel key, e.g. "motherearth_jazz".
        """
        if channel_id not in MER_CHANNELS:
            return None

        shortcode = MER_CHANNELS[channel_id]["shortcode"]
        api_url = f"{NOWPLAYING_API_URL}/{shortcode}"

        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with self.mass.http_session.get(api_url, timeout=timeout) as response:
                if response.status != 200:
                    self.logger.debug(
                        f"AzuraCast API returned status {response.status} for {channel_id}"
                    )
                    return None
                return await response.json()

        except aiohttp.ClientError as exc:
            self.logger.debug(f"AzuraCast API request failed for {channel_id}: {exc}")
            return None
        except (KeyError, ValueError, TypeError) as exc:
            self.logger.debug(f"Error parsing AzuraCast response for {channel_id}: {exc}")
            return None

    async def _update_stream_metadata(
        self, stream_details: StreamDetails, elapsed_time: int
    ) -> None:
        """Update stream metadata callback called by player queue controller.

        Fetches current track info from AzuraCast and updates StreamDetails.
        Alternates between showing the artist and upcoming track info.

        :param stream_details: StreamDetails object to update with metadata.
        :param elapsed_time: Elapsed playback time in seconds (unused for live radio).
        """
        item_id = stream_details.item_id

        # Initialize data dict if needed
        if stream_details.data is None:
            stream_details.data = {}

        try:
            nowplaying = await self._get_nowplaying(item_id)
            if nowplaying and nowplaying.get("now_playing"):
                np = nowplaying["now_playing"]
                current_song_id = np.get("song", {}).get("id", "")

                # Track changed — reset to show artist first
                if stream_details.data.get("last_song_id") != current_song_id:
                    stream_details.data["last_song_id"] = current_song_id
                    stream_details.data["show_upcoming"] = False

                # Toggle between artist and upcoming info
                show_upcoming = stream_details.data.get("show_upcoming", False)

                stream_metadata = parsers.build_stream_metadata(
                    np,
                    nowplaying.get("playing_next"),
                    show_upcoming=show_upcoming,
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
