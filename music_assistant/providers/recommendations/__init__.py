"""
Builtin Library Recommendations Provider.

Surfaces library-based discovery rows as recommendations on the Discover page.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

from music_assistant_models.enums import MediaType, ProviderFeature
from music_assistant_models.media_items import RecommendationFolder, UniqueList

from music_assistant.models.plugin import PluginProvider

if TYPE_CHECKING:
    from collections.abc import Sequence

    from music_assistant_models.config_entries import (
        ConfigEntry,
        ConfigValueType,
        ProviderConfig,
    )
    from music_assistant_models.media_items import BrowseFolder, ItemMapping, MediaItemType
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

SUPPORTED_FEATURES: set[ProviderFeature] = {
    ProviderFeature.RECOMMENDATIONS,
}


class LibraryRowID(StrEnum):
    """The item_ids of library recommendation rows."""

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
    FORGOTTEN_TRACKS = "forgotten_tracks"
    FORGOTTEN_ALBUMS = "forgotten_albums"
    FORGOTTEN_ARTISTS = "forgotten_artists"
    MOST_PLAYED_TRACKS = "most_played_tracks"
    NEVER_PLAYED_TRACKS = "never_played_tracks"


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return LibraryRecommendationsProvider(mass, manifest, config, SUPPORTED_FEATURES)


async def get_config_entries(
    mass: MusicAssistant,  # noqa: ARG001
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,  # noqa: ARG001
    values: dict[str, ConfigValueType] | None = None,  # noqa: ARG001
) -> tuple[ConfigEntry, ...]:
    """Return config entries for this provider."""
    return ()


class LibraryRecommendationsProvider(PluginProvider):
    """Builtin provider for library-based recommendation rows."""

    async def get_recommendations(self) -> list[RecommendationFolder]:
        """Get all library recommendation rows, without items."""
        return [
            _folder(
                LibraryRowID.IN_PROGRESS, "In progress", "in_progress_items", "mdi-motion-play"
            ),
            _folder(
                LibraryRowID.RECENTLY_PLAYED,
                "Recently played",
                "recently_played",
                "mdi-motion-play",
            ),
            _folder(
                LibraryRowID.RECENTLY_ADDED_TRACKS,
                "Recently added tracks",
                "recently_added_tracks",
                "mdi-music-note-plus",
                False,
            ),
            _folder(
                LibraryRowID.RECENTLY_ADDED_ALBUMS,
                "Recently added albums",
                "recently_added_albums",
                "mdi-album",
            ),
            _folder(
                LibraryRowID.RANDOM_ARTISTS,
                "Random artists",
                "random_artists",
                "mdi-account-music",
                False,
            ),
            _folder(
                LibraryRowID.RANDOM_ALBUMS, "Random albums", "random_albums", "mdi-album", False
            ),
            _folder(
                LibraryRowID.RECENT_FAVORITE_TRACKS,
                "Recently favorited tracks",
                "recent_favorite_tracks",
                "mdi-file-music",
                False,
            ),
            _folder(
                LibraryRowID.FAVORITE_PLAYLISTS,
                "Favorite playlists",
                "favorite_playlists",
                "mdi-playlist-music",
                False,
            ),
            _folder(
                LibraryRowID.FAVORITE_RADIO,
                "Favorite Radio stations",
                "favorite_radio_stations",
                "mdi-access-point",
                False,
            ),
            _folder(
                LibraryRowID.RECENT_ARTISTS,
                "Recent artists",
                "recent_artists",
                "mdi-account-music",
                False,
            ),
            _folder(
                LibraryRowID.RECENT_TRACKS,
                "Recent tracks",
                "recent_tracks",
                "mdi-music-note",
                False,
            ),
            _folder(
                LibraryRowID.FORGOTTEN_TRACKS,
                "Forgotten Tracks",
                "forgotten_tracks",
                "mdi-timer-sand",
                False,
            ),
            _folder(
                LibraryRowID.FORGOTTEN_ALBUMS,
                "Forgotten Albums",
                "forgotten_albums",
                "mdi-timer-sand",
                False,
            ),
            _folder(
                LibraryRowID.FORGOTTEN_ARTISTS,
                "Forgotten Artists",
                "forgotten_artists",
                "mdi-timer-sand",
                False,
            ),
            _folder(
                LibraryRowID.MOST_PLAYED_TRACKS,
                "Most Played Tracks",
                "most_played_tracks",
                "mdi-trophy",
                False,
            ),
            _folder(
                LibraryRowID.NEVER_PLAYED_TRACKS,
                "Never / Rarely Played",
                "never_played_tracks",
                "mdi-sleep",
                False,
            ),
        ]

    async def get_recommendation_items(
        self, item_id: str, providers: list[str] | None = None
    ) -> UniqueList[MediaItemType | ItemMapping | BrowseFolder]:
        """
        Get the items for a single library recommendation row.

        :param item_id: The item_id of the row, as returned by get_recommendations.
        :param providers: Restrict items to those reachable through one of these provider
            instance ids (OR semantics). An explicit empty list returns no items; None
            applies no filter.
        """
        if providers is not None and not providers:
            return UniqueList()
        items: Sequence[MediaItemType | ItemMapping | BrowseFolder]
        match item_id:
            case LibraryRowID.IN_PROGRESS:
                items = await self.mass.music.in_progress_items(limit=10, providers=providers)
            case LibraryRowID.RECENTLY_PLAYED:
                items = await self.mass.music.recently_played(
                    limit=10,
                    media_types=[
                        MediaType.ALBUM,
                        MediaType.TRACK,
                        MediaType.PLAYLIST,
                        MediaType.ARTIST,
                        MediaType.GENRE,
                    ],
                    user_initiated_only=True,
                    always_include_media_types=[MediaType.PODCAST, MediaType.AUDIOBOOK],
                    providers=providers,
                )
            case LibraryRowID.RECENTLY_ADDED_TRACKS:
                items = await self.mass.music.tracks.library_items(
                    limit=10, order_by="timestamp_added_desc", reachable_via=providers
                )
            case LibraryRowID.RECENTLY_ADDED_ALBUMS:
                items = await self.mass.music.albums.library_items(
                    limit=10, order_by="timestamp_added_desc", reachable_via=providers
                )
            case LibraryRowID.RANDOM_ARTISTS:
                items = await self.mass.music.artists.library_items(
                    limit=10, order_by="random_play_count", reachable_via=providers
                )
            case LibraryRowID.RANDOM_ALBUMS:
                items = await self.mass.music.albums.library_items(
                    limit=10, order_by="random_play_count", reachable_via=providers
                )
            case LibraryRowID.RECENT_FAVORITE_TRACKS:
                items = await self.mass.music.tracks.library_items(
                    favorite=True,
                    limit=10,
                    order_by="timestamp_modified_desc",
                    reachable_via=providers,
                )
            case LibraryRowID.FAVORITE_PLAYLISTS:
                items = await self.mass.music.playlists.library_items(
                    favorite=True, limit=10, order_by="random", reachable_via=providers
                )
            case LibraryRowID.FAVORITE_RADIO:
                items = await self.mass.music.radio.library_items(
                    favorite=True, limit=10, order_by="play_count_desc", reachable_via=providers
                )
            case LibraryRowID.RECENT_ARTISTS:
                items = await self.mass.music.recently_played(
                    limit=10,
                    media_types=[MediaType.ARTIST],
                    user_initiated_only=False,
                    providers=providers,
                )
            case LibraryRowID.RECENT_TRACKS:
                items = await self.mass.music.recently_played(
                    limit=10,
                    media_types=[MediaType.TRACK],
                    user_initiated_only=False,
                    providers=providers,
                )
            case LibraryRowID.FORGOTTEN_TRACKS:
                items = await self.mass.music.tracks.library_items(
                    limit=10, order_by="last_played", played_only=True, reachable_via=providers
                )
            case LibraryRowID.FORGOTTEN_ALBUMS:
                items = await self.mass.music.albums.library_items(
                    limit=10, order_by="last_played", played_only=True, reachable_via=providers
                )
            case LibraryRowID.FORGOTTEN_ARTISTS:
                items = await self.mass.music.artists.library_items(
                    limit=10, order_by="last_played", played_only=True, reachable_via=providers
                )
            case LibraryRowID.MOST_PLAYED_TRACKS:
                items = await self.mass.music.tracks.library_items(
                    limit=10, order_by="play_count_desc", reachable_via=providers
                )
            case LibraryRowID.NEVER_PLAYED_TRACKS:
                items = await self.mass.music.tracks.library_items(
                    limit=10, order_by="play_count", reachable_via=providers
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
    """Create a recommendation folder metadata object."""
    return RecommendationFolder(
        item_id=item_id.value,
        provider="recommendations",
        name=name,
        translation_key=translation_key,
        icon=icon,
        enabled_by_default=enabled_by_default,
        uri=f"library://folder/{item_id.value}",
        supports_provider_filter=True,
    )
