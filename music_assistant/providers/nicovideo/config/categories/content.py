"""Content configuration category for Nicovideo provider."""

from __future__ import annotations

from typing import Literal

from music_assistant.providers.nicovideo.config.categories.base import ConfigCategoryBase
from music_assistant.providers.nicovideo.config.factory import ConfigFactory


class ContentConfigCategory(ConfigCategoryBase):
    """Content settings category."""

    _content = ConfigFactory("Content")

    auto_like_on_library_add = _content.bool_config(
        key="auto_like_on_library_add",
        label="Auto-like when adding to library",
        default=True,
        description=(
            "Automatically like videos on NicoNico when adding tracks to your "
            "Music Assistant library.\n"
            "Tracks removed from the library will not be unliked on NicoNico.\n"
        ),
    )

    include_library_track_artists = _content.bool_config(
        key="include_library_track_artists",
        label="Include artists from library tracks",
        default=True,
        description=(
            "Include artists from your library tracks in the artist library.\n"
            "When enabled, all artists from tracks in your library will appear "
            "in the artist section, even if you don't explicitly follow them on NicoNico."
        ),
    )

    include_own_mylists_tracks = _content.bool_config(
        key="include_own_mylists_tracks",
        label="Include tracks from own mylists",
        default=True,
        description=(
            "Include tracks from your own mylists in your library tracks.\n"
            "This allows you to manage whether playlist tracks appear in your main "
            "track library."
        ),
    )

    include_own_series_albums = _content.bool_config(
        key="include_own_series_albums",
        label="Include albums from own uploaded series",
        default=False,
        description=(
            "Include your own uploaded series as albums in your library.\n"
            "This allows you to manage whether your created series appear in your "
            "album library."
        ),
    )

    include_own_videos_tracks = _content.bool_config(
        key="include_own_videos_tracks",
        label="Include tracks from own uploaded videos",
        default=False,
        description=(
            "Include your own uploaded videos as tracks in your library.\n"
            "This allows you to manage whether your uploaded content appears in your "
            "track library."
        ),
    )

    include_followed_mylists = _content.bool_config(
        key="include_followed_mylists",
        label="Include playlists from followed mylists",
        default=False,
        description=(
            "Include mylists you directly follow in your library playlists.\n"
            "These playlists will be read-only and marked as not editable."
        ),
    )

    include_followed_mylists_tracks = _content.bool_config(
        key="include_followed_mylists_tracks",
        label="Include tracks from followed mylists",
        default=False,
        description=(
            "Include tracks from mylists you directly follow in your library tracks.\n"
            "This refers to mylists that you have explicitly followed,\n"
            "not to mylists from users you have followed."
        ),
    )

    @property
    def sensitive_contents_config(self) -> Literal["mask"]:
        """Get sensitive contents configuration - always returns 'mask' per niconico policy."""
        return "mask"
