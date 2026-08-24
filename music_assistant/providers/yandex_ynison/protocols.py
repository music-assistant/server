"""
Protocol definitions for provider dependencies.

Allows typing of external provider references without importing concrete classes.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from contextlib import AbstractAsyncContextManager

    from music_assistant_models.enums import MediaType
    from music_assistant_models.streamdetails import StreamDetails


@runtime_checkable
class YandexMusicProviderLike(Protocol):
    """
    Structural interface for the yandex_music MusicProvider.

    Only the subset of methods/properties used by the Ynison plugin.
    """

    @property
    def instance_id(self) -> str:
        """Return the exact linked provider instance ID."""
        ...

    @property
    def available(self) -> bool:
        """Return whether the linked provider instance is available."""
        ...

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Resolve stream details for a track."""
        ...

    def get_audio_stream(self, stream_details: StreamDetails) -> AsyncGenerator[bytes]:
        """Return async generator of raw audio bytes."""
        ...

    def acquire_stream_slot(self, wait_timeout: float | None) -> AbstractAsyncContextManager[None]:
        """Acquire one upstream source-stream slot."""
        ...

    async def get_rotor_station_tracks(
        self, station_id: str, queue: str | int | None = None
    ) -> tuple[list[Any], str | None]:
        """Fetch tracks from a rotor station for radio queue replenishment."""
        ...
