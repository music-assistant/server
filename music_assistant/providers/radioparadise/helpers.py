"""Helper functions for Radio Paradise provider."""

import time
from typing import Any, cast

from .constants import RADIO_PARADISE_CHANNELS


def get_current_block_position(block_data: dict[str, Any]) -> int:
    """Calculate current playback position within a Radio Paradise block.

    Args:
        block_data: Block data containing sched_time_millis

    Returns:
        Current position in milliseconds from block start
    """
    current_time_ms = int(time.time() * 1000)
    sched_time = int(block_data.get("sched_time_millis", current_time_ms))
    return current_time_ms - sched_time


def find_current_song(
    songs: dict[str, dict[str, Any]], current_time_ms: int
) -> dict[str, Any] | None:
    """Find which song should currently be playing based on elapsed time.

    Args:
        songs: Dictionary of songs from Radio Paradise block data
        current_time_ms: Current position in milliseconds within the block

    Returns:
        The song dict that should be playing now, or None if not found
    """
    sorted_keys = sorted(songs.keys(), key=int)

    for song_key in sorted_keys:
        song = songs[song_key]
        song_start = int(song.get("elapsed", 0))
        song_duration = int(song.get("duration", 0))
        song_end = song_start + song_duration

        if song_start <= current_time_ms < song_end:
            return song

    # If no exact match, return first song
    first_song = songs.get("0")
    return first_song if first_song is not None else {}


def get_next_song(songs: dict[str, Any], current_song: dict[str, Any]) -> dict[str, Any] | None:
    """Get the next song that will play after the current song.

    Args:
        songs: Dictionary of songs from Radio Paradise block data
        current_song: The currently playing song dictionary

    Returns:
        The next song dict, or None if no next song found
    """
    current_event = current_song.get("event")
    current_elapsed = int(current_song.get("elapsed", 0))
    sorted_keys = sorted(songs.keys(), key=int)

    for song_key in sorted_keys:
        song = cast("dict[str, Any]", songs[song_key])
        if song.get("event") != current_event and int(song.get("elapsed", 0)) > current_elapsed:
            return song
    return None


def build_stream_url(channel_id: str) -> str:
    """Build the streaming URL for a Radio Paradise channel.

    Args:
        channel_id: Radio Paradise channel ID (0-5)

    Returns:
        Streaming URL for the channel, or empty string if not found
    """
    if channel_id not in RADIO_PARADISE_CHANNELS:
        return ""

    channel_info = RADIO_PARADISE_CHANNELS[channel_id]
    return str(channel_info.get("stream_url", ""))


def enhance_title_with_upcoming(
    title: str,
    current_song: dict[str, Any],
    next_song: dict[str, Any] | None,
    block_data: dict[str, Any] | None,
) -> str:
    """Enhance track title with upcoming track info for scrolling display.

    Args:
        title: Original track title
        current_song: Current track data
        next_song: Next track data, or None if not available
        block_data: Full block data with all upcoming tracks

    Returns:
        Enhanced title with "Up Next" and "Later" information appended
    """
    enhanced_title = title

    # Add next track info
    if next_song:
        next_artist = next_song.get("artist", "")
        next_title = next_song.get("title", "")
        if next_artist and next_title:
            enhanced_title += f" | Up Next: {next_artist} - {next_title}"

    # Add later artists in a single pass with deduplication
    if block_data and "song" in block_data:
        current_event = current_song.get("event")
        current_elapsed = int(current_song.get("elapsed", 0))
        next_event = next_song.get("event") if next_song else None

        # Use set to deduplicate artist names (including next_song artist)
        seen_artists = set()
        if next_song:
            next_artist = next_song.get("artist", "")
            if next_artist:
                seen_artists.add(next_artist)

        later_artists = []
        sorted_keys = sorted(block_data["song"].keys(), key=int)
        for song_key in sorted_keys:
            song = block_data["song"][song_key]
            song_event = song.get("event")

            # Skip current and next song, only include songs that come after current
            if (
                song_event not in (current_event, next_event)
                and int(song.get("elapsed", 0)) > current_elapsed
            ):
                artist_name = song.get("artist", "")
                if artist_name and artist_name not in seen_artists:
                    seen_artists.add(artist_name)
                    later_artists.append(artist_name)
                    if len(later_artists) >= 4:  # Limit to 4 artists
                        break

        if later_artists:
            artists_list = ", ".join(later_artists)
            enhanced_title += f" | Later: {artists_list}"

    return enhanced_title
