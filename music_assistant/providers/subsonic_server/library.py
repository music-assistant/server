"""Aggregates over the Music Assistant library that Subsonic responses need."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Collection, Sequence
from typing import TYPE_CHECKING

from music_assistant.constants import DB_TABLE_ALBUM_ARTISTS, DB_TABLE_ALBUM_TRACKS, DB_TABLE_TRACKS

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

PAGE_SIZE = 500


async def album_track_stats(
    mass: MusicAssistant, album_ids: Collection[str | int]
) -> dict[str, tuple[int, int]]:
    """
    Return the number of library tracks and their total duration for each album.

    :param mass: The Music Assistant instance.
    :param album_ids: Library ids of the albums.
    :return: Mapping of album id to (song count, duration in seconds).
    """
    if not album_ids:
        return {}
    query = (
        "SELECT album_tracks.album_id AS album_id, COUNT(*) AS song_count, "
        "COALESCE(SUM(tracks.duration), 0) AS duration "
        f"FROM {DB_TABLE_ALBUM_TRACKS} AS album_tracks "
        f"JOIN {DB_TABLE_TRACKS} AS tracks ON tracks.item_id = album_tracks.track_id "
        "WHERE album_tracks.album_id IN :album_ids GROUP BY album_tracks.album_id"
    )
    rows = await mass.music.database.get_rows_from_query(
        query, {"album_ids": [int(album_id) for album_id in album_ids]}, limit=0
    )
    return {
        str(row["album_id"]): (int(row["song_count"]), int(row["duration"] or 0)) for row in rows
    }


async def artist_album_counts(
    mass: MusicAssistant, artist_ids: Collection[str | int]
) -> dict[str, int]:
    """
    Return the number of library albums of each artist.

    :param mass: The Music Assistant instance.
    :param artist_ids: Library ids of the artists.
    """
    if not artist_ids:
        return {}
    query = (
        "SELECT artist_id, COUNT(*) AS album_count "
        f"FROM {DB_TABLE_ALBUM_ARTISTS} WHERE artist_id IN :artist_ids GROUP BY artist_id"
    )
    rows = await mass.music.database.get_rows_from_query(
        query, {"artist_ids": [int(artist_id) for artist_id in artist_ids]}, limit=0
    )
    return {str(row["artist_id"]): int(row["album_count"]) for row in rows}


async def collect_pages[T](fetch: Callable[[int, int], Awaitable[Sequence[T]]]) -> list[T]:
    """
    Return every item of a paged listing.

    :param fetch: Returns one page for the given limit and offset.
    """
    items: list[T] = []
    offset = 0
    while True:
        page = await fetch(PAGE_SIZE, offset)
        items.extend(page)
        if len(page) < PAGE_SIZE:
            return items
        offset += PAGE_SIZE
