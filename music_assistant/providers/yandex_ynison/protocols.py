"""Protocol definitions for provider dependencies.

Allows typing of external provider references without importing concrete classes.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from music_assistant_models.enums import MediaType
    from music_assistant_models.streamdetails import StreamDetails


@runtime_checkable
class YandexMusicProviderLike(Protocol):
    """Structural interface for the yandex_music MusicProvider.

    Only the subset of methods/properties used by the Ynison plugin.
    """

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Resolve stream details for a track."""
        ...

    def get_audio_stream(self, stream_details: StreamDetails) -> AsyncGenerator[bytes, None]:
        """Return async generator of raw audio bytes."""
        ...

    async def get_rotor_station_tracks(
        self, station_id: str, queue: str | int | None = None
    ) -> tuple[list[Any], str | None]:
        """Fetch tracks from a rotor station for radio queue replenishment."""
        ...
