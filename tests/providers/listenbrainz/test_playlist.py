"""Tests for ListenBrainz playlist snapshots and lazy track listings."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import ExternalID
from music_assistant_models.errors import (
    MediaNotFoundError,
    MusicAssistantError,
)

from music_assistant.constants import EXTERNAL_ID_PROVIDER
from music_assistant.controllers.cache import CacheController
from music_assistant.helpers.external_ids import external_item_id
from music_assistant.providers.listenbrainz import ListenBrainzProvider
from music_assistant.providers.listenbrainz.playlists import (
    ListenBrainzPlaylistManager,
)

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant

RECORDING_MBID = "11111111-1111-4111-8111-111111111111"


def _playlist_data(playlist_mbid: str, title: str, source_patch: str) -> dict[str, Any]:
    """Return synthetic ListenBrainz playlist metadata."""
    return {
        "extension": {
            "https://musicbrainz.org/doc/jspf#playlist": {
                "additional_metadata": {"algorithm_metadata": {"source_patch": source_patch}},
                "last_modified_at": f"2026-01-01T00:00:{(hash(playlist_mbid) % 60):02d}.000000+00:00",
            }
        },
        "identifier": f"https://listenbrainz.org/playlist/{playlist_mbid}",
        "title": title,
    }


def _manager() -> ListenBrainzPlaylistManager:
    """Return a playlist manager backed by synthetic provider dependencies."""
    provider = ListenBrainzProvider.__new__(ListenBrainzProvider)
    provider.manifest = cast("ProviderManifest", MagicMock(domain="listenbrainz"))
    provider.config = cast(
        "ProviderConfig",
        MagicMock(instance_id="listenbrainz--test", name=None),
    )
    api = MagicMock()
    api.get_createdfor_playlists = AsyncMock()
    api.get_playlist = AsyncMock()
    provider.api = api
    provider.logger = logging.getLogger("listenbrainz")

    mass = MagicMock()
    mass.music.tracks.get_item_by_external_id = AsyncMock(return_value=None)
    mass.music.tracks.store_listed_external_item = AsyncMock()
    mass.music.tracks.search = AsyncMock(return_value=[])
    mass.music.providers = []  # no streaming providers by default
    mass.cache.get = AsyncMock(return_value=None)
    mass.cache.set = AsyncMock()
    provider.mass = cast("MusicAssistant", mass)
    return ListenBrainzPlaylistManager(provider)


def _track_data() -> dict[str, Any]:
    """Return a fresh synthetic JSPF track entry."""
    return {
        "identifier": [f"https://musicbrainz.org/recording/{RECORDING_MBID}"],
        "title": "Synthetic Track",
        "duration": 120000,
        "album": "Synthetic Album",
        "extension": {
            "https://musicbrainz.org/doc/jspf#track": {"additional_metadata": {"artists": []}}
        },
    }


@pytest.mark.parametrize(
    ("yearly_enabled", "expected_names"),
    [
        (False, ["Synthetic Playlist One", "Synthetic Playlist Two"]),
        (
            True,
            [
                "Synthetic Playlist One",
                "Synthetic Playlist Two",
                "Synthetic Playlist Three",
                "Synthetic Playlist Four",
            ],
        ),
    ],
)
async def test_refresh_only_loads_enabled_playlist_types(
    yearly_enabled: bool, expected_names: list[str]
) -> None:
    """Disabled playlist types are filtered before their tracks are requested."""
    manager = _manager()
    manager.provider.username = "synthetic-user"
    playlist_data = [
        _playlist_data(
            "11111111-1111-4111-8111-111111111111",
            "Synthetic Playlist One",
            "weekly-jams",
        ),
        _playlist_data(
            "22222222-2222-4222-8222-222222222222",
            "Synthetic Playlist Two",
            "weekly-exploration",
        ),
        _playlist_data(
            "33333333-3333-4333-8333-333333333333",
            "Synthetic Playlist Three",
            "top-missed-recordings-of-2025",
        ),
        _playlist_data(
            "44444444-4444-4444-8444-444444444444",
            "Synthetic Playlist Four",
            "top-discoveries-of-2025",
        ),
    ]
    get_createdfor_playlists = cast("AsyncMock", manager.api.get_createdfor_playlists)
    get_createdfor_playlists.return_value = playlist_data
    get_playlist = cast("AsyncMock", manager.api.get_playlist)
    get_playlist.return_value = {"track": []}
    enabled = {
        "enable_weekly_jams": True,
        "enable_weekly_exploration": True,
        "enable_yearly_missed_tracks": yearly_enabled,
        "enable_yearly_top_discoveries": yearly_enabled,
    }
    cast("MagicMock", manager.provider.config.get_value).side_effect = lambda key, default=None: (
        enabled.get(key, default)
    )

    await manager.refresh()

    assert [playlist.name for playlist in await manager.browse()] == expected_names
    assert [request.args[0] for request in get_playlist.await_args_list] == [
        "11111111-1111-4111-8111-111111111111",
        "22222222-2222-4222-8222-222222222222",
        "33333333-3333-4333-8333-333333333333",
        "44444444-4444-4444-8444-444444444444",
    ][: len(expected_names)]


@pytest.mark.parametrize("later_playlist_fails", [False, True])
async def test_refresh_publishes_completed_playlist_before_loading_next(
    later_playlist_fails: bool,
) -> None:
    """A completed playlist is readable even while a later playlist loads or fails."""
    manager = _manager()
    manager.provider.username = "synthetic-user"
    cast("MagicMock", manager.provider.config.get_value).return_value = True
    first_id = "11111111-1111-4111-8111-111111111111"
    second_id = "22222222-2222-4222-8222-222222222222"
    cast("AsyncMock", manager.api.get_createdfor_playlists).return_value = [
        _playlist_data(first_id, "Synthetic Playlist One", "weekly-jams"),
        _playlist_data(second_id, "Synthetic Playlist Two", "weekly-exploration"),
    ]
    raw_track = _track_data()

    async def get_playlist(playlist_id: str) -> dict[str, Any]:
        if playlist_id == first_id:
            return {"track": [raw_track]}
        assert (await manager.get_playlist(first_id)).name == "Synthetic Playlist One"
        tracks = await manager.get_playlist_tracks(first_id)
        assert len(tracks) == 1
        assert tracks[0].item_id == external_item_id(ExternalID.MB_RECORDING, RECORDING_MBID)
        assert tracks[0].position == 1
        assert [playlist.item_id for playlist in await manager.browse()] == [first_id]
        if later_playlist_fails:
            raise MusicAssistantError("Synthetic playlist fetch failure")
        return {"track": []}

    cast("AsyncMock", manager.api.get_playlist).side_effect = get_playlist
    if later_playlist_fails:
        with pytest.raises(MusicAssistantError, match="Synthetic playlist fetch failure"):
            await manager.refresh()
        cast("AsyncMock", manager.provider.mass.cache.set).assert_not_awaited()
    else:
        await manager.refresh()
        cast("AsyncMock", manager.provider.mass.cache.set).assert_awaited_once()

    assert (await manager.get_playlist(first_id)).name == "Synthetic Playlist One"
    assert len(await manager.get_playlist_tracks(first_id)) == 1


async def test_playlist_lists_all_source_entries_without_resolving() -> None:
    """Listing preserves source order and duplicates without asking music services."""
    manager = _manager()
    manager.provider.username = "synthetic-user"
    cast("MagicMock", manager.provider.config.get_value).return_value = True
    cast("AsyncMock", manager.api.get_createdfor_playlists).return_value = [
        _playlist_data(RECORDING_MBID, "Synthetic Playlist", "weekly-jams")
    ]
    second = _track_data()
    second["identifier"] = [
        "https://musicbrainz.org/recording/22222222-2222-4222-8222-222222222222"
    ]
    second["title"] = "Synthetic Second Track"
    cast("AsyncMock", manager.api.get_playlist).return_value = {
        "track": [_track_data(), second, _track_data()]
    }

    await manager.refresh()
    tracks = await manager.get_playlist_tracks(RECORDING_MBID)

    assert [track.name for track in tracks] == [
        "Synthetic Track",
        "Synthetic Second Track",
        "Synthetic Track",
    ]
    assert [track.position for track in tracks] == [1, 2, 3]
    assert all(
        track.provider == EXTERNAL_ID_PROVIDER and not track.provider_mappings for track in tracks
    )
    assert tracks[0] is not tracks[2]
    cast(
        "AsyncMock", manager.provider.mass.music.tracks.get_item_by_external_id
    ).assert_not_awaited()
    cast("AsyncMock", manager.provider.mass.music.tracks.search).assert_not_awaited()
    store = cast("AsyncMock", manager.provider.mass.music.tracks.store_listed_external_item)
    assert [call.args[0].name for call in store.await_args_list] == [track.name for track in tracks]
    assert all(call.args[0].position is None for call in store.await_args_list)
    assert await manager.get_playlist_tracks(RECORDING_MBID, page=1) == []


async def test_failed_fetch_does_not_publish_source_version() -> None:
    """A failed playlist rebuild retains its old snapshot and refetches the unbuilt version."""
    manager = _manager()
    manager.provider.username = "synthetic-user"
    cast("MagicMock", manager.provider.config.get_value).return_value = True
    playlist_id = "11111111-1111-4111-8111-111111111111"
    metadata = _playlist_data(playlist_id, "Synthetic Original", "weekly-jams")
    cast("AsyncMock", manager.api.get_createdfor_playlists).return_value = [metadata]
    get_playlist = cast("AsyncMock", manager.api.get_playlist)
    get_playlist.return_value = {"track": []}
    await manager.refresh()
    original = await manager.get_playlist(playlist_id)

    metadata["title"] = "Synthetic Replacement"
    metadata["extension"]["https://musicbrainz.org/doc/jspf#playlist"]["last_modified_at"] = (
        "2026-06-01T12:00:00.000000+00:00"
    )

    async def failed_fetch(_playlist_id: str) -> dict[str, Any]:
        assert await manager.get_playlist(playlist_id) is original
        assert await manager.get_playlist_tracks(playlist_id) == []
        raise MusicAssistantError("Synthetic failure")

    get_playlist.side_effect = failed_fetch
    with pytest.raises(MusicAssistantError, match="Synthetic failure"):
        await manager.refresh()

    assert await manager.get_playlist(playlist_id) is original
    assert await manager.get_playlist_tracks(playlist_id) == []
    get_playlist.reset_mock()
    get_playlist.side_effect = None
    get_playlist.return_value = {"track": [_track_data()]}
    await manager.refresh()

    get_playlist.assert_awaited_once_with(playlist_id)
    assert (await manager.get_playlist(playlist_id)).name == "Synthetic Replacement"
    assert len(await manager.get_playlist_tracks(playlist_id)) == 1


async def test_published_playlists_survive_manager_restart(mass_minimal: MusicAssistant) -> None:
    """A fresh manager serves persisted playlist metadata and ordered tracks without API calls."""
    await mass_minimal.cache._setup_database()
    manager = _manager()
    manager.provider.username = "synthetic-user"
    cast("Any", manager.provider.mass).cache = mass_minimal.cache
    cast("MagicMock", manager.provider.config.get_value).return_value = True
    playlist_id = "11111111-1111-4111-8111-111111111111"
    cast("AsyncMock", manager.api.get_createdfor_playlists).return_value = [
        _playlist_data(playlist_id, "Synthetic Persisted Playlist", "weekly-jams")
    ]
    raw_track = _track_data()
    cast("AsyncMock", manager.api.get_playlist).return_value = {"track": [raw_track, raw_track]}
    await manager.refresh()

    await mass_minimal.cache.close()
    mass_minimal.cache = CacheController(mass_minimal)
    await mass_minimal.cache._setup_database()
    cast("Any", manager.provider.mass).cache = mass_minimal.cache
    restarted = ListenBrainzPlaylistManager(manager.provider)
    await restarted.load_cache()

    assert [item.name for item in await restarted.browse()] == ["Synthetic Persisted Playlist"]
    assert (await restarted.get_playlist(playlist_id)).to_dict() == (
        await manager.get_playlist(playlist_id)
    ).to_dict()
    tracks = await restarted.get_playlist_tracks(playlist_id)
    assert [track.to_dict() for track in tracks] == [
        track.to_dict() for track in await manager.get_playlist_tracks(playlist_id)
    ]
    assert [track.position for track in tracks] == [1, 2]
    assert all(track.provider == EXTERNAL_ID_PROVIDER for track in tracks)
    assert await restarted.get_playlist_tracks(playlist_id, page=1) == []
    get_playlist = cast("AsyncMock", manager.api.get_playlist)
    get_playlist.reset_mock()
    await restarted.refresh()
    get_playlist.assert_not_awaited()
    assert len(await restarted.get_playlist_tracks(playlist_id)) == 2


@pytest.mark.parametrize("changed_scope", ["username", "instance"])
async def test_snapshot_cache_is_scoped_to_provider_configuration(
    mass_minimal: MusicAssistant, changed_scope: str
) -> None:
    """Snapshots from another account or instance are not restored."""
    await mass_minimal.cache._setup_database()
    manager = _manager()
    manager.provider.username = "synthetic-user"
    cast("Any", manager.provider.mass).cache = mass_minimal.cache
    cast("MagicMock", manager.provider.config.get_value).return_value = True
    cast("AsyncMock", manager.api.get_createdfor_playlists).return_value = [
        _playlist_data(RECORDING_MBID, "Synthetic Playlist", "weekly-jams")
    ]
    cast("AsyncMock", manager.api.get_playlist).return_value = {"track": []}
    await manager.refresh()

    if changed_scope == "username":
        manager.provider.username = "another-synthetic-user"
    else:
        manager.provider.config.instance_id = "listenbrainz--other-test"
    restarted = ListenBrainzPlaylistManager(manager.provider)
    await restarted.load_cache()

    assert await restarted.browse() == []


@pytest.mark.parametrize(
    ("source_patch", "disabled_option"),
    [
        ("weekly-exploration", "enable_weekly_exploration"),
        ("top-missed-recordings-of-2025", "enable_yearly_missed_tracks"),
        ("top-discoveries-of-2025", "enable_yearly_top_discoveries"),
    ],
)
async def test_cached_playlists_are_filtered_without_invalidating_enabled_playlists(
    mass_minimal: MusicAssistant,
    source_patch: str,
    disabled_option: str,
) -> None:
    """Disabling one type restores other cached playlists without fetching or resolving."""
    await mass_minimal.cache._setup_database()
    manager = _manager()
    manager.provider.username = "synthetic-user"
    cast("Any", manager.provider.mass).cache = mass_minimal.cache
    config = cast("MagicMock", manager.provider.config.get_value)
    config.return_value = True
    first_id = "11111111-1111-4111-8111-111111111111"
    second_id = "22222222-2222-4222-8222-222222222222"
    cast("AsyncMock", manager.api.get_createdfor_playlists).return_value = [
        _playlist_data(first_id, "Synthetic Jams", "weekly-jams"),
        _playlist_data(second_id, "Synthetic Disabled Playlist", source_patch),
    ]
    cast("AsyncMock", manager.api.get_playlist).return_value = {"track": []}
    await manager.refresh()
    get_createdfor = cast("AsyncMock", manager.api.get_createdfor_playlists)
    get_playlist = cast("AsyncMock", manager.api.get_playlist)
    get_createdfor.reset_mock()
    get_playlist.reset_mock()
    config.side_effect = lambda key, _default=None: key != disabled_option

    restarted = ListenBrainzPlaylistManager(manager.provider)
    await restarted.load_cache()

    assert [playlist.item_id for playlist in await restarted.browse()] == [first_id]
    assert (await restarted.get_playlist(first_id)).name == "Synthetic Jams"
    with pytest.raises(MediaNotFoundError):
        await restarted.get_playlist(second_id)
    assert await restarted.get_playlist_tracks(second_id) == []
    get_createdfor.assert_not_awaited()
    get_playlist.assert_not_awaited()


async def test_removed_playlists_are_not_restored_from_persistent_cache(
    mass_minimal: MusicAssistant,
) -> None:
    """An empty successful refresh replaces the persisted snapshot too."""
    await mass_minimal.cache._setup_database()
    manager = _manager()
    manager.provider.username = "synthetic-user"
    cast("Any", manager.provider.mass).cache = mass_minimal.cache
    cast("MagicMock", manager.provider.config.get_value).return_value = True
    get_createdfor = cast("AsyncMock", manager.api.get_createdfor_playlists)
    get_createdfor.return_value = [
        _playlist_data(RECORDING_MBID, "Synthetic Playlist", "weekly-jams")
    ]
    cast("AsyncMock", manager.api.get_playlist).return_value = {"track": []}
    await manager.refresh()
    assert len(await manager.browse()) == 1

    get_createdfor.return_value = []
    await manager.refresh()
    restarted = ListenBrainzPlaylistManager(manager.provider)
    await restarted.load_cache()

    assert await restarted.browse() == []
    with pytest.raises(MediaNotFoundError):
        await restarted.get_playlist(RECORDING_MBID)
    assert await restarted.get_playlist_tracks(RECORDING_MBID) == []


@pytest.mark.parametrize(
    "cached",
    [
        None,
        {},
        [{}],
        [{"playlist": {}, "tracks": [], "source_patch": "weekly-jams"}],
    ],
)
async def test_missing_or_invalid_snapshot_cache_is_ignored(cached: Any) -> None:
    """Missing or obsolete cached data does not prevent normal provider startup."""
    manager = _manager()
    manager.provider.username = "synthetic-user"
    cast("AsyncMock", manager.provider.mass.cache.get).return_value = cached

    await manager.load_cache()

    assert await manager.browse() == []
    cast("AsyncMock", manager.api.get_createdfor_playlists).assert_not_awaited()
    cast("AsyncMock", manager.api.get_playlist).assert_not_awaited()


async def test_refresh_keeps_one_playlist_per_id_in_source_order() -> None:
    """Repeated IDs update metadata without duplicating or moving the playlist."""
    manager = _manager()
    manager.provider.username = "synthetic-user"
    cast("MagicMock", manager.provider.config.get_value).return_value = True
    first_id = "11111111-1111-4111-8111-111111111111"
    second_id = "22222222-2222-4222-8222-222222222222"
    get_createdfor = cast("AsyncMock", manager.api.get_createdfor_playlists)
    get_createdfor.return_value = [
        _playlist_data(first_id, "Synthetic Original", "weekly-jams"),
        _playlist_data(second_id, "Synthetic Second", "weekly-exploration"),
        _playlist_data(first_id, "Synthetic Replacement", "weekly-jams"),
    ]
    cast("AsyncMock", manager.api.get_playlist).return_value = {"track": []}

    await manager.refresh()

    assert [playlist.item_id for playlist in await manager.browse()] == [first_id, second_id]
    assert (await manager.get_playlist(first_id)).name == "Synthetic Replacement"

    get_createdfor.return_value = [
        _playlist_data(second_id, "Synthetic Second", "weekly-exploration"),
        _playlist_data(first_id, "Synthetic Replacement", "weekly-jams"),
    ]
    await manager.refresh()

    assert [playlist.item_id for playlist in await manager.browse()] == [second_id, first_id]


async def test_unchanged_playlists_skip_get_playlist_on_refresh() -> None:
    """A second refresh with the same modification times skips per-playlist fetches."""
    manager = _manager()
    manager.provider.username = "synthetic-user"
    cast("MagicMock", manager.provider.config.get_value).return_value = True
    playlist_data = [
        _playlist_data("11111111-1111-4111-8111-111111111111", "Playlist One", "weekly-jams"),
        _playlist_data(
            "22222222-2222-4222-8222-222222222222", "Playlist Two", "weekly-exploration"
        ),
    ]
    cast("AsyncMock", manager.api.get_createdfor_playlists).return_value = playlist_data
    cast("AsyncMock", manager.api.get_playlist).return_value = {"track": []}

    await manager.refresh()
    get_playlist = cast("AsyncMock", manager.api.get_playlist)
    assert get_playlist.await_count == 2

    # Second refresh: same last_modified_at → should skip get_playlist entirely
    get_playlist.reset_mock()
    await manager.refresh()
    get_playlist.assert_not_awaited()


async def test_changed_playlists_trigger_get_playlist_on_refresh() -> None:
    """A playlist whose modification time changed is re-fetched on the next refresh."""
    manager = _manager()
    manager.provider.username = "synthetic-user"
    cast("MagicMock", manager.provider.config.get_value).return_value = True

    playlist_mbid = "11111111-1111-4111-8111-111111111111"
    first_page = [
        _playlist_data(playlist_mbid, "Playlist One", "weekly-jams"),
    ]
    cast("AsyncMock", manager.api.get_createdfor_playlists).return_value = first_page
    cast("AsyncMock", manager.api.get_playlist).return_value = {"track": []}

    await manager.refresh()
    get_playlist = cast("AsyncMock", manager.api.get_playlist)

    # Second refresh: last_modified_at changed → must re-fetch
    changed_page = [
        _playlist_data(playlist_mbid, "Playlist One", "weekly-jams"),
    ]
    # Override the last_modified_at to something different
    ext_key = "https://musicbrainz.org/doc/jspf#playlist"
    changed_page[0]["extension"][ext_key]["last_modified_at"] = "2026-06-01T12:00:00.000000+00:00"
    cast("AsyncMock", manager.api.get_createdfor_playlists).return_value = changed_page

    get_playlist.reset_mock()
    await manager.refresh()
    get_playlist.assert_awaited_once_with(playlist_mbid)


async def test_removed_playlists_are_evicted_from_cache() -> None:
    """Playlists that disappear from the created-for list are evicted on refresh."""
    manager = _manager()
    manager.provider.username = "synthetic-user"
    cast("MagicMock", manager.provider.config.get_value).return_value = True

    mbid_one = "11111111-1111-4111-8111-111111111111"
    mbid_two = "22222222-2222-4222-8222-222222222222"

    # First refresh: two playlists
    cast("AsyncMock", manager.api.get_createdfor_playlists).return_value = [
        _playlist_data(mbid_one, "Playlist One", "weekly-jams"),
        _playlist_data(mbid_two, "Playlist Two", "weekly-exploration"),
    ]
    cast("AsyncMock", manager.api.get_playlist).return_value = {"track": []}
    await manager.refresh()

    assert manager._playlists[mbid_one].tracks == ()
    assert manager._playlists[mbid_two].tracks == ()

    # Second refresh: playlist two is gone
    cast("AsyncMock", manager.api.get_createdfor_playlists).return_value = [
        _playlist_data(mbid_one, "Playlist One", "weekly-jams"),
    ]
    await manager.refresh()

    assert mbid_one in manager._playlists
    assert mbid_two not in manager._playlists
