"""Tests for syncing Spotify recently played history into Music Assistant."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import Album, Playlist, ProviderMapping, Track

from music_assistant.providers.spotify.constants import (
    CACHE_KEY_RECENTLY_PLAYED_CURSOR,
    CONF_SYNC_RECENTLY_PLAYED,
)
from music_assistant.providers.spotify.provider import SpotifyProvider

INSTANCE_ID = "spotify--test"


@dataclass
class _MockEnv:
    """Mock collaborators attached to a SpotifyProvider under test."""

    provider: SpotifyProvider
    cache_get: AsyncMock
    cache_set: AsyncMock
    mark_item_played: AsyncMock
    get_library_track: AsyncMock
    get_library_album: AsyncMock
    get_library_playlist: AsyncMock
    register_task: MagicMock
    unregister_task_and_wait: AsyncMock


def _make_provider(
    *,
    sync_enabled: bool = True,
    cached_cursor: int | None = None,
) -> _MockEnv:
    """Return a Spotify provider wired with mocks for recently-played sync tests."""
    provider = object.__new__(SpotifyProvider)
    provider.config = MagicMock(instance_id=INSTANCE_ID)
    provider.config.get_value.side_effect = lambda key, default=None: (
        sync_enabled if key == CONF_SYNC_RECENTLY_PLAYED else default
    )
    provider.manifest = MagicMock(domain="spotify")
    provider.logger = MagicMock()
    provider._sp_user = {"id": "user-1", "display_name": "Alice"}

    cache_get = AsyncMock(return_value=cached_cursor)
    cache_set = AsyncMock()
    mark_item_played = AsyncMock()
    get_library_track = AsyncMock(return_value=None)
    get_library_album = AsyncMock(return_value=None)
    get_library_playlist = AsyncMock(return_value=None)
    register_task = MagicMock()
    unregister_task_and_wait = AsyncMock()

    provider.mass = MagicMock()
    provider.mass.cache.get = cache_get
    provider.mass.cache.set = cache_set
    provider.mass.music.tracks.get_library_item_by_prov_id = get_library_track
    provider.mass.music.albums.get_library_item_by_prov_id = get_library_album
    provider.mass.music.playlists.get_library_item_by_prov_id = get_library_playlist
    provider.mass.music.mark_item_played = mark_item_played
    provider.mass.tasks.register_scheduled_task = register_task
    provider.mass.tasks.unregister_scheduled_task = MagicMock()
    provider.mass.tasks.unregister_scheduled_task_and_wait = unregister_task_and_wait
    return _MockEnv(
        provider=provider,
        cache_get=cache_get,
        cache_set=cache_set,
        mark_item_played=mark_item_played,
        get_library_track=get_library_track,
        get_library_album=get_library_album,
        get_library_playlist=get_library_playlist,
        register_task=register_task,
        unregister_task_and_wait=unregister_task_and_wait,
    )


def _played_entry(
    track_id: str,
    played_at: str,
    *,
    album_id: str = "album-1",
    context: dict[str, Any] | None = None,
    is_local: bool = False,
) -> dict[str, Any]:
    """Build one item from Spotify's me/player/recently-played response."""
    return {
        "played_at": played_at,
        "context": context,
        "track": {
            "id": None if is_local else track_id,
            "name": f"Track {track_id}",
            "duration_ms": 180000,
            "disc_number": 1,
            "track_number": 1,
            "explicit": False,
            "is_local": is_local,
            "is_playable": True,
            "external_urls": {"spotify": f"https://open.spotify.com/track/{track_id}"},
            "artists": [
                {
                    "id": "artist-1",
                    "name": "Alice Artist",
                    "external_urls": {"spotify": "https://open.spotify.com/artist/artist-1"},
                }
            ],
            "album": {
                "id": album_id,
                "name": f"Album {album_id}",
                "album_type": "album",
                "external_urls": {"spotify": f"https://open.spotify.com/album/{album_id}"},
                "artists": [
                    {
                        "id": "artist-1",
                        "name": "Alice Artist",
                        "external_urls": {"spotify": "https://open.spotify.com/artist/artist-1"},
                    }
                ],
                "images": [],
            },
        },
    }


async def test_recently_played_sync_processes_chronologically_and_advances_cursor() -> None:
    """Plays are forwarded oldest-first with provider timestamp and cursor is persisted."""
    env = _make_provider(cached_cursor=1700000000000)
    get_data = AsyncMock(
        return_value={
            "items": [
                _played_entry("track-2", "2026-11-21T06:10:00Z"),
                _played_entry("track-1", "2026-11-21T06:05:00Z"),
            ]
        }
    )
    env.provider._get_data = get_data  # type: ignore[method-assign]

    await env.provider._sync_recently_played()

    get_data.assert_awaited_once_with("me/player/recently-played", limit=50, after=1700000000000)
    calls = env.mark_item_played.await_args_list
    assert [c.args[0].item_id for c in calls] == ["track-1", "track-2"]
    assert all(c.kwargs["user_initiated"] is False for c in calls)
    assert all(c.kwargs["provider_instance_id"] == INSTANCE_ID for c in calls)
    assert calls[0].kwargs["progress_timestamp"] < calls[1].kwargs["progress_timestamp"]

    env.cache_set.assert_awaited_once()
    cache_call = env.cache_set.await_args
    assert cache_call is not None
    assert cache_call.args[0] == CACHE_KEY_RECENTLY_PLAYED_CURSOR
    assert cache_call.kwargs["provider"] == INSTANCE_ID
    assert cache_call.kwargs["persistent"] is True


async def test_recently_played_sync_prefers_library_track_and_marks_context_container() -> None:
    """Library tracks are reused and album/playlist contexts mark containers user-initiated."""
    env = _make_provider()
    lib_track = Track(
        item_id="42",
        provider="library",
        name="Library Track",
        duration=200,
        provider_mappings={
            ProviderMapping(
                item_id="track-1",
                provider_domain="spotify",
                provider_instance=INSTANCE_ID,
            )
        },
    )
    lib_playlist = Playlist(
        item_id="9",
        provider="library",
        name="Morning Mix",
        provider_mappings={
            ProviderMapping(
                item_id="playlist-1",
                provider_domain="spotify",
                provider_instance=INSTANCE_ID,
            )
        },
    )
    env.get_library_track.return_value = lib_track
    env.get_library_playlist.return_value = lib_playlist
    env.provider._get_data = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "items": [
                _played_entry(
                    "track-1",
                    "2026-11-21T06:00:00Z",
                    context={"type": "playlist", "uri": "spotify:playlist:playlist-1"},
                ),
                _played_entry(
                    "track-1",
                    "2026-11-21T06:04:00Z",
                    context={"type": "playlist", "uri": "spotify:playlist:playlist-1"},
                ),
            ]
        }
    )

    await env.provider._sync_recently_played()

    calls = env.mark_item_played.await_args_list
    # two track plays (user_initiated=False) + one deduplicated container update (user_initiated=True)
    assert len(calls) == 3
    assert calls[0].args[0] is lib_track
    assert calls[0].kwargs["user_initiated"] is False
    assert calls[1].args[0] is lib_track
    assert calls[1].kwargs["user_initiated"] is False
    assert calls[2].args[0] is lib_playlist
    assert calls[2].kwargs["user_initiated"] is True
    assert calls[2].kwargs["progress_timestamp"] == calls[1].kwargs["progress_timestamp"]


async def test_recently_played_sync_uses_embedded_album_context_and_skips_local_tracks() -> None:
    """Album contexts fall back to embedded album metadata; local tracks are ignored."""
    env = _make_provider()
    env.provider._get_data = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "items": [
                _played_entry("local-1", "2026-11-21T06:01:00Z", is_local=True),
                _played_entry(
                    "track-9",
                    "2026-11-21T06:02:00Z",
                    album_id="album-9",
                    context={"type": "album", "uri": "spotify:album:album-9"},
                ),
            ]
        }
    )

    await env.provider._sync_recently_played()

    calls = env.mark_item_played.await_args_list
    assert len(calls) == 2
    assert isinstance(calls[0].args[0], Track)
    assert calls[0].args[0].item_id == "track-9"
    assert calls[0].kwargs["user_initiated"] is False
    assert isinstance(calls[1].args[0], Album)
    assert calls[1].args[0].item_id == "album-9"
    assert calls[1].kwargs["user_initiated"] is True


async def test_recently_played_sync_disabled_skips_api_call() -> None:
    """No Spotify API call is made when recently played sync is disabled."""
    env = _make_provider(sync_enabled=False)
    get_data = AsyncMock()
    env.provider._get_data = get_data  # type: ignore[method-assign]

    await env.provider._sync_recently_played()

    get_data.assert_not_awaited()
    env.mark_item_played.assert_not_awaited()


async def test_loaded_in_mass_registers_and_unload_unregisters_task() -> None:
    """Enabling sync registers the scheduled hourly task and unloading cleans it up."""
    env = _make_provider(sync_enabled=True)
    env.provider.backend = MagicMock(unload=AsyncMock())

    await env.provider.loaded_in_mass()
    env.register_task.assert_called_once()
    assert env.provider._recently_played_task_registered is True

    await env.provider.unload(is_removed=False)
    env.unregister_task_and_wait.assert_awaited_once_with(
        f"spotify_recently_played_{INSTANCE_ID}",
        clear_persisted_state=False,
    )
    assert env.provider._recently_played_task_registered is False


async def test_sync_library_triggers_recently_played_on_track_sync() -> None:
    """Manual or scheduled track library sync also refreshes recently played tracks."""
    env = _make_provider(sync_enabled=True)
    run_library_sync = AsyncMock()
    sync_recently_played = AsyncMock()
    env.provider._run_library_sync = run_library_sync  # type: ignore[method-assign]
    env.provider._sync_recently_played = sync_recently_played  # type: ignore[method-assign]

    await env.provider.sync_library(MediaType.TRACK)

    run_library_sync.assert_awaited_once_with(MediaType.TRACK)
    sync_recently_played.assert_awaited_once()
