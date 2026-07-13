"""Default (library) recommendation sources."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.enums import MediaType

from .base import CallableRecommendationSource, RecommendationSource

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant


def build_default_sources(mass: MusicAssistant) -> list[RecommendationSource]:
    """Return the built-in library recommendation sources in canonical order."""
    return [
        CallableRecommendationSource(
            mass,
            item_id="in_progress",
            name="In progress",
            translation_key="in_progress_items",
            icon="mdi-motion-play",
            items_factory=lambda: mass.music.in_progress_items(limit=10),
        ),
        CallableRecommendationSource(
            mass,
            item_id="recently_played",
            name="Recently played",
            translation_key="recently_played",
            icon="mdi-motion-play",
            items_factory=lambda: mass.music.recently_played(
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
        CallableRecommendationSource(
            mass,
            item_id="recently_added_tracks",
            name="Recently added tracks",
            translation_key="recently_added_tracks",
            icon="music-note-plus",
            items_factory=lambda: mass.music.tracks.library_items(
                limit=10, order_by="timestamp_added_desc", summary=False
            ),
        ),
        CallableRecommendationSource(
            mass,
            item_id="recently_added_albums",
            name="Recently added albums",
            translation_key="recently_added_albums",
            icon="music-note-plus",
            items_factory=lambda: mass.music.albums.library_items(
                limit=10, order_by="timestamp_added_desc", summary=False
            ),
        ),
        CallableRecommendationSource(
            mass,
            item_id="random_artists",
            name="Random artists",
            translation_key="random_artists",
            icon="mdi-account-music",
            items_factory=lambda: mass.music.artists.library_items(
                limit=10, order_by="random_play_count", summary=False
            ),
        ),
        CallableRecommendationSource(
            mass,
            item_id="random_albums",
            name="Random albums",
            translation_key="random_albums",
            icon="mdi-album",
            items_factory=lambda: mass.music.albums.library_items(
                limit=10, order_by="random_play_count", summary=False
            ),
        ),
        CallableRecommendationSource(
            mass,
            item_id="recent_favorite_tracks",
            name="Recently favorited tracks",
            translation_key="recent_favorite_tracks",
            icon="mdi-file-music",
            items_factory=lambda: mass.music.tracks.library_items(
                favorite=True, limit=10, order_by="timestamp_modified_desc", summary=False
            ),
        ),
        CallableRecommendationSource(
            mass,
            item_id="favorite_playlists",
            name="Favorite playlists",
            translation_key="favorite_playlists",
            icon="mdi-playlist-music",
            items_factory=lambda: mass.music.playlists.library_items(
                favorite=True, limit=10, order_by="random", summary=False
            ),
        ),
        CallableRecommendationSource(
            mass,
            item_id="favorite_radio",
            name="Favorite Radio stations",
            translation_key="favorite_radio_stations",
            icon="mdi-access-point",
            items_factory=lambda: mass.music.radio.library_items(
                favorite=True, limit=10, order_by="play_count_desc", summary=False
            ),
        ),
        CallableRecommendationSource(
            mass,
            item_id="recent_artists",
            name="Recent artists",
            translation_key="recent_artists",
            icon="mdi-account-music",
            items_factory=lambda: mass.music.recently_played(
                limit=10, media_types=[MediaType.ARTIST], user_initiated_only=False
            ),
        ),
        CallableRecommendationSource(
            mass,
            item_id="recent_tracks",
            name="Recent tracks",
            translation_key="recent_tracks",
            icon="mdi-music-note",
            items_factory=lambda: mass.music.recently_played(
                limit=10, media_types=[MediaType.TRACK], user_initiated_only=False
            ),
        ),
    ]
