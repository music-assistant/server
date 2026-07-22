"""The default (library-backed) recommendation rows."""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import RecommendationFolder, UniqueList

if TYPE_CHECKING:
    from collections.abc import Sequence

    from music_assistant_models.media_items import BrowseFolder, ItemMapping, MediaItemType

    from music_assistant.mass import MusicAssistant


class LibraryRowID(StrEnum):
    """The item_ids of the built-in library recommendation rows."""

    IN_PROGRESS = "in_progress"
    RECENTLY_PLAYED = "recently_played"
    RECENTLY_ADDED_TRACKS = "recently_added_tracks"
    RECENTLY_ADDED_ALBUMS = "recently_added_albums"
    RANDOM_ARTISTS = "random_artists"
    RANDOM_ALBUMS = "random_albums"
    RECENT_FAVORITE_TRACKS = "recent_favorite_tracks"
    FAVORITE_PLAYLISTS = "favorite_playlists"
    FAVORITE_RADIO = "favorite_radio"
    RECENT_ARTISTS = "recent_artists"
    RECENT_TRACKS = "recent_tracks"


def library_rows() -> list[RecommendationFolder]:
    """Return the built-in library recommendation rows, without items."""
    return [
        _folder(LibraryRowID.IN_PROGRESS, "In progress", "in_progress_items", "mdi-motion-play"),
        _folder(
            LibraryRowID.RECENTLY_PLAYED, "Recently played", "recently_played", "mdi-motion-play"
        ),
        _folder(
            LibraryRowID.RECENTLY_ADDED_TRACKS,
            "Recently added tracks",
            "recently_added_tracks",
            "music-note-plus",
        ),
        _folder(
            LibraryRowID.RECENTLY_ADDED_ALBUMS,
            "Recently added albums",
            "recently_added_albums",
            "music-note-plus",
        ),
        _folder(
            LibraryRowID.RANDOM_ARTISTS,
            "Random artists",
            "random_artists",
            "mdi-account-music",
            False,
        ),
        _folder(LibraryRowID.RANDOM_ALBUMS, "Random albums", "random_albums", "mdi-album", False),
        _folder(
            LibraryRowID.RECENT_FAVORITE_TRACKS,
            "Recently favorited tracks",
            "recent_favorite_tracks",
            "mdi-file-music",
        ),
        _folder(
            LibraryRowID.FAVORITE_PLAYLISTS,
            "Favorite playlists",
            "favorite_playlists",
            "mdi-playlist-music",
        ),
        _folder(
            LibraryRowID.FAVORITE_RADIO,
            "Favorite Radio stations",
            "favorite_radio_stations",
            "mdi-access-point",
        ),
        _folder(
            LibraryRowID.RECENT_ARTISTS,
            "Recent artists",
            "recent_artists",
            "mdi-account-music",
            False,
        ),
        _folder(
            LibraryRowID.RECENT_TRACKS, "Recent tracks", "recent_tracks", "mdi-music-note", False
        ),
    ]


async def library_items(
    mass: MusicAssistant, item_id: str
) -> UniqueList[MediaItemType | ItemMapping | BrowseFolder]:
    """Return the items for one built-in library row (empty for an unknown item_id)."""
    items: Sequence[MediaItemType | ItemMapping | BrowseFolder]
    match item_id:
        case LibraryRowID.IN_PROGRESS:
            items = await mass.music.in_progress_items(limit=10)
        case LibraryRowID.RECENTLY_PLAYED:
            items = await mass.music.recently_played(
                limit=10,
                media_types=[
                    MediaType.ALBUM,
                    MediaType.TRACK,
                    MediaType.PLAYLIST,
                    MediaType.ARTIST,
                ],
                user_initiated_only=True,
                always_include_media_types=[MediaType.PODCAST, MediaType.AUDIOBOOK],
            )
        case LibraryRowID.RECENTLY_ADDED_TRACKS:
            items = await mass.music.tracks.library_items(limit=10, order_by="timestamp_added_desc")
        case LibraryRowID.RECENTLY_ADDED_ALBUMS:
            items = await mass.music.albums.library_items(limit=10, order_by="timestamp_added_desc")
        case LibraryRowID.RANDOM_ARTISTS:
            items = await mass.music.artists.library_items(limit=10, order_by="random_play_count")
        case LibraryRowID.RANDOM_ALBUMS:
            items = await mass.music.albums.library_items(limit=10, order_by="random_play_count")
        case LibraryRowID.RECENT_FAVORITE_TRACKS:
            items = await mass.music.tracks.library_items(
                favorite=True, limit=10, order_by="timestamp_modified_desc"
            )
        case LibraryRowID.FAVORITE_PLAYLISTS:
            items = await mass.music.playlists.library_items(
                favorite=True, limit=10, order_by="random"
            )
        case LibraryRowID.FAVORITE_RADIO:
            items = await mass.music.radio.library_items(
                favorite=True, limit=10, order_by="play_count_desc"
            )
        case LibraryRowID.RECENT_ARTISTS:
            items = await mass.music.recently_played(
                limit=10, media_types=[MediaType.ARTIST], user_initiated_only=False
            )
        case LibraryRowID.RECENT_TRACKS:
            items = await mass.music.recently_played(
                limit=10, media_types=[MediaType.TRACK], user_initiated_only=False
            )
        case _:
            items = []
    return UniqueList(items)


def _folder(
    item_id: LibraryRowID,
    name: str,
    translation_key: str,
    icon: str,
    enabled_by_default: bool = True,
) -> RecommendationFolder:
    return RecommendationFolder(
        item_id=item_id.value,
        provider="library",
        name=name,
        translation_key=translation_key,
        icon=icon,
        enabled_by_default=enabled_by_default,
    )
