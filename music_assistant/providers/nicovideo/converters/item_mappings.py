"""
Item mapping utilities for nicovideo converter.

Functions to create lightweight ItemMapping references without full metadata.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import ItemMapping
from niconico.objects.user import NicoUser

if TYPE_CHECKING:
    from niconico.objects.video import Owner

from music_assistant.providers.nicovideo.converters.base import NicovideoConverterBase


class ItemMappingConverter(NicovideoConverterBase):
    """Converter for creating ItemMapping references."""

    def get_album_mapping(self, album_id: str, album_name: str) -> ItemMapping:
        """Create an ItemMapping for album references without full metadata."""
        return ItemMapping(
            media_type=MediaType.ALBUM,
            item_id=album_id,
            provider=self.provider.lookup_key,
            name=album_name,
        )

    def get_artist_mapping(self, owner_or_user: Owner | NicoUser) -> ItemMapping:
        """Create an ItemMapping for artist references without full metadata."""
        # Handle different object types for ID and name
        if isinstance(owner_or_user, NicoUser):
            item_id = str(owner_or_user.id_)
            name = owner_or_user.nickname
        else:  # Owner
            item_id = str(owner_or_user.id_) if owner_or_user.id_ else ""
            name = owner_or_user.name if owner_or_user.name else ""

        return ItemMapping(
            media_type=MediaType.ARTIST,
            item_id=item_id,
            provider=self.provider.lookup_key,
            name=name,
        )
