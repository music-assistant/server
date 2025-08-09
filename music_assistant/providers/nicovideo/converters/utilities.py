"""
Base utilities for nicovideo converters.

Common functions and utilities used across multiple converter modules.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.enums import ContentType
from music_assistant_models.media_items import AudioFormat, ProviderMapping

if TYPE_CHECKING:
    from niconico.objects.video.watch import WatchData

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


def create_audio_format_from_watch_data(watch_data: WatchData) -> AudioFormat | None:
    """Create AudioFormat from WatchData audio information.

    Args:
        watch_data: WatchData object containing media information.

    Returns:
        AudioFormat object if audio information is available, None otherwise.
    """
    if not watch_data.media or not watch_data.media.domand or not watch_data.media.domand.audios:
        return None

    # Use the first available audio stream (typically the highest quality)
    audio = watch_data.media.domand.audios[0]

    if not audio.is_available:
        return None

    # Determine channels - niconico videos are typically stereo (2 channels)
    # Since niconico doesn't explicitly provide channel info, we assume stereo
    channels = 2

    # Determine bit depth - niconico typically uses 16-bit audio
    # Since this info isn't available, we use a reasonable default
    bit_depth = 16

    return AudioFormat(
        content_type=ContentType.MP4,  # niconico primarily uses MP4
        codec_type=ContentType.AAC,
        sample_rate=audio.sampling_rate,
        bit_depth=bit_depth,
        channels=channels,
        bit_rate=audio.bit_rate,
        output_format_str=(
            f"AAC {audio.sampling_rate // 1000}kHz/{bit_depth}bit/{channels}ch {audio.bit_rate}kbps"
        ),
    )


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
