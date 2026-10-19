"""Aggregates over the Music Assistant library that Subsonic responses need."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Collection, Sequence
from typing import TYPE_CHECKING, Any

from music_assistant_models.enums import MediaType

from music_assistant.constants import (
    DB_TABLE_ALBUM_ARTISTS,
    DB_TABLE_ALBUM_TRACKS,
    DB_TABLE_PROVIDER_MAPPINGS,
    DB_TABLE_TRACKS,
)
from music_assistant.controllers.webserver.helpers.auth_middleware import get_current_user
from music_assistant.helpers.provider_access import visible_music_sources

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

PAGE_SIZE = 500


async def album_track_stats(
    mass: MusicAssistant, album_ids: Collection[str | int]
) -> dict[str, tuple[int, int]]:
    """
    Return the number of library tracks the calling user may see, and their duration, per album.

    :param mass: The Music Assistant instance.
    :param album_ids: Library ids of the albums.
    :return: Mapping of album id to (song count, duration in seconds).
    """
    if not album_ids:
        return {}
    params: dict[str, Any] = {"album_ids": [int(album_id) for album_id in album_ids]}
    # the tracks get_library_album_tracks lists for the calling user
    conditions = [
        "album_tracks.album_id IN :album_ids",
        *_visible_conditions(mass, "tracks.item_id", MediaType.TRACK, params),
    ]
    query = (
        "SELECT album_tracks.album_id AS album_id, COUNT(*) AS song_count, "
        "COALESCE(SUM(tracks.duration), 0) AS duration "
        f"FROM {DB_TABLE_ALBUM_TRACKS} AS album_tracks "
        f"JOIN {DB_TABLE_TRACKS} AS tracks ON tracks.item_id = album_tracks.track_id "
        f"WHERE {' AND '.join(conditions)} GROUP BY album_tracks.album_id"
    )
    rows = await mass.music.database.get_rows_from_query(query, params, limit=0)
    return {
        str(row["album_id"]): (int(row["song_count"]), int(row["duration"] or 0)) for row in rows
    }


async def artist_album_counts(
    mass: MusicAssistant, artist_ids: Collection[str | int]
) -> dict[str, int]:
    """
    Return the number of library albums the calling user may see, per artist.

    :param mass: The Music Assistant instance.
    :param artist_ids: Library ids of the artists.
    """
    if not artist_ids:
        return {}
    params: dict[str, Any] = {"artist_ids": [int(artist_id) for artist_id in artist_ids]}
    # the albums get_library_artist_albums lists for the calling user
    conditions = [
        "album_artists.artist_id IN :artist_ids",
        *_visible_conditions(
            mass, "album_artists.album_id", MediaType.ALBUM, params, in_library_only=True
        ),
    ]
    query = (
        "SELECT album_artists.artist_id AS artist_id, COUNT(*) AS album_count "
        f"FROM {DB_TABLE_ALBUM_ARTISTS} AS album_artists "
        f"WHERE {' AND '.join(conditions)} GROUP BY album_artists.artist_id"
    )
    rows = await mass.music.database.get_rows_from_query(query, params, limit=0)
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


def _visible_conditions(
    mass: MusicAssistant,
    item_id_column: str,
    media_type: MediaType,
    params: dict[str, Any],
    *,
    in_library_only: bool = False,
) -> list[str]:
    """
    Return the SQL conditions that keep the library items the calling user may see.

    :param mass: The Music Assistant instance.
    :param item_id_column: The column that holds the library id of the item.
    :param media_type: The media type of the item.
    :param params: Query params dict; the conditions' bound params are added to it.
    :param in_library_only: Only keep the items that are in a provider's library.
    """
    user = get_current_user()
    sources = visible_music_sources(mass, user) if user else None
    if sources is None and not in_library_only:
        return []
    params["mapping_media_type"] = media_type.value
    mapping = [
        f"mappings.item_id = {item_id_column}",
        "mappings.media_type = :mapping_media_type",
    ]
    if in_library_only:
        mapping.append("mappings.in_library = 1")
    if sources is not None:
        params["visible_sources"] = sources
        mapping.append("mappings.provider_instance IN :visible_sources")
    return [
        f"EXISTS(SELECT 1 FROM {DB_TABLE_PROVIDER_MAPPINGS} AS mappings "
        f"WHERE {' AND '.join(mapping)})"
    ]
