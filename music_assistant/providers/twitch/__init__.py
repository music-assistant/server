"""Twitch Audio music provider for Music Assistant."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Sequence
from typing import TYPE_CHECKING, Any

from music_assistant_models.enums import (
    ContentType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.media_items import (
    AudioFormat,
    BrowseFolder,
    MediaItemType,
    Radio,
    SearchResults,
)
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from music_assistant_models.config_entries import (
        ConfigEntry,
        ConfigValueType,
        ProviderConfig,
    )
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

SUPPORTED_FEATURES = {
    ProviderFeature.BROWSE,
    ProviderFeature.SEARCH,
    ProviderFeature.LIBRARY_RADIOS,
}

# Streamlink constants
STREAM_CHUNK_SIZE = 64 * 1024  # 64KB
MAX_CONSECUTIVE_RECONNECTS = 5
RECONNECT_DELAY = 0.5  # seconds
PREFERRED_QUALITIES = ("audio_only", "worst")


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return TwitchProvider(mass, manifest, config, SUPPORTED_FEATURES)


async def get_config_entries(
    mass: MusicAssistant,
    instance_id: str | None = None,
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """Return Config entries to setup this provider."""
    # ruff: noqa: ARG001
    # Step 3 will add OAuth config entries here
    return ()


class TwitchProvider(MusicProvider):
    """Provider implementation for Twitch audio streaming."""

    @property
    def is_streaming_provider(self) -> bool:
        """Return True if the provider is a streaming provider."""
        return True

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""
        # Step 6 will subscribe to QUEUE_UPDATED events here

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/close of the provider."""
        # Step 6 will clean up event subscriptions, timers, WebSocket here

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Get streamdetails for a Twitch channel."""
        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=AudioFormat(content_type=ContentType.UNKNOWN),
            media_type=MediaType.RADIO,
            stream_type=StreamType.CUSTOM,
            allow_seek=False,
            can_seek=False,
        )

    async def get_audio_stream(
        self, streamdetails: StreamDetails, seek_position: int = 0
    ) -> AsyncGenerator[bytes, None]:
        """Return the audio stream for a Twitch channel."""
        item_id = streamdetails.item_id
        reconnects = 0

        while True:
            streams = await asyncio.to_thread(self._resolve_streams, item_id)
            if not streams:
                return

            stream = self._select_quality(streams)
            if not stream:
                return

            fd = await asyncio.to_thread(stream.open)
            try:
                while True:
                    chunk = await asyncio.to_thread(fd.read, STREAM_CHUNK_SIZE)
                    if chunk:
                        reconnects = 0
                        yield chunk
                        continue

                    # Empty read — attempt reconnect
                    break
            finally:
                await asyncio.to_thread(fd.close)

            reconnects += 1
            if reconnects > MAX_CONSECUTIVE_RECONNECTS:
                return

            await asyncio.sleep(RECONNECT_DELAY)

    def _resolve_streams(self, channel: str) -> dict[str, Any] | None:
        """Resolve Streamlink streams for a channel. Blocking — call via to_thread."""
        from streamlink import Streamlink  # noqa: PLC0415

        try:
            session = Streamlink()
            # Step 3 will add streamlink_token auth header here
            streams = session.streams(f"https://twitch.tv/{channel}")
            return dict(streams) if streams else None
        except Exception:
            self.logger.exception("Failed to resolve streams for %s", channel)
            return None

    @staticmethod
    def _select_quality(streams: dict[str, Any]) -> Any | None:
        """Select preferred audio quality from available streams."""
        return next((streams[q] for q in PREFERRED_QUALITIES if q in streams), None)

    async def browse(self, path: str) -> Sequence[MediaItemType | BrowseFolder]:
        """Browse this provider's items."""
        # Step 4 will implement Live/Following browse folders
        return []

    async def get_library_radios(self) -> AsyncGenerator[Radio, None]:
        """Retrieve live followed channels as radio stations."""
        # Step 4 will implement this
        yield  # type: ignore[misc]

    async def search(
        self,
        search_query: str,
        media_types: list[MediaType],
        limit: int = 5,
    ) -> SearchResults:
        """Perform search on Twitch."""
        # Step 4 will implement this
        return SearchResults()
