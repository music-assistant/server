"""
AudioAddict Music Provider for Music Assistant.

This provider supports the AudioAddict network of streaming radio services:
- DI.FM (Digitally Imported)
- RadioTunes
- RockRadio
- JazzRadio
- ClassicalRadio
- ZenRadio

The provider requires a premium AudioAddict account and listen key for authentication.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any

from music_assistant_models.enums import (
    ConfigEntryType,
    ContentType,
    ImageType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import MediaNotFoundError, ProviderUnavailableError
from music_assistant_models.media_items import (
    AudioFormat,
    BrowseFolder,
    MediaItemImage,
    MediaItemMetadata,
    MediaItemType,
    ProviderMapping,
    Radio,
    SearchResults,
    UniqueList,
)
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.controllers.cache import use_cache
from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import (
        ConfigEntry,
        ConfigValueOption,
        ConfigValueType,
        ProviderConfig,
    )
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType
else:
    from music_assistant_models.config_entries import (
        ConfigEntry,
        ConfigValueOption,
    )

SUPPORTED_FEATURES = {
    ProviderFeature.BROWSE,
    ProviderFeature.SEARCH,
    ProviderFeature.LIBRARY_RADIOS,
}

# AudioAddict networks configuration
NETWORKS = {
    "di": {
        "domain": "di.fm",
        "display_name": "DigitallyImported",
        "description": "Electronic music radio stations",
    },
    "radiotunes": {
        "domain": "radiotunes.com",
        "display_name": "RadioTunes",
        "description": "Variety music radio stations",
    },
    "rockradio": {
        "domain": "rockradio.com",
        "display_name": "RockRadio",
        "description": "Rock music radio stations",
    },
    "jazzradio": {
        "domain": "jazzradio.com",
        "display_name": "JazzRadio",
        "description": "Jazz music radio stations",
    },
    "classicalradio": {
        "domain": "classicalradio.com",
        "display_name": "ClassicalRadio",
        "description": "Classical music radio stations",
    },
    "zenradio": {
        "domain": "zenradio.com",
        "display_name": "ZenRadio",
        "description": "Ambient and relaxation radio stations",
    },
}

QUALITY_SETTINGS = {
    "low": "premium_medium",  # 64k AAC-HE
    "medium": "premium",  # 128k AAC
    "high": "premium_high",  # 320k MP3 (Ultra)
}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return AudioAddictProvider(mass, manifest, config, SUPPORTED_FEATURES)


# ruff: noqa: ARG001
async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """Return Config entries to setup this provider."""
    entries = []

    # Listen key configuration
    entries.append(
        ConfigEntry(
            key="listen_key",
            type=ConfigEntryType.STRING,
            label="Listen Key",
            description="Your AudioAddict premium listen key. Get this from your account settings.",
            required=True,
        )
    )

    # Quality setting
    entries.append(
        ConfigEntry(
            key="quality",
            type=ConfigEntryType.STRING,
            label="Stream Quality",
            description="Audio quality preference for streams",
            default_value="medium",
            required=True,
            options=[
                ConfigValueOption("Low - 64k AAC-HE", "low"),
                ConfigValueOption("Medium - 128k AAC", "medium"),
                ConfigValueOption("High - 320k MP3", "high"),
            ],
        )
    )

    # Network activation settings
    for network_key, network_info in NETWORKS.items():
        entries.append(
            ConfigEntry(
                key=f"activate_{network_key}",
                type=ConfigEntryType.BOOLEAN,
                label=f"Enable {network_info['display_name']}",
                description=f"Enable access to {network_info['description']}",
                default_value=True,
                required=False,
            )
        )

    return tuple(entries)


class AudioAddictProvider(MusicProvider):
    """AudioAddict Music Provider."""

    def __init__(
        self,
        mass: MusicAssistant,
        manifest: ProviderManifest,
        config: ProviderConfig,
        supported_features: set[ProviderFeature],
    ) -> None:
        """Initialize AudioAddict provider."""
        super().__init__(mass, manifest, config, supported_features)

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        # Test API connectivity by trying to get stats/channels from a network
        for network_key in self._get_active_networks():
            try:
                await self._get_channels(network_key)
                break
            except Exception as err:
                self.logger.warning("Failed to connect to network %s: %s", network_key, err)
                continue
        else:
            # If no networks are accessible, raise an error
            msg = "AudioAddict API unavailable - no networks accessible"
            raise ProviderUnavailableError(msg)

    @property
    def is_streaming_provider(self) -> bool:
        """Return True if the provider is a streaming provider."""
        return True

    async def search(
        self,
        search_query: str,
        media_types: list[MediaType],
        limit: int = 5,
    ) -> SearchResults:
        """Perform search on AudioAddict channels."""
        results = SearchResults()

        if MediaType.RADIO not in media_types:
            return results

        search_query_lower = search_query.lower()
        radios = []

        # Search across all active networks
        for network_key in self._get_active_networks():
            try:
                channels = await self._get_channels(network_key)

                for channel_data in channels:
                    if search_query_lower in str(channel_data["name"]).lower():
                        radio = self._channel_to_radio(channel_data, network_key)
                        radios.append(radio)

                        if len(radios) >= limit:
                            break

            except Exception as err:
                self.logger.warning("Search failed for network %s: %s", network_key, err)
                continue

            if len(radios) >= limit:
                break

        results.radio = radios
        return results

    async def get_library_radios(self) -> AsyncGenerator[Radio, None]:
        """Retrieve all radio stations from active networks."""
        for network_key in self._get_active_networks():
            try:
                channels = await self._get_channels(network_key)

                for channel_data in channels:
                    yield self._channel_to_radio(channel_data, network_key)

            except Exception as err:
                self.logger.warning("Failed to get channels for network %s: %s", network_key, err)
                continue

    async def get_radio(self, prov_radio_id: str) -> Radio:
        """Get full radio details by id."""
        try:
            # Parse the provider ID to get network and channel keys
            network_key, channel_key = prov_radio_id.split(":", 1)
        except ValueError as err:
            msg = f"Invalid radio ID format: {prov_radio_id}"
            raise MediaNotFoundError(msg) from err

        channels = await self._get_channels(network_key)

        for channel_data in channels:
            if channel_data["key"] == channel_key:
                return self._channel_to_radio(channel_data, network_key)

        msg = f"Radio station not found: {prov_radio_id}"
        raise MediaNotFoundError(msg)

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Get streamdetails for a radio station."""
        if media_type != MediaType.RADIO:
            msg = f"Unsupported media type: {media_type}"
            raise ValueError(msg)

        try:
            # Parse the provider ID
            network_key, channel_key = item_id.split(":", 1)
        except ValueError as err:
            msg = f"Invalid item ID format: {item_id}"
            raise MediaNotFoundError(msg) from err

        # Get the stream URL
        stream_url = await self._get_stream_url(network_key, channel_key)

        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=AudioFormat(
                content_type=ContentType.UNKNOWN,  # Let ffmpeg auto-detect
            ),
            media_type=MediaType.RADIO,
            stream_type=StreamType.HTTP,
            path=stream_url,
            allow_seek=False,
            can_seek=False,
        )

    async def browse(self, path: str) -> list[MediaItemType | BrowseFolder]:
        """Browse AudioAddict networks and channels."""
        self.logger.debug("Browse called with path: %s", path)

        # Parse the path to extract the actual browse path
        path_parts = [] if "://" not in path else path.split("://")[1].split("/")
        # Filter out empty parts and get the meaningful path components
        meaningful_parts = [part for part in path_parts if part]
        subpath = meaningful_parts[0] if len(meaningful_parts) > 0 else ""

        self.logger.debug("Parsed subpath: %s", subpath)

        if not subpath:
            # Return root level - show networks
            items: list[MediaItemType | BrowseFolder] = []

            active_networks = self._get_active_networks()
            self.logger.debug("Active networks: %s", active_networks)

            for network_key in active_networks:
                network_info = NETWORKS[network_key]
                folder = BrowseFolder(
                    item_id=network_key,
                    provider=self.instance_id,
                    path=f"{path}{network_key}"
                    if path.endswith("://")
                    else f"{path}/{network_key}",
                    name=network_info["display_name"],
                )
                items.append(folder)
                self.logger.debug("Added network folder: %s", network_info["display_name"])

            self.logger.debug("Returning %d network folders", len(items))
            return items

        # Show channels for the selected network
        if subpath in NETWORKS:
            self.logger.debug("Browsing channels for network: %s", subpath)
            try:
                channels = await self._get_channels(subpath)
                self.logger.debug("Found %d channels for network %s", len(channels), subpath)
                radio_items: list[MediaItemType | BrowseFolder] = [
                    self._channel_to_radio(ch, subpath) for ch in channels
                ]
                self.logger.debug("Converted to %d radio items", len(radio_items))
                return radio_items
            except Exception as err:
                self.logger.warning("Failed to browse network %s: %s", subpath, err)
                return []

        self.logger.debug("No matching path found, returning empty list")
        return []

    def _get_active_networks(self) -> list[str]:
        """Get list of active/enabled networks."""
        active = []
        for network_key in NETWORKS:
            if self.config.get_value(f"activate_{network_key}", True):
                active.append(network_key)
        return active

    @use_cache(86400)  # Cache for 24 hours
    async def _get_channels(self, network_key: str) -> list[dict[str, Any]]:
        """Get channels for a specific network."""
        try:
            # Get all channels
            base_url = f"api.audioaddict.com/v1/{network_key}"

            async with self.mass.http_session.get(f"http://{base_url}/channels") as resp:
                resp.raise_for_status()
                all_channels = await resp.json()

            # Get listenable channels
            async with self.mass.http_session.get(f"http://{base_url}/listen/channels") as resp:
                resp.raise_for_status()
                listen_channels_data = await resp.json()

            listen_channel_keys = {ch["key"] for ch in listen_channels_data}

            # Filter to only listenable channels
            return [ch for ch in all_channels if ch["key"] in listen_channel_keys]

        except Exception as err:
            self.logger.error("Failed to get channels for network %s: %s", network_key, err)
            raise

    @use_cache(300)  # Cache for 5 minutes to avoid multiple API calls
    async def _get_stream_url(self, network_key: str, channel_key: str) -> str:
        """Get the streaming URL for a channel."""
        self.logger.debug("Getting stream URL for %s:%s", network_key, channel_key)

        listen_key = self.config.get_value("listen_key")
        if not listen_key:
            msg = "Listen key not configured"
            raise ValueError(msg)

        quality = str(self.config.get_value("quality", "medium"))
        stream_key = QUALITY_SETTINGS.get(quality, "premium")
        self.logger.debug("Using quality setting: %s -> stream_key: %s", quality, stream_key)

        base_url = f"api.audioaddict.com/v1/{network_key}"

        try:
            # Get playlist with stream URLs
            url = f"https://{base_url}/listen/{stream_key}/{channel_key}?listen_key={listen_key}"
            self.logger.debug("Requesting playlist from: %s", url.replace(str(listen_key), "***"))

            async with self.mass.http_session.get(url) as resp:
                self.logger.debug("Playlist API response status: %d", resp.status)
                if resp.status == 403:
                    msg = "Invalid listen key or insufficient permissions"
                    raise ValueError(msg)
                resp.raise_for_status()
                playlist = await resp.json()

            # Use the first stream URL from the playlist
            self.logger.debug("AudioAddict playlist returned %d URLs", len(playlist))
            if not playlist:
                msg = "No stream URLs returned from AudioAddict API"
                raise RuntimeError(msg)

            # Log all available URLs for debugging
            for i, url in enumerate(playlist):
                self.logger.debug("Available stream URL %d: %s", i + 1, url)

            # Use the first URL - AudioAddict typically returns them in priority order
            stream_url: str = str(playlist[0])
            self.logger.debug("Selected stream URL: %s", stream_url)

            # Validate the stream URL
            if not stream_url or not isinstance(stream_url, str):
                msg = f"Invalid stream URL received: {stream_url}"
                raise RuntimeError(msg)

            self.logger.debug("Final stream URL: %s", stream_url)
            return stream_url

        except Exception as err:
            self.logger.error(
                "Failed to get stream URL for %s:%s: %s", network_key, channel_key, err
            )
            raise

    def _channel_to_radio(self, channel_data: dict[str, Any], network_key: str) -> Radio:
        """Convert channel data to Radio object."""
        # Create provider ID as network:channel_key
        prov_id = f"{network_key}:{channel_data['key']}"

        # Get image URL
        image_url = None
        if "images" in channel_data and "default" in channel_data["images"]:
            image_url = channel_data["images"]["default"]
            if image_url.startswith("//"):
                image_url = f"http:{image_url}"
            # Remove template parts if present
            image_url = image_url.split("{")[0]

        network_info = NETWORKS[network_key]

        # Create metadata with optional image
        metadata = MediaItemMetadata(
            description=f"{network_info['description']} - {channel_data['name']}",
            explicit=False,
        )

        # Add image if available
        if image_url:
            metadata.images = UniqueList(
                [
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=image_url,
                        provider=self.instance_id,
                        remotely_accessible=True,
                    )
                ]
            )

        return Radio(
            item_id=prov_id,
            provider=self.instance_id,
            name=str(channel_data["name"]),
            provider_mappings={
                ProviderMapping(
                    item_id=prov_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    available=True,
                    audio_format=AudioFormat(
                        content_type=ContentType.UNKNOWN,
                    ),
                )
            },
            metadata=metadata,
        )
