"""Streaming-related helpers for the Jellyfin provider.

Extracts streaming and similar-tracks logic from the provider so the main
provider file can remain a thin façade delegating to these helpers.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from music_assistant_models.enums import MediaType, StreamType
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.providers.jellyfin.parsers import audio_format, parse_track

from .const import (
    ITEM_KEY_ID,
    ITEM_KEY_RUNTIME_TICKS,
    SUPPORTED_CONTAINER_FORMATS,
    TRACK_FIELDS,
)

if TYPE_CHECKING:
    from aiojellyfin import Connection
    from music_assistant_models.media_items import Track


class JellyfinStreaming:
    """Helper class for Jellyfin streaming operations."""

    def __init__(self, client: Connection, logger: logging.Logger, instance_id: str) -> None:
        """Initialize JellyfinStreaming helper."""
        self._client = client
        self._logger = logger
        self._instance_id = instance_id

    async def get_stream_details(self, item_id: str, _media_type: MediaType) -> StreamDetails:
        """Get stream details for a given item ID."""
        jellyfin_track = await self._client.get_track(item_id)
        url = self._client.audio_url(
            jellyfin_track[ITEM_KEY_ID], container=SUPPORTED_CONTAINER_FORMATS
        )
        return StreamDetails(
            item_id=jellyfin_track[ITEM_KEY_ID],
            provider=self._instance_id,
            audio_format=audio_format(jellyfin_track),
            stream_type=StreamType.HTTP,
            duration=int(jellyfin_track[ITEM_KEY_RUNTIME_TICKS] / 10000000),
            path=url,
            can_seek=True,
            allow_seek=True,
        )

    async def get_similar_tracks(self, prov_track_id: str, limit: int = 25) -> list[Track]:
        """Get similar tracks for a given track ID."""
        resp = await self._client.get_similar_tracks(
            prov_track_id, limit=limit, fields=TRACK_FIELDS
        )
        return [
            parse_track(self._logger, self._instance_id, self._client, track)
            for track in resp["Items"]
        ]
