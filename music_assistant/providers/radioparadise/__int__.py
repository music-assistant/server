"""
Radio Paradise Music Provider for Music Assistant.

This provider integrates Radio Paradise's high-quality streaming channels into Music Assistant.
Radio Paradise offers multiple curated channels with FLAC/high bitrate streaming and rich metadata.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any

import aiohttp
from music_assistant_models.config_entries import (
    ConfigEntry,
    ConfigValueOption,
    ProviderConfig,
)
from music_assistant_models.enums import (
    ConfigEntryType,
    ContentType,
    ImageType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.media_items import (
    AudioFormat,
    MediaItemImage,
    ProviderMapping,
    Radio,
)
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

# Radio Paradise channel configurations with hardcoded channels
RADIO_PARADISE_CHANNELS = {
    "0": {
        "name": "Radio Paradise - Main Mix",
        "description": "Eclectic mix of music - hand-picked by real humans",
        "stream_urls": {
            "flac": "https://stream.radioparadise.com/flacm",
            "aac-320": "https://stream.radioparadise.com/aac-320",
            "mp3-192": "https://stream.radioparadise.com/mp3-192",
            "aac-128": "https://stream.radioparadise.com/aac-128",
            "aac-64": "https://stream.radioparadise.com/aac-64",
        },
        "api_url": "https://api.radioparadise.com/api/now_playing",
    },
    "1": {
        "name": "Radio Paradise - Mellow Mix",
        "description": "A mellower selection from the RP music library",
        "stream_urls": {
            "flac": "https://stream.radioparadise.com/mellow-flacm",
            "aac-320": "https://stream.radioparadise.com/mellow-320",
            "mp3-192": "https://stream.radioparadise.com/mellow-192",
            "aac-128": "https://stream.radioparadise.com/mellow-128",
            "aac-64": "https://stream.radioparadise.com/mellow-64",
        },
        "api_url": "https://api.radioparadise.com/api/now_playing?chan=1",
    },
    "2": {
        "name": "Radio Paradise - Rock Mix",
        "description": "Heavier selections from the RP music library",
        "stream_urls": {
            "flac": "https://stream.radioparadise.com/rock-flacm",
            "aac-320": "https://stream.radioparadise.com/rock-320",
            "mp3-192": "https://stream.radioparadise.com/rock-192",
            "aac-128": "https://stream.radioparadise.com/rock-128",
            "aac-64": "https://stream.radioparadise.com/rock-64",
        },
        "api_url": "https://api.radioparadise.com/api/now_playing?chan=2",
    },
    "3": {
        "name": "Radio Paradise - Global",
        "description": "Global music and experimental selections",
        "stream_urls": {
            "flac": "https://stream.radioparadise.com/global-flacm",
            "aac-320": "https://stream.radioparadise.com/global-320",
            "mp3-192": "https://stream.radioparadise.com/global-192",
            "aac-128": "https://stream.radioparadise.com/global-128",
            "aac-64": "https://stream.radioparadise.com/global-64",
        },
        "api_url": "https://api.radioparadise.com/api/now_playing?chan=3",
    },
    "4": {
        "name": "Radio Paradise - Beyond",
        "description": "Exploring the frontiers of improvisational music",
        "stream_urls": {
            "flac": "https://stream.radioparadise.com/beyond-flacm",
            "aac-320": "https://stream.radioparadise.com/beyond-320",
            "mp3-192": "https://stream.radioparadise.com/beyond-192",
            "aac-128": "https://stream.radioparadise.com/beyond-128",
            "aac-64": "https://stream.radioparadise.com/beyond-64",
        },
        "api_url": "https://api.radioparadise.com/api/now_playing?chan=4",
    },
    "5": {
        "name": "Radio Paradise - Serenity",
        "description": "Don't panic, and don't forget your towel",
        "stream_urls": {
            "aac-128": "https://stream.radioparadise.com/serenity",
        },
        "api_url": "https://api.radioparadise.com/api/now_playing?chan=5",
    },
}

# Stream format configurations
STREAM_FORMATS = ["flac", "aac-320", "mp3-192", "aac-128", "aac-64"]
BITRATE_FORMATS = {
    "flac": {"content_type": ContentType.FLAC, "sample_rate": 44100, "bit_depth": 32},
    "aac-320": {"content_type": ContentType.AAC, "sample_rate": 44100, "bit_depth": 16},
    "mp3-192": {"content_type": ContentType.MP3, "sample_rate": 44100, "bit_depth": 16},
    "aac-128": {"content_type": ContentType.AAC, "sample_rate": 44100, "bit_depth": 16},
    "aac-64": {"content_type": ContentType.AAC, "sample_rate": 44100, "bit_depth": 16},
}

# Ordered list of formats for fallback logic
FALLBACK_ORDER = ["flac", "aac-320", "mp3-192", "aac-128", "aac-64"]


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return RadioParadiseProvider(mass, manifest, config)


async def get_config_entries(
    mass: MusicAssistant,  # noqa: ARG001
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,  # noqa: ARG001
    values: dict[str, Any] | None = None,  # noqa: ARG001
) -> tuple[ConfigEntry, ...]:
    """Return Config entries to setup this provider."""
    return (
        ConfigEntry(
            key="stream_format",
            type=ConfigEntryType.STRING,
            label="Stream Quality",
            description="Choose the audio quality/format for streams",
            required=True,
            default_value="flac",
            options=[
                ConfigValueOption(title="FLAC (Lossless)", value="flac"),
                ConfigValueOption(title="AAC 320kbps", value="aac-320"),
                ConfigValueOption(title="MP3 192kbps", value="mp3-192"),
                ConfigValueOption(title="AAC 128kbps", value="aac-128"),
                ConfigValueOption(title="AAC 64kbps", value="aac-64"),
            ],
        ),
    )


class RadioParadiseProvider(MusicProvider):
    """Radio Paradise Music Provider for Music Assistant."""

    def __init__(self, mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig):
        """Initialize the provider."""
        super().__init__(mass, manifest, config)
        self._channels_cache: dict[str, dict] = RADIO_PARADISE_CHANNELS.copy()
        self._stream_format = self.config.get_value("stream_format", "flac")
        self._monitor_task: asyncio.Task | None = None

    @property
    def supported_features(self) -> set[ProviderFeature]:
        """Return the features supported by this Provider."""
        return {
            ProviderFeature.LIBRARY_RADIOS,
            ProviderFeature.BROWSE,
            ProviderFeature.ALBUM_METADATA,
        }

    @property
    def is_streaming_provider(self) -> bool:
        """Return True if the provider is a streaming provider."""
        return True

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""
        if self.mass and hasattr(self.mass, "music") and hasattr(self.mass.music, "sync_library"):
            self.mass.create_task(self.mass.music.sync_library(self.instance_id, MediaType.RADIO))

    async def _get_channel_metadata(self, channel_id: str) -> dict:
        """Get current playing metadata for a channel."""
        if channel_id not in self._channels_cache:
            return {}

        channel_info = self._channels_cache[channel_id]
        api_url = channel_info["api_url"]

        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with (
                aiohttp.ClientSession(timeout=timeout) as session,
                session.get(api_url) as response,
            ):
                if response.status == 200:
                    data = await response.json()

                    if "artist" in data:
                        current_song = data
                    elif "song" in data and len(data["song"]) > 0:
                        current_song = data["song"][0]
                    else:
                        self.logger.debug(f"No song data in API response for channel {channel_id}")
                        return {}

                    cover_url = current_song.get("cover")
                    # Extract the track duration from the TIME key
                    track_duration = current_song.get("time")

                    return {
                        "title": current_song.get("title", "TEST"),
                        "artist": current_song.get("artist", "BLAH"),
                        "album": current_song.get("album", ""),
                        "cover_url": cover_url,
                        # Add the track duration to the metadata dictionary
                        "duration": track_duration,
                    }
        except Exception as exc:
            self.logger.debug(f"Failed to get metadata for channel {channel_id}: {exc}")

        return {}

    def _build_stream_url(self, channel_id: str) -> str:
        """Build stream URL for a channel with fallback to other formats."""
        if channel_id not in self._channels_cache:
            return ""

        channel_info = self._channels_cache[channel_id]
        stream_urls = channel_info.get("stream_urls", {})

        if self._stream_format in stream_urls:
            return stream_urls[self._stream_format]

        current_format_index = FALLBACK_ORDER.index(self._stream_format)
        for format_key in FALLBACK_ORDER[current_format_index + 1 :]:
            if format_key in stream_urls:
                self.logger.warning(
                    f"Preferred stream format '{self._stream_format}' not available for "
                    f"channel {channel_id}. Falling back to '{format_key}'."
                )
                return stream_urls[format_key]

        if stream_urls:
            first_available = next(iter(stream_urls.keys()))
            self.logger.warning(
                f"Preferred stream format '{self._stream_format}' and all "
                f"fallbacks not available for channel {channel_id}. Using "
                f"first available format '{first_available}'."
            )
            return stream_urls[first_available]

        self.logger.error(f"No streams available for channel {channel_id}.")
        return ""

    async def get_library_radios(self) -> AsyncGenerator[Radio, None]:
        """Retrieve library/subscribed radio stations from the provider."""
        for channel_id, channel_info in self._channels_cache.items():
            metadata = await self._get_channel_metadata(channel_id)
            stream_url = self._build_stream_url(channel_id)
            stream_format = next(
                (k for k, v in channel_info["stream_urls"].items() if v == stream_url),
                self._stream_format,
            )
            format_info = BITRATE_FORMATS.get(stream_format, BITRATE_FORMATS["flac"])

            radio = Radio(
                item_id=channel_id,
                provider=self.instance_id,
                name=channel_info["name"],
                provider_mappings={
                    ProviderMapping(
                        item_id=channel_id,
                        provider_domain=self.domain,
                        provider_instance=self.instance_id,
                        available=True,
                        audio_format=AudioFormat(
                            content_type=format_info["content_type"],
                            sample_rate=format_info["sample_rate"],
                            bit_depth=format_info["bit_depth"],
                            channels=2,
                        ),
                    )
                },
            )

            # Add current track metadata and cover art
            if metadata:
                # Set the radio name to include current track info
                if metadata.get("title") and metadata.get("artist"):
                    radio.metadata.description = (
                        f"Now Playing: {metadata['artist']} - {metadata['title']}"
                    )
                # Set the track duration
                if metadata.get("duration"):
                    radio.duration = metadata["duration"]

                # Add cover art if available
                if metadata.get("cover_url"):
                    radio.metadata.images = [
                        MediaItemImage(
                            type=ImageType.THUMB,
                            path=metadata["cover_url"],
                            remotely_accessible=True,
                            provider=self.instance_id,
                        )
                    ]

            yield radio

    async def get_radio(self, prov_radio_id: str) -> Radio:
        """Get full radio details by id."""
        if prov_radio_id not in self._channels_cache:
            raise ValueError(f"Unknown radio channel: {prov_radio_id}")

        channel_info = self._channels_cache[prov_radio_id]
        metadata = await self._get_channel_metadata(prov_radio_id)
        stream_url = self._build_stream_url(prov_radio_id)
        stream_format = next(
            (k for k, v in channel_info["stream_urls"].items() if v == stream_url),
            self._stream_format,
        )
        format_info = BITRATE_FORMATS.get(stream_format, BITRATE_FORMATS["flac"])

        radio = Radio(
            item_id=prov_radio_id,
            provider=self.instance_id,
            name=channel_info["name"],
            provider_mappings={
                ProviderMapping(
                    item_id=prov_radio_id,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    available=True,
                    audio_format=AudioFormat(
                        content_type=format_info["content_type"],
                        sample_rate=format_info["sample_rate"],
                        bit_depth=format_info["bit_depth"],
                        channels=2,
                    ),
                )
            },
        )

        # Add current track metadata and cover art
        if metadata:
            # Set the radio description to include current track info
            if metadata.get("title") and metadata.get("artist"):
                radio.metadata.description = (
                    f"Now Playing: {metadata['artist']} - {metadata['title']}"
                )
            # Set the track duration
            if metadata.get("duration"):
                radio.duration = metadata["duration"]

            # Add cover art if available
            if metadata.get("cover_url"):
                radio.metadata.images = [
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=metadata["cover_url"],
                        remotely_accessible=True,
                        provider=self.instance_id,
                    )
                ]

        return radio

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Get streamdetails for a radio station."""
        if media_type != MediaType.RADIO:
            raise ValueError(f"Unsupported media type: {media_type}")

        if item_id not in self._channels_cache:
            raise ValueError(f"Unknown radio channel: {item_id}")

        stream_url = self._build_stream_url(item_id)
        if not stream_url:
            raise ValueError(f"No stream URL found for channel {item_id}")

        channel_info = self._channels_cache[item_id]
        stream_format = next(
            (k for k, v in channel_info["stream_urls"].items() if v == stream_url),
            self._stream_format,
        )
        format_info = BITRATE_FORMATS.get(stream_format, BITRATE_FORMATS["flac"])

        # Fetch metadata to get the duration
        metadata = await self._get_channel_metadata(item_id)
        track_duration = metadata.get("duration", 0)

        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=AudioFormat(
                content_type=format_info["content_type"],
                sample_rate=format_info["sample_rate"],
                bit_depth=format_info["bit_depth"],
                channels=2,
            ),
            media_type=MediaType.RADIO,
            stream_type=StreamType.HTTP,
            path=stream_url,
            allow_seek=False,
            can_seek=False,
            # Pass the track duration to the StreamDetails object
            duration=track_duration,
        )

    async def on_streamed(self, streamdetails: StreamDetails) -> None:
        """Handle callback when given streamdetails completed streaming."""
        self.logger.debug(
            f"Radio Paradise channel {streamdetails.item_id} streamed for "
            f"{streamdetails.seconds_streamed} seconds"
        )
        # Cancel the metadata monitor task when streaming ends
        if self._monitor_task:
            self._monitor_task.cancel()
            self._monitor_task = None

    async def browse(self, path: str) -> list[Radio]:
        """Browse this provider's radio stations."""
        radios = []
        async for radio in self.get_library_radios():
            radios.append(radio)
        return radios
