"""Radio Paradise Music Provider for Music Assistant."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncGenerator, Sequence
from typing import TYPE_CHECKING, Any, cast

import aiohttp
from music_assistant_models.enums import (
    ContentType,
    ImageType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import MediaNotFoundError, UnplayableMediaError
from music_assistant_models.media_items import (
    AudioFormat,
    BrowseFolder,
    ItemMapping,
    MediaItemImage,
    MediaItemType,
    ProviderMapping,
    Radio,
)
from music_assistant_models.streamdetails import StreamDetails, StreamMetadata

from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import (
        ConfigEntry,
        ConfigValueType,
        ProviderConfig,
    )
    from music_assistant_models.provider import ProviderManifest

    from music_assistant import MusicAssistant
    from music_assistant.models import ProviderInstanceType

# Base URL for station icons
STATION_ICONS_BASE_URL = (
    "https://raw.githubusercontent.com/music-assistant/music-assistant.io/main/docs/assets/icons"
)

# Radio Paradise channel configurations with hardcoded channels
RADIO_PARADISE_CHANNELS: dict[str, dict[str, Any]] = {
    "0": {
        "name": "Radio Paradise - Main Mix",
        "description": "Eclectic mix of music - hand-picked by real humans",
        "stream_url": "https://stream.radioparadise.com/flac",
        "content_type": ContentType.FLAC,
        "api_url": "https://api.radioparadise.com/api/now_playing",
        "station_icon": "radioparadise-logo-main.png",
    },
    "1": {
        "name": "Radio Paradise - Mellow Mix",
        "description": "A mellower selection from the RP music library",
        "stream_url": "https://stream.radioparadise.com/mellow-flac",
        "content_type": ContentType.FLAC,
        "api_url": "https://api.radioparadise.com/api/now_playing?chan=1",
        "station_icon": "radioparadise-logo-mellow.png",
    },
    "2": {
        "name": "Radio Paradise - Rock Mix",
        "description": "Heavier selections from the RP music library",
        "stream_url": "https://stream.radioparadise.com/rock-flac",
        "content_type": ContentType.FLAC,
        "api_url": "https://api.radioparadise.com/api/now_playing?chan=2",
        "station_icon": "radioparadise-logo-rock.png",
    },
    "3": {
        "name": "Radio Paradise - Global",
        "description": "Global music and experimental selections",
        "stream_url": "https://stream.radioparadise.com/global-flac",
        "content_type": ContentType.FLAC,
        "api_url": "https://api.radioparadise.com/api/now_playing?chan=3",
        "station_icon": "radioparadise-logo-global.png",
    },
    "4": {
        "name": "Radio Paradise - Beyond",
        "description": "Exploring the frontiers of improvisational music",
        "stream_url": "https://stream.radioparadise.com/beyond-flac",
        "content_type": ContentType.FLAC,
        "api_url": "https://api.radioparadise.com/api/now_playing?chan=4",
        "station_icon": "radioparadise-logo-beyond.png",
    },
    "5": {
        "name": "Radio Paradise - Serenity",
        "description": "Don't panic, and don't forget your towel",
        "stream_url": "https://stream.radioparadise.com/serenity",
        "content_type": ContentType.AAC,
        "api_url": "https://api.radioparadise.com/api/now_playing?chan=5",
        "station_icon": "radioparadise-logo-serenity.png",
    },
}


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
    return ()


class RadioParadiseProvider(MusicProvider):
    """Radio Paradise Music Provider for Music Assistant."""

    def __init__(self, mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig):
        """Initialize the provider."""
        super().__init__(mass, manifest, config)

    @property
    def supported_features(self) -> set[ProviderFeature]:
        """Return the features supported by this Provider."""
        return {
            ProviderFeature.BROWSE,
            ProviderFeature.LIBRARY_RADIOS,
        }

    @property
    def is_streaming_provider(self) -> bool:
        """Return True if the provider is a streaming provider."""
        return True

    async def get_library_radios(self) -> AsyncGenerator[Radio, None]:
        """Retrieve library/subscribed radio stations from the provider."""
        for channel_id in RADIO_PARADISE_CHANNELS:
            yield self._parse_radio(channel_id)

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

        stream_url = self._build_stream_url(item_id)
        if not stream_url:
            raise UnplayableMediaError(f"No stream URL found for channel {item_id}")

        # Get content type from channel configuration
        channel_info = RADIO_PARADISE_CHANNELS[item_id]
        content_type = channel_info["content_type"]

        stream_details = StreamDetails(
            item_id=item_id,
            provider=self.lookup_key,
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
            stream_details.stream_metadata = self._build_stream_metadata(current_song, metadata)

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
        channel_info = RADIO_PARADISE_CHANNELS.get(channel_id, {})

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

        # Add static station icon
        station_icon = channel_info.get("station_icon")
        if station_icon:
            icon_url = f"{STATION_ICONS_BASE_URL}/{station_icon}"
            radio.metadata.add_image(
                MediaItemImage(
                    provider=self.lookup_key,
                    type=ImageType.THUMB,
                    path=icon_url,
                    remotely_accessible=True,
                )
            )

        return radio

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
                current_time_ms = self._get_current_block_position(data)
                current_song = self.find_current_song(data.get("song", {}), current_time_ms)

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

    def _get_current_block_position(self, block_data: dict[str, Any]) -> int:
        """Calculate current playback position within a Radio Paradise block.

        Args:
            block_data: Block data containing sched_time_millis

        Returns:
            Current position in milliseconds from block start
        """
        current_time_ms = int(time.time() * 1000)
        sched_time = int(block_data.get("sched_time_millis", current_time_ms))
        return current_time_ms - sched_time

    def find_current_song(
        self, songs: dict[str, dict[str, Any]], current_time_ms: int
    ) -> dict[str, Any] | None:
        """Find which song should currently be playing based on elapsed time.

        Args:
            songs: Dictionary of songs from Radio Paradise block data
            current_time_ms: Current position in milliseconds within the block

        Returns:
            The song dict that should be playing now, or None if not found
        """
        sorted_keys = sorted(songs.keys(), key=int)

        for song_key in sorted_keys:
            song = songs[song_key]
            song_start = int(song.get("elapsed", 0))
            song_duration = int(song.get("duration", 0))
            song_end = song_start + song_duration

            if song_start <= current_time_ms < song_end:
                return song

        # If no exact match, return first song
        first_song = songs.get("0")
        return first_song if first_song is not None else {}

    def _get_next_song(
        self, songs: dict[str, Any], current_song: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Get the next song that will play after the current song.

        Args:
            songs: Dictionary of songs from Radio Paradise block data
            current_song: The currently playing song dictionary

        Returns:
            The next song dict, or None if no next song found
        """
        current_event = current_song.get("event")
        current_elapsed = int(current_song.get("elapsed", 0))
        sorted_keys = sorted(songs.keys(), key=int)

        for song_key in sorted_keys:
            song = cast("dict[str, Any]", songs[song_key])
            if song.get("event") != current_event and int(song.get("elapsed", 0)) > current_elapsed:
                return song
        return None

    def _build_stream_url(self, channel_id: str) -> str:
        """Build the streaming URL for a Radio Paradise channel.

        Args:
            channel_id: Radio Paradise channel ID (0-5)

        Returns:
            Streaming URL for the channel, or empty string if not found
        """
        if channel_id not in RADIO_PARADISE_CHANNELS:
            return ""

        channel_info = RADIO_PARADISE_CHANNELS[channel_id]
        return str(channel_info.get("stream_url", ""))

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
                        stream_metadata = self._build_stream_metadata(current_song, metadata)

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

    def _build_stream_metadata(
        self, current_song: dict[str, Any], metadata: dict[str, Any]
    ) -> StreamMetadata:
        """Build StreamMetadata with current track info and upcoming tracks.

        Args:
            current_song: Current track data from Radio Paradise API
            metadata: Full metadata response with next song and block data

        Returns:
            StreamMetadata with track info and upcoming track previews
        """
        # Extract track info
        artist = current_song.get("artist", "Unknown Artist")
        title = current_song.get("title", "Unknown Title")
        album = current_song.get("album")
        year = current_song.get("year")

        # Build album string with year if available
        album_display = album
        if album and year:
            album_display = f"{album} ({year})"
        elif year:
            album_display = str(year)

        # Get cover image URL
        cover_path = current_song.get("cover")
        image_url = None
        if cover_path:
            image_url = f"https://img.radioparadise.com/{cover_path}"

        # Debug log the image URL
        self.logger.debug(f"Cover art URL for {artist} - {title}: {image_url}")

        # Get track duration
        duration = current_song.get("duration")
        if duration:
            duration = int(duration) // 1000  # Convert from ms to seconds

        # Add upcoming tracks info to title for scrolling display
        next_song = metadata.get("next")
        block_data = metadata.get("block_data")
        enhanced_title = self._enhance_title_with_upcoming(
            title, current_song, next_song, block_data
        )

        return StreamMetadata(
            title=enhanced_title,
            artist=artist,
            album=album_display,
            image_url=image_url,
            duration=duration,
        )

    def _enhance_title_with_upcoming(
        self,
        title: str,
        current_song: dict[str, Any],
        next_song: dict[str, Any] | None,
        block_data: dict[str, Any] | None,
    ) -> str:
        """Enhance track title with upcoming track info for scrolling display.

        Args:
            title: Original track title
            current_song: Current track data
            next_song: Next track data, or None if not available
            block_data: Full block data with all upcoming tracks

        Returns:
            Enhanced title with "Up Next" and "Later" information appended
        """
        enhanced_title = title

        # Add next track info
        if next_song:
            next_artist = next_song.get("artist", "")
            next_title = next_song.get("title", "")
            if next_artist and next_title:
                enhanced_title += f" | Up Next: {next_artist} - {next_title}"

        # Add later artists in a single pass with deduplication
        if block_data and "song" in block_data:
            current_event = current_song.get("event")
            current_elapsed = int(current_song.get("elapsed", 0))
            next_event = next_song.get("event") if next_song else None

            # Use set to deduplicate artist names (including next_song artist)
            seen_artists = set()
            if next_song:
                next_artist = next_song.get("artist", "")
                if next_artist:
                    seen_artists.add(next_artist)

            later_artists = []
            sorted_keys = sorted(block_data["song"].keys(), key=int)
            for song_key in sorted_keys:
                song = block_data["song"][song_key]
                song_event = song.get("event")

                # Skip current and next song, only include songs that come after current
                if (
                    song_event not in (current_event, next_event)
                    and int(song.get("elapsed", 0)) > current_elapsed
                ):
                    artist_name = song.get("artist", "")
                    if artist_name and artist_name not in seen_artists:
                        seen_artists.add(artist_name)
                        later_artists.append(artist_name)
                        if len(later_artists) >= 4:  # Limit to 4 artists
                            break

            if later_artists:
                artists_list = ", ".join(later_artists)
                enhanced_title += f" | Later: {artists_list}"

        return enhanced_title
