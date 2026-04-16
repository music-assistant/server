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


def _build_upcoming_string(metadata: dict[str, Any], current_song: dict[str, Any]) -> str | None:
    """Build "Up Next: Artist - Track ● Later: Artist2, Artist3" string.

    :param metadata: Full metadata response with next song and block data.
    :param current_song: Current track data to exclude from upcoming list.
    """
    next_song = metadata.get("next")
    if not next_song:
        return None

    next_artist = next_song.get("artist", "")
    next_title = next_song.get("title", "")
    if not next_artist or not next_title:
        return None

    result = f"Up Next: {next_artist} - {next_title}"

    # Get additional artists from block data for "Later" section
    block_data = metadata.get("block_data")
    if block_data and "song" in block_data:
        current_event = current_song.get("event")
        next_event = next_song.get("event")
        current_elapsed = int(current_song.get("elapsed", 0))

        # Collect unique artists that come after current and next song
        seen_artists = {next_artist}
        later_artists = []

        sorted_keys = sorted(block_data["song"].keys(), key=int)
        for song_key in sorted_keys:
            song = block_data["song"][song_key]
            song_event = song.get("event")

            # Skip current and next song, only include songs after current
            if (
                song_event not in (current_event, next_event)
                and int(song.get("elapsed", 0)) > current_elapsed
            ):
                artist_name = song.get("artist", "")
                if artist_name and artist_name not in seen_artists:
                    seen_artists.add(artist_name)
                    later_artists.append(artist_name)
                    if len(later_artists) >= 3:
                        break

        if later_artists:
            result += f" ● Later: {', '.join(later_artists)}"

    return result


def build_stream_metadata(
    current_song: dict[str, Any],
    metadata: dict[str, Any],
    *,
    show_upcoming: bool = False,
) -> StreamMetadata:
    """Build StreamMetadata with current track info.

    :param current_song: Current track data from Radio Paradise API.
    :param metadata: Full metadata response with next song and block data.
    :param show_upcoming: If True, show upcoming info in artist field.
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

    # Alternate artist field with upcoming info
    artist_display = artist
    if show_upcoming:
        upcoming = _build_upcoming_string(metadata, current_song)
        if upcoming:
            artist_display = upcoming

    # Get cover image URL
    # Play API returns relative path (e.g., "covers/l/19806.jpg")
    # Now playing API returns full URL (e.g., "https://img.radioparadise.com/covers/l/19806.jpg")
    cover = current_song.get("cover")
    image_url = None
    if cover:
        image_url = cover if cover.startswith("http") else f"https://img.radioparadise.com/{cover}"

    # Get track duration (API returns milliseconds, convert to seconds)
    duration = current_song.get("duration")
    if duration:
        duration = int(duration) // 1000

    return StreamMetadata(
        title=title,
        artist=artist_display,
        album=album_display,
        image_url=image_url,
        duration=duration,
    )
