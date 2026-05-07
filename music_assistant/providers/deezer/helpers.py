"""Shared helper functions for the Deezer provider.

Utility functions used across multiple modules (parsers, browse, media, streaming).
"""

from __future__ import annotations

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


def create_virtual_playlist(
    provider: DeezerProvider,
    item_id: str,
    name: str,
    image_url: str | None = None,
    is_dynamic: bool = False,
) -> Playlist:
    """Create a virtual playlist for Flow, recommended content, etc.

    :param provider: The Deezer provider instance.
    :param item_id: The unique identifier (e.g., "flow", "smart_tracklist_123").
    :param name: Display name for the playlist.
    :param image_url: Optional cover image URL.
    :param is_dynamic: Whether the playlist returns fresh tracks on each fetch.
    """
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
