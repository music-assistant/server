"""SomaFM Radio music provider support for MusicAssistant."""

from __future__ import annotations

import random
from typing import TYPE_CHECKING, Any

from music_assistant_models.config_entries import ConfigEntry, ConfigValueOption, ConfigValueType
from music_assistant_models.enums import (
    ConfigEntryType,
    ContentType,
    ImageType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import (
    AudioFormat,
    MediaItemImage,
    MediaItemMetadata,
    ProviderMapping,
    Radio,
)
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.controllers.cache import use_cache
from music_assistant.helpers.playlists import PlaylistItem, fetch_playlist
from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant import MusicAssistant
    from music_assistant.models import ProviderInstanceType

SUPPORTED_FEATURES = {
    ProviderFeature.LIBRARY_RADIOS,
    ProviderFeature.BROWSE,
}

CONF_QUALITY = "quality"


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return SomaFMProvider(mass, manifest, config, SUPPORTED_FEATURES)


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """Return Config entries to setup this provider."""
    # ruff: noqa: ARG001
    return (
        ConfigEntry(
            key=CONF_QUALITY,
            advanced=True,
            type=ConfigEntryType.STRING,
            label="Stream Quality",
            options=[
                ConfigValueOption("Highest", "highest"),
                ConfigValueOption("High", "high"),
                ConfigValueOption("Low", "low"),
            ],
            default_value="highest",
        ),
    )


class SomaFMProvider(MusicProvider):
    """Provider implementation for SomaFM Radio."""

    @property
    def is_streaming_provider(self) -> bool:
        """Return True if the provider is a streaming provider."""
        return True

    async def get_library_radios(self) -> AsyncGenerator[Radio, None]:
        """Retrieve library/subscribed radio stations from the provider."""
        stations = await self._get_stations()  # May be cached
        if stations:
            for channel_info in stations.values():
                radio = self._parse_channel(channel_info)
                yield radio

    async def get_radio(self, prov_radio_id: str) -> Radio:
        """Get radio station details."""
        stations = await self._get_stations()  # May be cached
        if stations:
            radio = stations.get(prov_radio_id)
        if radio:
            return self._parse_channel(radio)
        msg = f"Item {prov_radio_id} not found"
        raise MediaNotFoundError(msg)

    @use_cache(3600 * 24 * 1)  # Cache for 1 day
    async def _get_stations(self) -> dict[str, dict[str, Any]]:
        url = "https://somafm.com/channels.json"
        locale = self.mass.metadata.locale.replace("_", "-")
        language = locale.split("-")[0]
        headers = {"Accept-Language": f"{locale}, {language};q=0.9, *;q=0.5"}
        async with (
            self.mass.http_session.get(url, headers=headers, ssl=False) as response,
        ):
            result: Any = await response.json()
            if not result or "error" in result:
                self.logger.error(url)
            elif isinstance(result, dict):
                stations = result.get("channels")
                if stations:
                    # Reformat into dict by channel id
                    return {info.get("id"): info for info in stations if info.get("id")}
            raise MediaNotFoundError("Could not fetch SomaFM stations list")

    def _parse_channel(self, channel_info: dict[str, Any]) -> Radio:
        """Convert SomaFM channel info into a Radio object."""
        # Construct radio station information
        item_id = channel_info.get("id")
        if not item_id:
            raise MediaNotFoundError("Soma FM station generation failed")

        radio = Radio(
            provider=self.instance_id,
            item_id=item_id,
            name=f"SomaFM: {channel_info.get('title', 'Unknown Radio')}",
            metadata=MediaItemMetadata(
                description=channel_info.get("description", "No description"),
                genres={channel_info.get("genre", "No genre")},
                popularity=int(channel_info.get("listeners", "0")),
                performers={
                    f"DJ: {channel_info.get('dj', 'No DJ info')}",
                    f"DJ Email: {channel_info.get('djmail', 'No DJ email')}",
                },
            ),
            provider_mappings={
                ProviderMapping(
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    item_id=item_id,
                    available=True,
                )
            },
        )

        # Add station image URL
        station_icon_url = channel_info.get("largeimage")
        if station_icon_url:
            radio.metadata.add_image(
                MediaItemImage(
                    provider=self.instance_id,
                    type=ImageType.THUMB,
                    path=station_icon_url,
                    remotely_accessible=True,
                )
            )
        return radio

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Get stream details for a track/radio."""

        async def _get_valid_playlist_item(playlist: list[PlaylistItem]) -> PlaylistItem:
            """Randomly select stream URL from playlist and test it."""
            random.shuffle(playlist)
            for item in playlist:
                async with self.mass.http_session.head(item.path, ssl=False) as response:
                    if response.status >= 100 and response.status < 300:
                        # Stream exists, return valid path
                        return item
            self.logger.error("Could not find a working stream for playlist")
            raise MediaNotFoundError("No valid SomaFM stream available")

        def _get_playlist_url(station: dict[str, Any]) -> str:
            """Pick playlist based on quality config value."""
            req_quality = self.config.get_value(CONF_QUALITY)
            playlists: list[dict[str, str]] = station.get("playlists", [])

            # Remove MP3 playlist options for now; AAC is generally better
            playlists = [
                playlist for playlist in playlists if playlist["format"] in {"aac", "aacp"}
            ]

            # Sort by quality just in case they already aren't sorted highest/high/low
            quality_map = {"highest": 0, "high": 1, "low": 2}
            playlists.sort(key=lambda x: quality_map[x["quality"]])

            # Detect empty playlist after sort and filter
            if len(playlists) == 0:
                raise MediaNotFoundError("No valid SomaFM playlist available")

            # Find the first playlist item that has the requested quality
            for playlist in playlists:
                avail_quality = playlist.get("quality")
                playlist_url = playlist.get("url")
                if req_quality == avail_quality and playlist_url:
                    return playlist_url

            self.logger.warning("Couldn't find SomaFM stream with requested quality and format")

            # Get the first (highest quality) playlist if we couldn't find requested quality
            playlist_url = playlists[0].get("url")
            if playlist_url:
                return playlist_url
            raise MediaNotFoundError("No valid SomaFM playlist available")

        async def _get_stream_path(item_id: str) -> str:
            """Pick correct playlist, fetch the playlist, and extract stream URL."""
            stations = await self._get_stations()
            station = stations.get(item_id)
            if station:
                playlist_url = _get_playlist_url(station)
                playlist = await fetch_playlist(self.mass, playlist_url)
                playlist_item: PlaylistItem = await _get_valid_playlist_item(playlist)
                return playlist_item.path
            raise MediaNotFoundError

        stream_path = await _get_stream_path(item_id)

        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=AudioFormat(
                content_type=ContentType.UNKNOWN,
            ),
            media_type=MediaType.RADIO,
            path=stream_path,
            stream_type=StreamType.HTTP,
            allow_seek=False,
            can_seek=False,
        )
