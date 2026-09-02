"""Helper functions for Radio Paradise provider."""

import time
from typing import Any

from music_assistant.helpers.compare import compare_strings


def get_current_block_position(block_data: dict[str, Any]) -> int:
    """
    Calculate current playback position within a Radio Paradise block.

    :param block_data: Block data containing sched_time_millis.
    """
    current_time_ms = int(time.time() * 1000)
    sched_time = int(block_data.get("sched_time_millis", current_time_ms))
    return current_time_ms - sched_time


def find_current_song(
    songs: dict[str, dict[str, Any]], current_time_ms: int
) -> dict[str, Any] | None:
    """
    Find which song should currently be playing based on elapsed time.

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


def find_song_by_stream_title(
    songs: dict[str, dict[str, Any]], stream_title: str
) -> dict[str, Any] | None:
    """
    Find the block song matching an in-band ICY stream title.

    Matches by composing "artist - title" from the block's structured fields;
    the ICY string is never split, which would be ambiguous around " - ".

    :param songs: Dictionary of songs from Radio Paradise block data.
    :param stream_title: Cleaned in-band stream title.
    """
    if not stream_title.strip():
        return None
    for song in songs.values():
        artist = song.get("artist") or ""
        title = song.get("title") or ""
        if not artist or not title:
            continue
        if compare_strings(f"{artist} - {title}", stream_title, strict=False):
            return song
    return None


def get_next_song(songs: dict[str, Any], current_song: dict[str, Any]) -> dict[str, Any] | None:
    """
    Get the next song that will play after the current song.

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
