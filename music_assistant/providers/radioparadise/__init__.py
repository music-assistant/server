"""Radio Paradise Music Provider for Music Assistant."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncGenerator, Sequence
from typing import TYPE_CHECKING, Any, cast

import aiohttp
from music_assistant_models.config_entries import (
    ConfigEntry,
    ConfigValueOption,
    ConfigValueType,
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
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import (
    AudioFormat,
    BrowseFolder,
    ItemMapping,
    MediaItemImage,
    MediaItemType,
    ProviderMapping,
    Radio,
    UniqueList,
)
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from music_assistant_models.provider import ProviderManifest

    from music_assistant import MusicAssistant
    from music_assistant.models import ProviderInstanceType

# Radio Paradise channel configurations with hardcoded channels
RADIO_PARADISE_CHANNELS: dict[str, dict[str, Any]] = {
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
BITRATE_FORMATS: dict[str, dict[str, int | ContentType]] = {
    "flac": {"content_type": ContentType.FLAC},
    "aac-320": {"content_type": ContentType.AAC},
    "mp3-192": {"content_type": ContentType.MP3},
    "aac-128": {"content_type": ContentType.AAC},
    "aac-64": {"content_type": ContentType.AAC},
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
    values: dict[str, ConfigValueType] | None = None,  # noqa: ARG001
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
        self._channels_cache: dict[str, dict[str, Any]] = RADIO_PARADISE_CHANNELS.copy()
        self._stream_format: str = cast("str", self.config.get_value("stream_format", "flac"))

    @property
    def supported_features(self) -> set[ProviderFeature]:
        """Return the features supported by this Provider."""
        return {
            ProviderFeature.BROWSE,
            ProviderFeature.LIBRARY_RADIOS,
            ProviderFeature.TRACK_METADATA,
        }

    @property
    def is_streaming_provider(self) -> bool:
        """Return True if the provider is a streaming provider."""
        return True

    async def get_library_radios(self) -> AsyncGenerator[Radio, None]:
        """Retrieve library/subscribed radio stations from the provider."""
        for channel_id in self._channels_cache:
            yield await self._parse_radio(channel_id)

    async def get_radio(self, prov_radio_id: str) -> Radio:
        """Get full radio details by id."""
        if prov_radio_id not in self._channels_cache:
            raise MediaNotFoundError("Station not found")
        return await self._parse_radio(prov_radio_id)

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

        stream_details = StreamDetails(
            item_id=item_id,
            provider=self.lookup_key,
            audio_format=AudioFormat(
                content_type=cast("ContentType", format_info["content_type"]),
                channels=2,
            ),
            media_type=MediaType.RADIO,
            stream_type=StreamType.HTTP,
            path=stream_url,
            allow_seek=False,
            can_seek=False,
            duration=0,
        )

        # Store the monitoring task in streamdetails.data
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
        return [await self._parse_radio(channel_id) for channel_id in self._channels_cache]

    async def _parse_radio(self, channel_id: str) -> Radio:
        """Create a Radio object from channel information and fetch cover art from API."""
        channel_info = cast("dict[str, str]", self._channels_cache.get(channel_id, {}))
        radio = Radio(
            provider=self.lookup_key,
            item_id=channel_id,
            name=channel_info.get("name", "Unknown Radio"),
            provider_mappings={
                ProviderMapping(
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    item_id=channel_id,
                    available=True,
                )
            },
        )
        radio.metadata.description = channel_info.get("description")

        # Fetch the current metadata to get the latest cover image for the browse view.
        metadata = await self._get_channel_metadata(channel_id)
        cover_url = cast("str | None", metadata.get("cover_url")) if metadata else None

        if cover_url:
            images = [
                MediaItemImage(
                    provider=self.lookup_key,
                    type=ImageType.THUMB,
                    path=cover_url,
                    remotely_accessible=True,
                )
            ]
            radio.metadata.images = UniqueList(images)

        return radio

    async def _get_channel_metadata(self, channel_id: str) -> dict[str, Any] | None:
        """Get current playing metadata for a channel."""
        if channel_id not in self._channels_cache:
            return None
    
        channel_info = self._channels_cache[channel_id]
        api_url = channel_info["api_url"]
    
        try:
            # Create a ClientTimeout object for the request
            timeout = aiohttp.ClientTimeout(total=10)
            async with self.mass.http_session.get(api_url, timeout=timeout) as response:
                if response.status != 200:
                    self.logger.debug(f"API call to {api_url} failed with status {response.status}")
                    return None
    
                data = await response.json()
                if "artist" in data:
                    current_song = data
                elif "song" in data and len(data["song"]) > 0:
                    current_song = data["song"][0]
                else:
                    self.logger.debug(f"No song data in API response for channel {channel_id}")
                    return None
    
                return {
                    "title": current_song.get("title", ""),
                    "artist": current_song.get("artist", ""),
                    "cover_url": current_song.get("cover"),
                    "duration": current_song.get("time"),
                }
        except aiohttp.ClientError as exc:
            self.logger.debug(f"Failed to get metadata for channel {channel_id}: {exc}")
            return None
        except Exception as exc:
            self.logger.debug(f"Unexpected error getting metadata for channel {channel_id}: {exc}")
            return None

    def _build_stream_url(self, channel_id: str) -> str:
        """Build stream URL for a channel with fallback to other formats."""
        if channel_id not in self._channels_cache:
            return ""

        channel_info = self._channels_cache[channel_id]
        stream_urls = channel_info.get("stream_urls", {})

        try:
            current_format_index = FALLBACK_ORDER.index(self._stream_format)
        except ValueError:
            # If preferred format not in fallback order, start from beginning
            current_format_index = 0

        for format_key in FALLBACK_ORDER[current_format_index:]:
            if format_key in stream_urls:
                return cast("str", stream_urls[format_key])

        return ""

    async def _monitor_stream_metadata(self, stream_details: StreamDetails) -> None:
        """Monitor and update the StreamDetails object metadata every 10 seconds."""
        last_track_title = ""
        item_id = stream_details.item_id

        try:
            while True:  # Continue until cancelled
                metadata = await self._get_channel_metadata(item_id)
                if metadata:
                    track_title = cast("str", metadata.get("title", "Unknown Title"))
                    artist = cast("str", metadata.get("artist", "Unknown Artist"))

                    if track_title != last_track_title:
                        self.logger.info(
                            f"Updating stream metadata for {item_id}: {artist} - {track_title}"
                        )
                        stream_details.stream_title = f"{artist} - {track_title}"

                        # Future: When streammetadata is added expand this

                        last_track_title = track_title

                await asyncio.sleep(10)
        except asyncio.CancelledError:
            self.logger.debug(f"Monitor task cancelled for {item_id}")
