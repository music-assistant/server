"""Shared state models for the DLNA Receiver provider."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from music_assistant_models.streamdetails import StreamMetadata

    from .renderer import UPnPRenderer
    from .ssdp import SSDPAdvertiser


@dataclass
class RendererInstance:
    """Store the renderer and playback state for one Music Assistant player."""

    player_id: str
    player_name: str
    renderer: UPnPRenderer
    ssdp: SSDPAdvertiser
    current_stream_url: str | None = None
    current_metadata: dict[str, str | None] | None = None
    stream_metadata: StreamMetadata | None = None
    play_start_time: float | None = None
    elapsed_offset: int = 0
    metadata_dirty: bool = False
    last_metadata_push: float = 0.0
