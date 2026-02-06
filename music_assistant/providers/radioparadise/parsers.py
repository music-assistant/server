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


def parse_radio(channel_id: str, instance_id: str, provider_domain: str) -> Radio:
    """Create a Radio object from cached channel information."""
    channel_info = RADIO_PARADISE_CHANNELS.get(channel_id, {})

    radio = Radio(
        provider=instance_id,
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
                provider=instance_id,
                type=ImageType.THUMB,
                path=icon_url,
                remotely_accessible=True,
            )
        )

    return radio


def build_stream_metadata(current_song: dict[str, Any]) -> StreamMetadata:
    """Build StreamMetadata with current track info.

    :param current_song: Current track data from Radio Paradise now_playing API.
    """
    # Extract track info from now_playing API response
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

    # Get cover image URL - now_playing API returns full URL
    image_url = current_song.get("cover")

    return StreamMetadata(
        title=title,
        artist=artist,
        album=album_display,
        image_url=image_url,
    )
