"""Helper functions for Radio Paradise provider."""

import time
from typing import Any


def get_current_block_position(block_data: dict[str, Any]) -> int:
    """Calculate current playback position within a Radio Paradise block.

    :param block_data: Block data containing sched_time_millis.
    """
    current_time_ms = int(time.time() * 1000)
    sched_time = int(block_data.get("sched_time_millis", current_time_ms))
    return current_time_ms - sched_time


def find_current_song(
    songs: dict[str, dict[str, Any]], current_time_ms: int
) -> dict[str, Any] | None:
    """Find which song should currently be playing based on elapsed time.

    :param songs: Dictionary of songs from Radio Paradise block data.
    :param current_time_ms: Current position in milliseconds within the block.
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
    return first_song if first_song is not None else None


def get_next_song(songs: dict[str, Any], current_song: dict[str, Any]) -> dict[str, Any] | None:
    """Get the next song that will play after the current song.

    :param songs: Dictionary of songs from Radio Paradise block data.
    :param current_song: The currently playing song dictionary.
    """
    current_event = current_song.get("event")
    current_elapsed = int(current_song.get("elapsed", 0))
    sorted_keys = sorted(songs.keys(), key=int)

    for song_key in sorted_keys:
        song: dict[str, Any] = songs[song_key]
        if song.get("event") != current_event and int(song.get("elapsed", 0)) > current_elapsed:
            return song
    return None
