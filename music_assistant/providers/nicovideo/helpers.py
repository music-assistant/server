"""Utility functions for handling cookies and converting them into Netscape format."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from mashumaro import DataClassDictMixin
from music_assistant_models.media_items import (  # noqa: TC002 - Used by DataClassDictMixin
    Album,
    Playlist,
    Track,
)

from music_assistant.constants import (
    CACHE_CATEGORY_MUSIC_PROVIDER_ITEM,
    VERBOSE_LOG_LEVEL,
)

if TYPE_CHECKING:
    import logging

    from requests.cookies import RequestsCookieJar

    from music_assistant.models.music_provider import MusicProvider


@dataclass
class PlaylistWithTracks(DataClassDictMixin):
    """Helper class to hold playlist and its tracks."""

    playlist: Playlist
    tracks: list[Track]


@dataclass
class AlbumWithTracks(DataClassDictMixin):
    """Helper class to hold album and its tracks."""

    album: Album
    tracks: list[Track]


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


async def cache_track(provider: MusicProvider, track: Track) -> None:
    """Cache single track with provider item cache.

    Note: While MusicAssistant's get_provider_item automatically handles cache retrieval
    and storage, this helper function is needed for explicit cache updates (e.g., when
    adding album information to tracks) since MusicAssistant doesn't provide a dedicated
    cache-only update function.
    """
    cache_key = f"track.{track.item_id}"
    await provider.mass.cache.set(
        cache_key,
        track.to_dict(),
        category=CACHE_CATEGORY_MUSIC_PROVIDER_ITEM,
        base_key=provider.lookup_key,
    )


def log_verbose(logger: logging.Logger, message: str, *args: object) -> None:
    """Log a message at VERBOSE level with performance optimization.

    Args:
        logger: Logger instance
        message: Log message format string
        *args: Arguments for the message format string
    """
    if logger.isEnabledFor(VERBOSE_LOG_LEVEL):
        logger.log(VERBOSE_LOG_LEVEL, message, *args)
