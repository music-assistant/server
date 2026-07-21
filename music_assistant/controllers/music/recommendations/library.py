"""The default (library-backed) recommendation rows."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import RecommendationFolder

if TYPE_CHECKING:
    from collections.abc import Sequence

    from music_assistant_models.media_items import BrowseFolder, ItemMapping, MediaItemType

    from music_assistant.mass import MusicAssistant


def library_rows() -> list[RecommendationFolder]:
    """Return the built-in library recommendation rows, without items."""
    return [
        _folder("in_progress", "In progress", "in_progress_items", "mdi-motion-play"),
        _folder("recently_played", "Recently played", "recently_played", "mdi-motion-play"),
        _folder(
            "recently_added_tracks",
            "Recently added tracks",
            "recently_added_tracks",
            "music-note-plus",
        ),
        _folder(
            "recently_added_albums",
            "Recently added albums",
            "recently_added_albums",
            "music-note-plus",
        ),
        _folder("random_artists", "Random artists", "random_artists", "mdi-account-music", False),
        _folder("random_albums", "Random albums", "random_albums", "mdi-album", False),
        _folder(
            "recent_favorite_tracks",
            "Recently favorited tracks",
            "recent_favorite_tracks",
            "mdi-file-music",
        ),
        _folder(
            "favorite_playlists", "Favorite playlists", "favorite_playlists", "mdi-playlist-music"
        ),
        _folder(
            "favorite_radio",
            "Favorite Radio stations",
            "favorite_radio_stations",
            "mdi-access-point",
        ),
        _folder("recent_artists", "Recent artists", "recent_artists", "mdi-account-music", False),
        _folder("recent_tracks", "Recent tracks", "recent_tracks", "mdi-music-note", False),
    ]


async def library_items(
    mass: MusicAssistant, item_id: str
) -> Sequence[MediaItemType | ItemMapping | BrowseFolder]:
    """Return the items for one built-in library row (empty for an unknown item_id)."""
    match item_id:
        case "in_progress":
            return await mass.music.in_progress_items(limit=10)
        case "recently_played":
            return await mass.music.recently_played(
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
        case "recently_added_tracks":
            return await mass.music.tracks.library_items(
                limit=10, order_by="timestamp_added_desc", summary=False
            )
        case "recently_added_albums":
            return await mass.music.albums.library_items(
                limit=10, order_by="timestamp_added_desc", summary=False
            )
        case "random_artists":
            return await mass.music.artists.library_items(
                limit=10, order_by="random_play_count", summary=False
            )
        case "random_albums":
            return await mass.music.albums.library_items(
                limit=10, order_by="random_play_count", summary=False
            )
        case "recent_favorite_tracks":
            return await mass.music.tracks.library_items(
                favorite=True, limit=10, order_by="timestamp_modified_desc", summary=False
            )
        case "favorite_playlists":
            return await mass.music.playlists.library_items(
                favorite=True, limit=10, order_by="random", summary=False
            )
        case "favorite_radio":
            return await mass.music.radio.library_items(
                favorite=True, limit=10, order_by="play_count_desc", summary=False
            )
        case "recent_artists":
            return await mass.music.recently_played(
                limit=10, media_types=[MediaType.ARTIST], user_initiated_only=False
            )
        case "recent_tracks":
            return await mass.music.recently_played(
                limit=10, media_types=[MediaType.TRACK], user_initiated_only=False
            )
        case _:
            return []


def _folder(
    item_id: str,
    name: str,
    translation_key: str,
    icon: str,
    enabled_by_default: bool = True,
) -> RecommendationFolder:
    return RecommendationFolder(
        item_id=item_id,
        provider="library",
        name=name,
        translation_key=translation_key,
        icon=icon,
        enabled_by_default=enabled_by_default,
    )
