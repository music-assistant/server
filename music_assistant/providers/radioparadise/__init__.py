"""Radio Paradise Music Provider for Music Assistant."""

from __future__ import annotations

import asyncio
import contextlib
import time
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
            "flac": "https://stream.radioparadise.com/flac",
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
            "flac": "https://stream.radioparadise.com/mellow-flac",
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
            "flac": "https://stream.radioparadise.com/rock-flac",
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
            "flac": "https://stream.radioparadise.com/global-flac",
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
            "flac": "https://stream.radioparadise.com/beyond-flac",
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
        """Create a Radio object with enhanced metadata from block API."""
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

        # Get enhanced metadata from block API
        metadata = await self._get_channel_metadata(channel_id)
        if metadata and metadata.get("current"):
            current_song = metadata["current"]

            # Use current track's cover art
            cover_path = current_song.get("cover")
            if cover_path:
                cover_url = f"https://img.radioparadise.com/{cover_path}"
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
        """Get current playing metadata and upcoming tracks from block API."""
        if channel_id not in self._channels_cache:
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
                current_time_ms = self._get_current_block_position(data)
                current_song = self._find_current_song(data.get("song", {}), current_time_ms)

                if not current_song:
                    self.logger.debug(f"No current song found for channel {channel_id}")
                    return None

                # Get next song
                next_song = self._get_next_song(data.get("song", {}), current_song)

                return {"current": current_song, "next": next_song, "block_data": data}
        except aiohttp.ClientError as exc:
            self.logger.debug(f"Failed to get block metadata for channel {channel_id}: {exc}")
            return None
        except Exception as exc:
            self.logger.debug(
                f"Unexpected error getting block metadata for channel {channel_id}: {exc}"
            )
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
        """Monitor and update the StreamDetails with rich metadata every 10 seconds."""
        last_track_event = ""
        item_id = stream_details.item_id

        try:
            while True:
                metadata = await self._get_channel_metadata(item_id)
                if metadata and metadata.get("current"):
                    current_song = metadata["current"]
                    next_song = metadata.get("next")
                    block_data = metadata.get("block_data")  # Get the full block data

                    current_event = current_song.get("event", "")

                    if current_event != last_track_event:
                        # Build rich stream title with block data for "Later" section
                        stream_title = self._build_rich_stream_title(
                            current_song, next_song, block_data
                        )

                        self.logger.debug(f"Updating stream metadata for {item_id}: {stream_title}")
                        stream_details.stream_title = stream_title

                        last_track_event = current_event

                await asyncio.sleep(10)
        except asyncio.CancelledError:
            self.logger.debug(f"Monitor task cancelled for {item_id}")

    def _get_current_block_position(self, block_data: dict) -> int:
        """Calculate current position in block based on scheduled time."""
        current_time_ms = int(time.time() * 1000)
        sched_time = block_data.get("sched_time_millis", current_time_ms)
        return current_time_ms - sched_time

    def _find_current_song(self, songs: dict, current_time_ms: int) -> dict | None:
        """Find which song should be playing based on elapsed time."""
        for song_key in sorted(songs.keys(), key=int):
            song = songs[song_key]
            song_start = song.get("elapsed", 0)
            song_duration = song.get("duration", 0)
            song_end = song_start + song_duration

            if song_start <= current_time_ms < song_end:
                return song

        # If no exact match, return first song
        return songs.get("0") if songs else None

    def _get_next_song(self, songs: dict, current_song: dict) -> dict | None:
        """Get the next song after current song."""
        current_event = current_song.get("event")
        for song_key in sorted(songs.keys(), key=int):
            song = songs[song_key]
            if song.get("event") != current_event and song.get("elapsed", 0) > current_song.get(
                "elapsed", 0
            ):
                return song
        return None

    def _build_rich_stream_title(
        self, current_song: dict, next_song: dict | None, block_data: dict | None = None
    ) -> str:
        """Build a rich, scrolling stream title with all the metadata."""
        # Current track info
        artist = current_song.get("artist", "Unknown Artist")
        title = current_song.get("title", "Unknown Title")
        year = current_song.get("year", "----")
        # Add remaining time for current track
        duration = current_song.get("duration", 0) // 1000
        mins, secs = divmod(duration, 60)

        # Build main title
        stream_title = f"Now: {artist} - {title} ({year}) ⏱️ {mins}:{secs:02d}"

        # Add next track info
        if next_song:
            next_artist = next_song.get("artist", "")
            next_title = next_song.get("title", "")
            if next_artist and next_title:
                stream_title += f" | Up Next: {next_artist} - {next_title}"

        # Add later artists from remaining songs in block
        if block_data and "song" in block_data:
            current_event = current_song.get("event")
            later_artists = []

            # Get all songs after the next song
            for song_key in sorted(block_data["song"].keys(), key=int):
                song = block_data["song"][song_key]
                song_event = song.get("event")

                # Skip current and next song
                if song_event == current_event or (
                    next_song and song_event == next_song.get("event")
                ):
                    continue

                # Only include songs that come after current song
                if song.get("elapsed", 0) > current_song.get("elapsed", 0):
                    artist_name = song.get("artist", "")
                    if artist_name and artist_name not in later_artists:
                        later_artists.append(artist_name)

            # Add later artists to stream title
            if later_artists:
                artists_list = ", ".join(later_artists[:4])  # Limit to 4 artists to avoid too long
                stream_title += f" | Later: {artists_list}"

        return stream_title
