"""
Helper utilities for nicovideo converters.

Provides common utility functions and lightweight mapping creation for converters.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from music_assistant_models.media_items import ProviderMapping

from music_assistant.providers.nicovideo.converters.base import NicovideoConverterBase

if TYPE_CHECKING:
    from music_assistant_models.media_items import AudioFormat

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
