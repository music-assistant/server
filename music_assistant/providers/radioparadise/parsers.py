"""Parsers for Radio Paradise provider."""

from typing import Any

from music_assistant_models.enums import ImageType
from music_assistant_models.media_items import (
    MediaItemImage,
    ProviderMapping,
    Radio,
)
from music_assistant_models.streamdetails import StreamMetadata

from .constants import RADIO_PARADISE_CHANNELS, STATION_ICONS_BASE_URL
from .helpers import enhance_title_with_upcoming


def parse_radio(
    channel_id: str, provider_lookup_key: str, provider_domain: str, instance_id: str
) -> Radio:
    """Create a Radio object from cached channel information."""
    channel_info = RADIO_PARADISE_CHANNELS.get(channel_id, {})

    radio = Radio(
        provider=provider_lookup_key,
        item_id=channel_id,
        name=channel_info.get("name", "Unknown Radio"),
        provider_mappings={
            ProviderMapping(
                provider_domain=provider_domain,
                provider_instance=instance_id,
                item_id=channel_id,
                available=True,
            )
        },
    )

    # Add static station icon
    station_icon = channel_info.get("station_icon")
    if station_icon:
        icon_url = f"{STATION_ICONS_BASE_URL}/{station_icon}"
        radio.metadata.add_image(
            MediaItemImage(
                provider=provider_lookup_key,
                type=ImageType.THUMB,
                path=icon_url,
                remotely_accessible=True,
            )
        )

    return radio


def build_stream_metadata(current_song: dict[str, Any], metadata: dict[str, Any]) -> StreamMetadata:
    """Build StreamMetadata with current track info and upcoming tracks.

    Args:
        current_song: Current track data from Radio Paradise API
        metadata: Full metadata response with next song and block data

    Returns:
        StreamMetadata with track info and upcoming track previews
    """
    # Extract track info
    artist = current_song.get("artist", "Unknown Artist")
    title = current_song.get("title", "Unknown Title")
    album = current_song.get("album")
    year = current_song.get("year")

    # Build album string with year if available
    album_display = album
    if album and year:
        album_display = f"{album} ({year})"
    elif year:
        album_display = str(year)

    # Get cover image URL
    cover_path = current_song.get("cover")
    image_url = None
    if cover_path:
        image_url = f"https://img.radioparadise.com/{cover_path}"

    # Get track duration
    duration = current_song.get("duration")
    if duration:
        duration = int(duration) // 1000  # Convert from ms to seconds

    # Add upcoming tracks info to title for scrolling display
    next_song = metadata.get("next")
    block_data = metadata.get("block_data")
    enhanced_title = enhance_title_with_upcoming(title, current_song, next_song, block_data)

    return StreamMetadata(
        title=enhanced_title,
        artist=artist,
        album=album_display,
        image_url=image_url,
        duration=duration,
    )
