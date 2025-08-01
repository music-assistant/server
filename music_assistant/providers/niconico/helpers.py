"""Utility functions for handling cookies and converting them into Netscape format."""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, TypeVar, cast

from music_assistant_models.errors import ProviderUnavailableError
from niconico.exceptions import LoginFailureError

from music_assistant.constants import (
    CACHE_CATEGORY_LIBRARY_ITEMS,
    CACHE_CATEGORY_MUSIC_PROVIDER_ITEM,
)
from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from music_assistant_models.media_items import Album, Playlist, Track
    from requests.cookies import RequestsCookieJar

T = TypeVar("T")


class ErrorState:
    """Error state wrapper that can be used as a boolean."""

    exception: Exception | None = None

    def __init__(self) -> None:
        """Initialize error state as False."""
        self.exception = None

    def set_error(self, exception: Exception) -> None:
        """Mark that an error occurred."""
        self.exception = exception

    def __bool__(self) -> bool:
        """Return True if an error occurred."""
        return self.exception is not None

    def raise_error(self) -> None:
        """Raise the stored exception if one exists, otherwise raise a default error."""
        raise self.exception or ProviderUnavailableError("No error set")


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


@asynccontextmanager
async def handle_niconico_errors(
    logger: logging.Logger,
    operation: str,
    context: str = "",
) -> AsyncGenerator[ErrorState, None]:
    """
    Context manager for handling Niconico errors with consistent logging.

    Args:
        logger: Logger instance
        operation: Description of the operation
        context: Additional context (e.g., item name)

    Yields:
        ErrorState: Object that evaluates to True if an error occurred

    Usage:
        async with handle_niconico_errors(
            self.provider.logger, "fetching playlist", playlist.name
        ) as error_state:
            # This may raise LoginFailureError, ConnectionError, etc.
            data = await self.niconico_adapter.get_playlist_data(playlist_id)
            return self._parse_playlist(data)

        # If an exception occurred, error_state will be True
        if error_state:
            return None
    """
    error_state = ErrorState()

    try:
        yield error_state
    except Exception as err:
        error_state.set_error(err)
        # Import here to avoid circular imports
        try:
            if isinstance(err, LoginFailureError):
                logger.debug("Authentication required for %s %s: %s", operation, context, err)
            elif isinstance(err, (ConnectionError, TimeoutError)):
                logger.warning("Network error %s %s: %s", operation, context, err)
            else:
                logger.debug("Error %s %s: %s", operation, context, err)
        except ImportError:
            # Fallback if niconico module is not available
            logger.debug("Error %s %s: %s", operation, context, err)
        # Don't re-raise - let caller check error state


async def get_library_items(
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
        from music_assistant_models.media_items import Track as TrackModel

        return TrackModel.from_dict(cached_track_data)

    return None
