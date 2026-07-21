"""The default (library-backed) recommendation rows."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import RecommendationFolder

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from music_assistant_models.media_items import BrowseFolder, ItemMapping, MediaItemType

    from music_assistant.mass import MusicAssistant


@dataclass(frozen=True)
class LibraryRecommendation:
    """One default recommendation row: its descriptor fields and the query for its items."""

    item_id: str
    name: str
    translation_key: str
    icon: str
    get_items: Callable[
        [MusicAssistant], Awaitable[Sequence[MediaItemType | ItemMapping | BrowseFolder]]
    ]
    enabled_by_default: bool = True

    def folder(self) -> RecommendationFolder:
        """Return the row descriptor: a RecommendationFolder without items."""
        return RecommendationFolder(
            item_id=self.item_id,
            provider="library",
            name=self.name,
            translation_key=self.translation_key,
            icon=self.icon,
            enabled_by_default=self.enabled_by_default,
        )


def _build_library_recommendations() -> tuple[LibraryRecommendation, ...]:
    """Build the default rows table (function scope so the lambdas type-check deferred)."""
    return (
        LibraryRecommendation(
            item_id="in_progress",
            name="In progress",
            translation_key="in_progress_items",
            icon="mdi-motion-play",
            get_items=lambda mass: mass.music.in_progress_items(limit=10),
        ),
        LibraryRecommendation(
            item_id="recently_played",
            name="Recently played",
            translation_key="recently_played",
            icon="mdi-motion-play",
            get_items=lambda mass: mass.music.recently_played(
                limit=10,
                media_types=[
                    MediaType.ALBUM,
                    MediaType.TRACK,
                    MediaType.PLAYLIST,
                    MediaType.ARTIST,
                ],
                user_initiated_only=True,
                always_include_media_types=[MediaType.PODCAST, MediaType.AUDIOBOOK],
            ),
        ),
        LibraryRecommendation(
            item_id="recently_added_tracks",
            name="Recently added tracks",
            translation_key="recently_added_tracks",
            icon="music-note-plus",
            get_items=lambda mass: mass.music.tracks.library_items(
                limit=10, order_by="timestamp_added_desc", summary=False
            ),
        ),
        LibraryRecommendation(
            item_id="recently_added_albums",
            name="Recently added albums",
            translation_key="recently_added_albums",
            icon="music-note-plus",
            get_items=lambda mass: mass.music.albums.library_items(
                limit=10, order_by="timestamp_added_desc", summary=False
            ),
        ),
        LibraryRecommendation(
            item_id="random_artists",
            name="Random artists",
            translation_key="random_artists",
            icon="mdi-account-music",
            get_items=lambda mass: mass.music.artists.library_items(
                limit=10, order_by="random_play_count", summary=False
            ),
            enabled_by_default=False,
        ),
        LibraryRecommendation(
            item_id="random_albums",
            name="Random albums",
            translation_key="random_albums",
            icon="mdi-album",
            get_items=lambda mass: mass.music.albums.library_items(
                limit=10, order_by="random_play_count", summary=False
            ),
            enabled_by_default=False,
        ),
        LibraryRecommendation(
            item_id="recent_favorite_tracks",
            name="Recently favorited tracks",
            translation_key="recent_favorite_tracks",
            icon="mdi-file-music",
            get_items=lambda mass: mass.music.tracks.library_items(
                favorite=True, limit=10, order_by="timestamp_modified_desc", summary=False
            ),
        ),
        LibraryRecommendation(
            item_id="favorite_playlists",
            name="Favorite playlists",
            translation_key="favorite_playlists",
            icon="mdi-playlist-music",
            get_items=lambda mass: mass.music.playlists.library_items(
                favorite=True, limit=10, order_by="random", summary=False
            ),
        ),
        LibraryRecommendation(
            item_id="favorite_radio",
            name="Favorite Radio stations",
            translation_key="favorite_radio_stations",
            icon="mdi-access-point",
            get_items=lambda mass: mass.music.radio.library_items(
                favorite=True, limit=10, order_by="play_count_desc", summary=False
            ),
        ),
        LibraryRecommendation(
            item_id="recent_artists",
            name="Recent artists",
            translation_key="recent_artists",
            icon="mdi-account-music",
            get_items=lambda mass: mass.music.recently_played(
                limit=10, media_types=[MediaType.ARTIST], user_initiated_only=False
            ),
            enabled_by_default=False,
        ),
        LibraryRecommendation(
            item_id="recent_tracks",
            name="Recent tracks",
            translation_key="recent_tracks",
            icon="mdi-music-note",
            get_items=lambda mass: mass.music.recently_played(
                limit=10, media_types=[MediaType.TRACK], user_initiated_only=False
            ),
            enabled_by_default=False,
        ),
    )


LIBRARY_RECOMMENDATIONS = _build_library_recommendations()
