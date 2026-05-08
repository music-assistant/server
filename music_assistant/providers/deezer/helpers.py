"""Shared helper functions for the Deezer provider.

Utility functions used across multiple modules (parsers, browse, media, streaming).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from music_assistant_models.enums import ImageType, MediaType
from music_assistant_models.media_items import (
    MediaItemImage,
    MediaItemMetadata,
    Playlist,
    ProviderMapping,
    UniqueList,
)

if TYPE_CHECKING:
    from .provider import DeezerProvider

# -- Virtual playlist IDs --

FLOW_PLAYLIST_ID = "flow"
FLOW_CONFIG_PREFIX = "flow_config_"
SMART_TRACKLIST_PREFIX = "smart_tracklist_"
RECOMMENDED_TRACKS_PLAYLIST_ID = "recommended_tracks"
TOP_CHARTS_PLAYLIST_ID = "top_charts"
USER_TOP_TRACKS_PLAYLIST_ID = "user_top_tracks"
SHAKER_PREFIX = "shaker_"
SHAKER_CURATED_PREFIX = "shaker_curated_"
PERSONAL_SONGS_PLAYLIST_ID = "personal_songs"
SHAKER_MIX_COVER = "https://cdn-assets.dzcdn.net/shaker/_next/static/media/group_mix.d986951b.svg"


@dataclass(frozen=True)
class VirtualPlaylistMeta:
    """Canonical metadata for a virtual playlist type."""

    name: str
    is_dynamic: bool = False


# Registry of virtual playlist types with their canonical name and is_dynamic flag.
# Keyed by exact playlist ID for fixed IDs, and by prefix for parameterized IDs.
VIRTUAL_PLAYLIST_TYPES: dict[str, VirtualPlaylistMeta] = {
    FLOW_PLAYLIST_ID: VirtualPlaylistMeta("Flow", is_dynamic=True),
    FLOW_CONFIG_PREFIX: VirtualPlaylistMeta("Flow", is_dynamic=True),
    SMART_TRACKLIST_PREFIX: VirtualPlaylistMeta("Mix"),
    RECOMMENDED_TRACKS_PLAYLIST_ID: VirtualPlaylistMeta("Hot Tracks"),
    TOP_CHARTS_PLAYLIST_ID: VirtualPlaylistMeta("Top Charts"),
    USER_TOP_TRACKS_PLAYLIST_ID: VirtualPlaylistMeta("Your Top Tracks"),
    PERSONAL_SONGS_PLAYLIST_ID: VirtualPlaylistMeta("My Uploads"),
    SHAKER_PREFIX: VirtualPlaylistMeta("Mix", is_dynamic=True),
    SHAKER_CURATED_PREFIX: VirtualPlaylistMeta("Playlist"),
}


def get_virtual_playlist_meta(item_id: str) -> VirtualPlaylistMeta | None:
    """Look up canonical metadata for a virtual playlist by its item_id.

    Tries exact match first, then prefix match.
    """
    if item_id in VIRTUAL_PLAYLIST_TYPES:
        return VIRTUAL_PLAYLIST_TYPES[item_id]
    for prefix, meta in VIRTUAL_PLAYLIST_TYPES.items():
        if prefix.endswith("_") and item_id.startswith(prefix):
            return meta
    return None


def create_virtual_playlist(
    provider: DeezerProvider,
    item_id: str,
    name: str,
    image_url: str | None = None,
    is_dynamic: bool | None = None,
) -> Playlist:
    """Create a virtual playlist for Flow, recommended content, etc.

    :param provider: The Deezer provider instance.
    :param item_id: The unique identifier (e.g., "flow", "smart_tracklist_123").
    :param name: Display name for the playlist.
    :param image_url: Optional cover image URL.
    :param is_dynamic: Whether the playlist returns fresh tracks on each fetch.
        If None, the value is looked up from the virtual playlist registry.
    """
    if is_dynamic is None:
        meta = get_virtual_playlist_meta(item_id)
        is_dynamic = meta.is_dynamic if meta else False
    images: UniqueList[MediaItemImage] = UniqueList()
    if image_url:
        images.append(
            MediaItemImage(
                type=ImageType.THUMB,
                path=image_url,
                provider=provider.instance_id,
                remotely_accessible=True,
            )
        )
    return Playlist(
        item_id=item_id,
        provider=provider.instance_id,
        name=name,
        media_type=MediaType.PLAYLIST,
        provider_mappings={
            ProviderMapping(
                item_id=item_id,
                provider_domain=provider.domain,
                provider_instance=provider.instance_id,
            )
        },
        metadata=MediaItemMetadata(images=images) if images else MediaItemMetadata(),
        is_editable=False,
        is_dynamic=is_dynamic,
        owner="Deezer",
    )
