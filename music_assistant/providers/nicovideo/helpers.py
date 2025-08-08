"""Utility functions for handling cookies and converting them into Netscape format."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, cast

from music_assistant_models.media_items import Track as TrackModel

from music_assistant.constants import (
    CACHE_CATEGORY_LIBRARY_ITEMS,
    CACHE_CATEGORY_MUSIC_PROVIDER_ITEM,
    VERBOSE_LOG_LEVEL,
)
from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    import logging

    from music_assistant_models.media_items import Album, Playlist, Track
    from requests.cookies import RequestsCookieJar


class PlaylistWithTracks:
    """Helper class to hold playlist and its tracks."""

    def __init__(self, playlist: Playlist, tracks: list[Track]) -> None:
        """Initialize with playlist and its tracks."""
        self.playlist = playlist
        self.tracks = tracks


class AlbumWithTracks:
    """Helper class to hold album and its tracks."""

    def __init__(self, album: Album, tracks: list[Track]) -> None:
        """Initialize with album and its tracks."""
        self.album = album
        self.tracks = tracks


def convert_to_netscape(cookie: RequestsCookieJar, domain: str) -> str:
    """Convert a raw cookie into Netscape format for yt-dlp."""
    domain = domain.removeprefix("https://").removeprefix("http://")
    netscape_cookie = "# Netscape HTTP Cookie File\n"
    for morsel in iter(cookie):
        netscape_cookie += (
            f"{domain}\tTRUE\t/\t"
            f"{str(getattr(morsel, 'secure', False)).upper()}\t0\t"
            f"{getattr(morsel, 'name', '')}\t{getattr(morsel, 'value', '')}\n"
        )
    return netscape_cookie


async def get_library_items[T](
    provider: MusicProvider,
    cache_key: str,
    query_table: str,
    query_method: Callable[..., Awaitable[list[T]]],
) -> list[T]:
    """Get library items from cache or query method."""
    library_item_ids = await provider.mass.cache.get(
        cache_key,
        category=CACHE_CATEGORY_LIBRARY_ITEMS,
        base_key=provider.instance_id,
    )
    if not library_item_ids:
        return []
    library_item_ids = cast("list[int]", library_item_ids)
    query = f"{query_table}.item_id in :ids"
    query_params = {"ids": library_item_ids}
    return await query_method(extra_query=query, extra_query_params=query_params)


async def cache_track(provider: MusicProvider, track: Track) -> None:
    """Cache single track with provider item cache."""
    cache_key = f"track.{track.item_id}"
    await provider.mass.cache.set(
        cache_key,
        track.to_dict(),
        category=CACHE_CATEGORY_MUSIC_PROVIDER_ITEM,
        base_key=provider.lookup_key,
    )


async def get_cached_track(provider: MusicProvider, track_id: str) -> Track | None:
    """Get track from cache or return None if not found."""
    cache_key = f"track.{track_id}"
    cached_track_data = await provider.mass.cache.get(
        cache_key,
        category=CACHE_CATEGORY_MUSIC_PROVIDER_ITEM,
        base_key=provider.lookup_key,
    )

    if cached_track_data:
        return TrackModel.from_dict(cached_track_data)

    return None


def log_verbose(logger: logging.Logger, message: str, *args: object) -> None:
    """Log a message at VERBOSE level with performance optimization.

    Args:
        logger: Logger instance
        message: Log message format string
        *args: Arguments for the message format string
    """
    if logger.isEnabledFor(VERBOSE_LOG_LEVEL):
        logger.log(VERBOSE_LOG_LEVEL, message, *args)


def log_verbose_operation(
    logger: logging.Logger, operation: str, item_id: str, **details: object
) -> None:
    """Log verbose information about an operation with structured details.

    Args:
        logger: Logger instance
        operation: Operation name (e.g., "cached_tags", "auth_attempt")
        item_id: Item identifier
        **details: Additional details to include in the log
    """
    if logger.isEnabledFor(VERBOSE_LOG_LEVEL):
        detail_parts = [f"{k}={v}" for k, v in details.items()]
        detail_str = f" ({', '.join(detail_parts)})" if detail_parts else ""
        logger.log(VERBOSE_LOG_LEVEL, "%s for %s%s", operation, item_id, detail_str)
