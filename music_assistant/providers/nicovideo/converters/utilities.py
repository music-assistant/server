"""
Base utilities for nicovideo converters.

Common functions and utilities used across multiple converter modules.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.media_items import AudioFormat, ProviderMapping

if TYPE_CHECKING:
    from music_assistant.models.music_provider import MusicProvider


def calculate_popularity(
    mylist_count: int | None = None,
    like_count: int | None = None,
) -> int:
    """Calculate popularity score using standard formula.

    Args:
        mylist_count: Number of mylists.
        like_count: Number of likes.

    Returns:
        Popularity score (0-100).
    """
    # Primary calculation: mylist*3 + like*1 (normalized to 0-100 scale)
    if mylist_count is not None and like_count is not None:
        return min(100, max(0, int((mylist_count * 3 + like_count) / 10)))

    return 0


def create_provider_mapping(
    *,
    item_id: str,
    provider: MusicProvider,
    available: bool = True,
    url_path: str | None = None,
    audio_format: AudioFormat | None = None,
) -> set[ProviderMapping]:
    """Create provider mapping for media items.

    Args:
        item_id: Item ID.
        provider: Music provider instance.
        available: Whether the item is available.
        url_path: Custom URL path (e.g., 'watch', 'mylist', 'series', 'user').
                 If None, defaults to 'watch' for backward compatibility.
        audio_format: Optional AudioFormat for streamable content.

    Returns:
        Set of ProviderMapping objects.
    """
    if url_path is None:
        url_path = "watch"

    # Create mapping with required fields
    mapping = ProviderMapping(
        item_id=item_id,
        provider_domain=provider.domain,
        provider_instance=provider.instance_id,
        url=f"https://www.nicovideo.jp/{url_path}/{item_id}",
        available=available,
    )

    # Set audio_format if provided
    if audio_format is not None:
        mapping.audio_format = audio_format

    return {mapping}
