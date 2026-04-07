"""Parsers for Mother Earth Radio provider."""

from typing import Any

from music_assistant_models.enums import ImageType
from music_assistant_models.media_items import (
    MediaItemImage,
    ProviderMapping,
    Radio,
)
from music_assistant_models.streamdetails import StreamMetadata

from .constants import MER_CHANNELS, STATION_ICON_URL, MerChannel


def parse_radio(channel_id: str, instance_id: str, provider_domain: str) -> Radio:
    """
    Create a Radio object from channel configuration.

    :param channel_id: Key into MER_CHANNELS (e.g. "motherearth_jazz").
    :param instance_id: Provider instance identifier.
    :param provider_domain: Provider domain string.
    """
    channel_info: MerChannel = MER_CHANNELS[channel_id]

    radio = Radio(
        provider=instance_id,
        item_id=channel_id,
        name=channel_info["name"],
        provider_mappings={
            ProviderMapping(
                provider_domain=provider_domain,
                provider_instance=instance_id,
                item_id=channel_id,
                available=True,
            )
        },
    )

    radio.metadata.add_image(
        MediaItemImage(
            provider=instance_id,
            type=ImageType.THUMB,
            path=STATION_ICON_URL,
            remotely_accessible=True,
        )
    )

    return radio


def _build_source_label(custom_fields: dict[str, Any] | None) -> str | None:
    """
    Build a compact source label from AzuraCast custom_fields.

    Returns strings like "Vinyl · 1998 · !K7" or "Hi-Res Download · 2024".

    :param custom_fields: The custom_fields dict from the AzuraCast song object.
    """
    if not custom_fields:
        return None

    parts: list[str] = []
    source = custom_fields.get("source")
    if source:
        parts.append(source)
    year = custom_fields.get("year")
    if year:
        parts.append(str(year))
    label = custom_fields.get("label")
    if label:
        parts.append(label)

    return " · ".join(parts) if parts else None


def build_stream_metadata(
    now_playing: dict[str, Any],
    playing_next: dict[str, Any] | None = None,
    *,
    show_upcoming: bool = False,
) -> StreamMetadata:
    """
    Build StreamMetadata from AzuraCast now_playing data.

    :param now_playing: The now_playing object from the AzuraCast API.
    :param playing_next: The playing_next object (may be None).
    :param show_upcoming: If True, show upcoming track info in the artist field.
    """
    song = now_playing.get("song", {})

    artist = song.get("artist", "Unknown Artist")
    title = song.get("title", "Unknown Title")
    album = song.get("album")

    # Enrich album display with source info from custom_fields
    custom_fields = song.get("custom_fields")
    source_label = _build_source_label(custom_fields)
    album_display = album
    if album and source_label:
        album_display = f"{album} [{source_label}]"
    elif source_label:
        album_display = f"[{source_label}]"

    # Alternate artist field with upcoming info
    artist_display = artist
    if show_upcoming and playing_next:
        next_song = playing_next.get("song", {})
        next_artist = next_song.get("artist", "")
        next_title = next_song.get("title", "")
        if next_artist and next_title:
            artist_display = f"Up Next: {next_artist} - {next_title}"

    # Cover art URL from AzuraCast
    image_url = song.get("art")

    # Duration in seconds (AzuraCast already provides seconds)
    duration = now_playing.get("duration")
    if duration:
        duration = int(duration)

    return StreamMetadata(
        title=title,
        artist=artist_display,
        album=album_display,
        image_url=image_url,
        duration=duration,
    )
