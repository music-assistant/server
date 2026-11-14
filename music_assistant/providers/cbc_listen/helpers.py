"""Helper functions for the CBC Listen provider."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.streamdetails import StreamMetadata

from .constants import DEFAULT_IMAGE_URL, IMAGE_RATIO, IMAGE_WIDTH, MAX_TRACK_AGE_MS, NETWORK_LOGOS

if TYPE_CHECKING:
    from collections.abc import Iterable

    from .models import CBCProgram, CBCShow, CBCTrack, Channel


def format_image_url(url: str) -> str:
    """
    Format a CBC image URL template with concrete dimensions.

    :param url: Raw image URL or template from the CBC Listen API.
    """
    return url.replace("${width}", IMAGE_WIDTH).replace("${ratio}", IMAGE_RATIO)


def search_channels(channels: Iterable[Channel], query: str, limit: int) -> list[Channel]:
    """
    Return the radio channels matching every word of the query, ignoring case.

    :param channels: The channels to search.
    :param query: Search query string.
    :param limit: Maximum number of channels to return.
    """
    words = query.casefold().split()
    if not words:
        return []
    # Station name matches are ranked ahead of province or call-sign matches.
    primary: list[Channel] = []
    secondary: list[Channel] = []
    for channel in channels:
        name = channel.name.casefold()
        if all(word in name for word in words):
            primary.append(channel)
        elif all(
            word in f"{name} {channel.province} {channel.call_sign}".casefold() for word in words
        ):
            secondary.append(channel)
    return (primary + secondary)[:limit]


def search_shows(shows: Iterable[CBCShow], query: str, limit: int) -> list[CBCShow]:
    """
    Return the podcasts and radio shows matching every word of the query.

    :param shows: The shows to search.
    :param query: Search query string.
    :param limit: Maximum number of shows to return.
    """
    # Title and host matches are ranked ahead of description-only matches.
    words = query.casefold().split()
    if not words:
        return []
    primary: list[CBCShow] = []
    secondary: list[CBCShow] = []
    for show in shows:
        if not show.has_audio:
            continue
        header = f"{show.title} {show.hosts}".casefold()
        if all(word in header for word in words):
            primary.append(show)
        elif all(word in f"{header} {show.description.casefold()}" for word in words):
            secondary.append(show)
    return (primary + secondary)[:limit]


def find_program(programs: Iterable[CBCProgram], at_ms: int) -> CBCProgram | None:
    """
    Return the program airing at the given epoch timestamp in milliseconds.

    :param programs: The station's program queue entries.
    :param at_ms: Current UTC time in epoch milliseconds.
    """
    return next(
        (program for program in programs if program.epoch_start <= at_ms < program.epoch_end),
        None,
    )


def find_track(tracks: Iterable[CBCTrack], at_ms: int) -> CBCTrack | None:
    """
    Return the most recent track broadcasted at or before ``at_ms``.

    :param tracks: Playlog tracks for the current show.
    :param at_ms: Current UTC time in epoch milliseconds.
    """
    cutoff = at_ms - MAX_TRACK_AGE_MS
    candidates = [track for track in tracks if cutoff <= track.broadcasted_time <= at_ms]
    return max(candidates, key=lambda track: track.broadcasted_time, default=None)


def get_stream_metadata(
    channel: Channel,
    programs: Iterable[CBCProgram],
    tracks: Iterable[CBCTrack],
    at_ms: int,
) -> StreamMetadata:
    """
    Build live stream metadata from the current program and playlog tracks.

    :param channel: The playing radio channel.
    :param programs: The station's program queue entries.
    :param tracks: Playlog tracks for the current show, if any.
    :param at_ms: Current UTC time in epoch milliseconds.
    """
    program = find_program(programs, at_ms)
    image_url = (format_image_url(program.program_image) if program else None) or NETWORK_LOGOS.get(
        channel.network_id, DEFAULT_IMAGE_URL
    )
    if track := find_track(tracks, at_ms):
        artist = track.artists.strip() or track.composers.strip() or None
        album = track.album.strip() or (program.program_title.strip() if program else None) or None
        return StreamMetadata(
            title=track.title.strip(),
            artist=artist,
            album=album,
            image_url=image_url,
        )
    if program and program.program_title.strip():
        return StreamMetadata(
            title=program.program_title.strip(),
            artist=program.host_name.strip() or None,
            image_url=image_url,
        )
    return StreamMetadata(title=channel.name, image_url=image_url)
