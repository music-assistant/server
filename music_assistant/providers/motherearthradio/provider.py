"""Mother Earth Radio Music Provider for Music Assistant."""

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
from .constants import MER_BIT_DEPTH, MER_CHANNELS, MER_SAMPLE_RATE, NOWPLAYING_API_URL

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry


class MotherEarthRadioProvider(MusicProvider):
    """Mother Earth Radio Music Provider for Music Assistant."""

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return Config entries to configure this provider."""
        return ()

    @property
    def is_streaming_provider(self) -> bool:
        """Whether this provider streams from an external source."""
        return True

    async def get_radio(self, prov_radio_id: str) -> Radio:
        """
        Get full radio details by id.

        :param prov_radio_id: Channel key, e.g. "motherearth_jazz".
        """
        if prov_radio_id not in MER_CHANNELS:
            raise MediaNotFoundError("Station not found")
        return parsers.parse_radio(prov_radio_id, self.instance_id, self.domain)

    async def search(
        self,
        search_query: str,
        media_types: list[MediaType],
        limit: int = 5,
    ) -> SearchResults:
        """
        Search Mother Earth Radio channels by name or description.

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
            channel_name = channel_info["name"].lower()
            channel_desc = channel_info["description"].lower()
            if search_query_lower in channel_name or search_query_lower in channel_desc:
                radios.append(parsers.parse_radio(channel_id, self.instance_id, self.domain))
                if len(radios) >= limit:
                    break
        results.radio = radios
        return results

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """
        Return stream details for the given radio channel item_id.

        :param item_id: Channel key, e.g. "motherearth_klassik".
        :param media_type: Must be MediaType.RADIO.
        """
        if media_type != MediaType.RADIO:
            raise UnplayableMediaError(f"Unsupported media type: {media_type}")
        if item_id not in MER_CHANNELS:
            raise MediaNotFoundError(f"Unknown radio channel: {item_id}")

        channel_info = MER_CHANNELS[item_id]

        stream_details = StreamDetails(
            item_id=item_id,
            provider=self.instance_id,
            audio_format=AudioFormat(
                content_type=channel_info["content_type"],
                sample_rate=MER_SAMPLE_RATE,
                bit_depth=MER_BIT_DEPTH,
                channels=2,
            ),
            media_type=MediaType.RADIO,
            stream_type=StreamType.HTTP,
            path=channel_info["stream_url"],
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
        return [
            parsers.parse_radio(channel_id, self.instance_id, self.domain)
            for channel_id in MER_CHANNELS
        ]

    async def _get_nowplaying(self, channel_id: str) -> dict[str, Any] | None:
        """
        Fetch current now-playing data from the AzuraCast API.

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
                        "AzuraCast API returned status %s for %s", response.status, channel_id
                    )
                    return None
                result: dict[str, Any] = await response.json()
                return result

        except aiohttp.ClientError as exc:
            self.logger.debug("AzuraCast API request failed for %s: %s", channel_id, exc)
            return None
        except (KeyError, ValueError, TypeError) as exc:
            self.logger.warning(
                "Unexpected AzuraCast API response structure for %s: %s", channel_id, exc
            )
            return None

    async def _update_stream_metadata(
        self, stream_details: StreamDetails, elapsed_time: int
    ) -> None:
        """
        Update stream metadata with current track info from AzuraCast.

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

                show_upcoming = stream_details.data.get("show_upcoming", False)

                stream_metadata = parsers.build_stream_metadata(
                    np,
                    nowplaying.get("playing_next"),
                    show_upcoming=show_upcoming,
                )

                self.logger.debug(
                    "Updating stream metadata for %s: %s - %s",
                    item_id,
                    stream_metadata.artist,
                    stream_metadata.title,
                )
                stream_details.stream_metadata = stream_metadata

                # Toggle for next update
                stream_details.data["show_upcoming"] = not show_upcoming

        except aiohttp.ClientError as exc:
            self.logger.debug("Network error updating metadata for %s: %s", item_id, exc)
