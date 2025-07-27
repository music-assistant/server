"""Utility functions for handling cookies and converting them into Netscape format."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, TypeVar, cast

from music_assistant.constants import CACHE_CATEGORY_LIBRARY_ITEMS
from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from music_assistant_models.media_items import Album, Playlist, Track
    from requests.cookies import RequestsCookieJar

T = TypeVar("T")


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
