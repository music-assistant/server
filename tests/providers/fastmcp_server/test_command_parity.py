"""Parity contracts for canonical MA commands and capability classification."""

from __future__ import annotations

from pathlib import Path

from music_assistant import MusicAssistant
from music_assistant.controllers.config import ConfigController
from music_assistant.controllers.discovery import DiscoveryController
from music_assistant.providers.fastmcp_server.command_policy import resolve_command_policy
from music_assistant.providers.fastmcp_server.command_profiles import (
    COMMAND_PROFILES,
    CURATED_PROFILE_MAPPINGS,
)

_PROFILE_BASELINE = {
    "library_get_track_by_uri": "music/item_by_uri",
    "library_get_album_by_uri": "music/item_by_uri",
    "library_get_artist_by_uri": "music/item_by_uri",
    "library_get_artist_albums": "music/artists/artist_albums",
    "library_get_playlist_by_uri": "music/item_by_uri",
    "library_get_radio_by_uri": "music/item_by_uri",
    "library_get_album_tracks": "music/albums/album_tracks",
    "library_search_tracks": "music/search",
    "library_search_albums": "music/search",
    "library_search_artists": "music/search",
    "library_list_library_tracks": "music/tracks/library_items",
    "library_list_library_albums": "music/albums/library_items",
    "library_list_library_artists": "music/artists/library_items",
    "library_list_library_playlists": "music/playlists/library_items",
    "library_list_library_radio": "music/radios/library_items",
    "library_recently_added_tracks": "music/recently_added_tracks",
    "media_add_to_favorites": "music/favorites/add_item",
    "media_remove_from_favorites": "music/favorites/remove_item",
    "media_add_to_library": "music/library/add_item",
    "media_remove_from_library": "music/library/remove_item",
    "media_mark_played": "music/mark_played",
    "media_play_announcement": "players/cmd/play_announcement",
    "metadata_recommendations": "music/recommendations",
    "metadata_recommendation_items": "music/recommendations/items",
    "metadata_recently_played": "music/recently_played_items",
    "metadata_get_lyrics": "metadata/get_track_lyrics",
    "playback_pause": "players/cmd/pause",
    "playback_resume": "players/cmd/resume",
    "playback_play_pause": "players/cmd/play_pause",
    "playback_stop": "players/cmd/stop",
    "playback_next_track": "players/cmd/next",
    "playback_previous_track": "players/cmd/previous",
    "playback_skip": "player_queues/skip",
    "playback_seek": "players/cmd/seek",
    "playback_play_media": "player_queues/play_media",
    "playback_play_index": "player_queues/play_index",
    "players_set_power": "players/cmd/power",
    "players_group_player": "players/cmd/group",
    "players_ungroup_player": "players/cmd/ungroup",
    "playlists_create_playlist": "music/playlists/create_playlist",
    "playlists_add_track": "music/playlists/add_playlist_tracks",
    "playlists_remove_tracks": "music/playlists/remove_playlist_tracks",
    "queue_set_shuffle": "player_queues/shuffle",
    "queue_set_repeat": "player_queues/repeat",
    "volume_volume_set": "players/cmd/volume_set",
    "volume_volume_up": "players/cmd/volume_up",
    "volume_volume_down": "players/cmd/volume_down",
    "volume_volume_mute": "players/cmd/volume_mute",
    "volume_group_volume_set": "players/cmd/group_volume",
}


def test_canonical_profiles_keep_search_aliases_without_legacy_dispatch() -> None:
    """Former curated names are search metadata only, never executable redirects."""
    assert CURATED_PROFILE_MAPPINGS == _PROFILE_BASELINE
    for alias, command in CURATED_PROFILE_MAPPINGS.items():
        assert alias in COMMAND_PROFILES[command].search_aliases


async def test_current_ma_registry_is_capability_classified_or_explicitly_denied(
    tmp_path: Path,
) -> None:
    """Every live authenticated handler is classified or explicitly hard-denied."""
    mass = MusicAssistant(str(tmp_path), str(tmp_path))
    mass.config = ConfigController(mass)
    mass.config.initialized = True
    mass.discovery = DiscoveryController(mass)
    await mass._load_core_controllers()
    mass._register_api_commands()

    unclassified: list[str] = []
    unexpectedly_denied: list[str] = []
    classifications: dict[str, list[str] | str] = {}
    for command, handler in mass.command_handlers.items():
        if not handler.authenticated:
            continue
        decision = resolve_command_policy(
            command,
            handler.required_scope,
            COMMAND_PROFILES.get(command),
        )
        if not (
            decision.hard_denied
            or decision.required_capabilities
            or decision.alternative_capabilities
        ):
            unclassified.append(command)
        if decision.hard_denied and not (
            command.startswith("auth/")
            or command in {"dashboard/register", "dashboard/unregister", "music/tracks/preview"}
        ):
            unexpectedly_denied.append(command)
        classifications[command] = (
            "hard-denied"
            if decision.hard_denied
            else sorted(decision.required_capabilities or decision.alternative_capabilities)
        )

    assert unclassified == []
    assert unexpectedly_denied == []
    assert classifications
