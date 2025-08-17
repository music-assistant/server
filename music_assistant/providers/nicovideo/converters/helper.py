"""
Helper utilities for nicovideo converters.

Provides common utility functions and lightweight mapping creation for converters.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from music_assistant_models.enums import ImageType, MediaType
from music_assistant_models.media_items import (
    Artist,
    ItemMapping,
    MediaItemImage,
    ProviderMapping,
)
from music_assistant_models.unique_list import UniqueList
from niconico.objects.user import NicoUser

from music_assistant.providers.nicovideo.converters.base import NicovideoConverterBase

if TYPE_CHECKING:
    from music_assistant_models.media_items import AudioFormat
    from niconico.objects.video import Owner

# Type alias for nicovideo URL path types
type NicovideoUrlPath = Literal["watch", "mylist", "series", "user", "channel"]


class NicovideoConverterHelper(NicovideoConverterBase):
    """Helper for creating various mapping objects and utility functions."""

    def calculate_popularity(
        self,
        mylist_count: int | None = None,
        like_count: int | None = None,
    ) -> int:
        """Calculate popularity score using standard formula.

        Returns:
            Popularity score (0-100).
        """
        # Primary calculation: mylist*3 + like*1 (normalized to 0-100 scale)
        if mylist_count is not None and like_count is not None:
            return min(100, max(0, int((mylist_count * 3 + like_count) / 10)))

        return 0

    # ItemMapping creation methods
    def create_album_mapping(
        self, album_id: str, album_name: str, *, thumbnail_url: str | None = None
    ) -> ItemMapping:
        """Create an ItemMapping for album references without full metadata."""
        item_mapping = ItemMapping(
            media_type=MediaType.ALBUM,
            item_id=album_id,
            provider=self.provider.lookup_key,
            name=album_name,
        )

        # Add image if available (exclude default no-thumbnail image)
        if thumbnail_url and not thumbnail_url.endswith("/series/no_thumbnail.png"):
            item_mapping.image = MediaItemImage(
                type=ImageType.THUMB,
                path=thumbnail_url,
                provider=self.provider.lookup_key,
                remotely_accessible=True,
            )

        return item_mapping

    def create_artist_mapping(
        self, owner_or_user: Owner | NicoUser
    ) -> UniqueList[Artist | ItemMapping]:
        """Create an ItemMapping for artist references without full metadata."""
        # Handle different object types for ID and name
        icon_url: str | None
        if isinstance(owner_or_user, NicoUser):
            item_id = str(owner_or_user.id_)
            name = owner_or_user.nickname
            icon_url = owner_or_user.icons.large
        else:  # Owner
            # Skip Owner objects without valid ID to avoid AssertionError
            if not owner_or_user.id_:
                return UniqueList()
            item_id = str(owner_or_user.id_)
            name = owner_or_user.name if owner_or_user.name else ""
            icon_url = owner_or_user.icon_url

        # Create the ItemMapping
        item_mapping = ItemMapping(
            media_type=MediaType.ARTIST,
            item_id=item_id,
            provider=self.provider.lookup_key,
            name=name,
        )

        # Add image if available
        if icon_url:
            item_mapping.image = MediaItemImage(
                type=ImageType.THUMB,
                path=icon_url,
                provider=self.provider.lookup_key,
                remotely_accessible=True,
            )

        return UniqueList[Artist | ItemMapping]([item_mapping])

    # ProviderMapping creation methods
    def create_provider_mapping(
        self,
        item_id: str,
        url_path: NicovideoUrlPath,
        *,
        available: bool = True,
        audio_format: AudioFormat | None = None,
    ) -> set[ProviderMapping]:
        """Create provider mapping for media items."""
        # Create mapping with required fields
        mapping = ProviderMapping(
            item_id=item_id,
            provider_domain=self.provider.domain,
            provider_instance=self.provider.instance_id,
            url=f"https://www.nicovideo.jp/{url_path}/{item_id}",
            available=available,
        )

        # Set audio_format if provided
        if audio_format is not None:
            mapping.audio_format = audio_format

        return {mapping}
