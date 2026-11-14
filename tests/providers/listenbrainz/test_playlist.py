"""Tests for ListenBrainz playlist resolution."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import ExternalID, MediaType, ProviderFeature
from music_assistant_models.errors import MediaNotFoundError, MusicAssistantError
from music_assistant_models.media_items import ProviderMapping, Track

from music_assistant.controllers.cache import CacheController
from music_assistant.providers.listenbrainz import ListenBrainzProvider
from music_assistant.providers.listenbrainz.playlists import (
    CACHE_CATEGORY_PLAYLISTS,
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
    mass.music.tracks.get_library_item_by_external_id = AsyncMock(return_value=None)
    mass.music.tracks.search = AsyncMock()
    mass.music.tracks.get_provider_item = AsyncMock()
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


def _track() -> Track:
    """Return a synthetic ListenBrainz track with a recording MBID."""
    return Track(
        item_id=RECORDING_MBID,
        provider="listenbrainz--test",
        name="Synthetic Track",
        provider_mappings=set(),
        external_ids={(ExternalID.MB_RECORDING, RECORDING_MBID)},
    )


def _streaming_provider() -> MagicMock:
    """Return a mock streaming provider that supports track search."""
    prov = MagicMock()
    prov.instance_id = "synthetic_music--test"
    prov.is_streaming_provider = True
    prov.available = True
    prov.supported_features = {ProviderFeature.LIBRARY_TRACKS}
    prov.name = "Synthetic Music"
    return prov


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
    resolved_track = Track(
        item_id="synthetic-track-id",
        provider="synthetic_music--test",
        name="Synthetic Track",
        provider_mappings=set(),
    )

    async def get_playlist(playlist_id: str) -> dict[str, Any]:
        if playlist_id == first_id:
            return {"track": [raw_track]}
        assert (await manager.get_playlist(first_id)).name == "Synthetic Playlist One"
        tracks = await manager.get_playlist_tracks(first_id)
        assert len(tracks) == 1
        assert tracks[0].item_id == resolved_track.item_id
        assert tracks[0].position == 1
        assert [playlist.item_id for playlist in await manager.browse()] == [first_id]
        if later_playlist_fails:
            raise MusicAssistantError("Synthetic playlist fetch failure")
        return {"track": []}

    cast("AsyncMock", manager.api.get_playlist).side_effect = get_playlist
    with patch.object(manager, "_get_or_resolve_track", AsyncMock(return_value=resolved_track)):
        if later_playlist_fails:
            with pytest.raises(MusicAssistantError, match="Synthetic playlist fetch failure"):
                await manager.refresh()
            cast("AsyncMock", manager.provider.mass.cache.set).assert_not_awaited()
        else:
            await manager.refresh()
            cast("AsyncMock", manager.provider.mass.cache.set).assert_awaited_once()

    assert (await manager.get_playlist(first_id)).name == "Synthetic Playlist One"
    assert len(await manager.get_playlist_tracks(first_id)) == 1


async def test_refresh_keeps_previous_playlist_until_replacement_is_resolved() -> None:
    """Refreshing an existing playlist replaces metadata and tracks only when complete."""
    manager = _manager()
    manager.provider.username = "synthetic-user"
    cast("MagicMock", manager.provider.config.get_value).return_value = True
    playlist_id = "11111111-1111-4111-8111-111111111111"
    metadata = _playlist_data(playlist_id, "Synthetic Original", "weekly-jams")
    cast("AsyncMock", manager.api.get_createdfor_playlists).return_value = [metadata]
    cast("AsyncMock", manager.api.get_playlist).return_value = {"track": []}
    await manager.refresh()
    original = await manager.get_playlist(playlist_id)

    metadata["title"] = "Synthetic Replacement"
    raw_track = _track()
    manager._playlists[playlist_id].raw_tracks = (raw_track,)

    async def resolve_track(track: Track) -> Track:
        assert await manager.get_playlist(playlist_id) is original
        assert await manager.get_playlist_tracks(playlist_id) == []
        return track

    with patch.object(manager, "_get_or_resolve_track", AsyncMock(side_effect=resolve_track)):
        await manager.refresh()

    assert (await manager.get_playlist(playlist_id)).name == "Synthetic Replacement"
    assert len(await manager.browse()) == 1
    assert len(await manager.get_playlist_tracks(playlist_id)) == 1


async def test_failed_resolution_does_not_publish_source_version() -> None:
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
    get_playlist.return_value = {"track": [_track_data()]}
    with (
        patch.object(
            manager,
            "_get_or_resolve_track",
            AsyncMock(side_effect=MusicAssistantError("Synthetic failure")),
        ),
        pytest.raises(MusicAssistantError, match="Synthetic failure"),
    ):
        await manager.refresh()

    assert await manager.get_playlist(playlist_id) is original
    assert await manager.get_playlist_tracks(playlist_id) == []
    get_playlist.reset_mock()
    with patch.object(manager, "_get_or_resolve_track", AsyncMock(return_value=_track())):
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
    resolved = Track(
        item_id="synthetic-track-id",
        provider="synthetic_music--test",
        name="Synthetic Resolved Track",
        provider_mappings={
            ProviderMapping(
                item_id="synthetic-track-id",
                provider_domain="synthetic_music",
                provider_instance="synthetic_music--test",
            )
        },
        external_ids={(ExternalID.MB_RECORDING, RECORDING_MBID)},
    )
    with patch.object(manager, "_get_or_resolve_track", AsyncMock(side_effect=[None, resolved])):
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
    assert tracks[0].position == 2
    assert await restarted.get_playlist_tracks(playlist_id, page=1) == []
    restored_state = restarted._playlists[playlist_id]
    original_state = manager._playlists[playlist_id]
    assert restored_state.raw_tracks is not None
    assert original_state.raw_tracks is not None
    assert [track.to_dict() for track in restored_state.raw_tracks] == [
        track.to_dict() for track in original_state.raw_tracks
    ]
    assert restored_state.modified_at == original_state.modified_at
    assert restored_state.source_patch == original_state.source_patch

    get_playlist = cast("AsyncMock", manager.api.get_playlist)
    get_playlist.reset_mock()
    with patch.object(
        restarted, "_get_or_resolve_track", AsyncMock(return_value=resolved)
    ) as resolve:
        await restarted.refresh()
    assert resolve.await_count == 2
    get_playlist.assert_not_awaited()
    assert len(await restarted.get_playlist_tracks(playlist_id)) == 2


@pytest.mark.parametrize(
    ("snapshot_kind", "source_changed", "expect_fetch"),
    [
        ("legacy", False, True),
        ("unfetched", False, True),
        ("empty", False, False),
        ("empty", True, True),
    ],
)
async def test_restored_source_state_controls_playlist_refetch(
    mass_minimal: MusicAssistant, snapshot_kind: str, source_changed: bool, expect_fetch: bool
) -> None:
    """Restore distinguishes unfetched and empty sources, and detects changed versions."""
    await mass_minimal.cache._setup_database()
    manager = _manager()
    manager.provider.username = "synthetic-user"
    cast("Any", manager.provider.mass).cache = mass_minimal.cache
    cast("MagicMock", manager.provider.config.get_value).return_value = True
    playlist_id = "11111111-1111-4111-8111-111111111111"
    metadata = _playlist_data(playlist_id, "Synthetic Playlist", "weekly-jams")
    cast("AsyncMock", manager.api.get_createdfor_playlists).return_value = [metadata]
    get_playlist = cast("AsyncMock", manager.api.get_playlist)
    get_playlist.return_value = {"track": []}
    await manager.refresh()

    cached = await mass_minimal.cache.get(
        key="playlists", provider=manager.provider.instance_id, category=CACHE_CATEGORY_PLAYLISTS
    )
    if snapshot_kind == "legacy":
        cached[0].pop("raw_tracks")
        cached[0].pop("modified_at")
    elif snapshot_kind == "unfetched":
        cached[0]["raw_tracks"] = None
    await mass_minimal.cache.set(
        key="playlists",
        data=cached,
        provider=manager.provider.instance_id,
        category=CACHE_CATEGORY_PLAYLISTS,
        checksum=manager.provider.username,
    )

    restarted = ListenBrainzPlaylistManager(manager.provider)
    await restarted.load_cache()
    assert (await restarted.get_playlist(playlist_id)).name == "Synthetic Playlist"
    assert restarted._playlists[playlist_id].raw_tracks == (
        () if snapshot_kind == "empty" else None
    )
    if snapshot_kind == "legacy":
        assert restarted._playlists[playlist_id].modified_at is None
    if source_changed:
        metadata["extension"]["https://musicbrainz.org/doc/jspf#playlist"]["last_modified_at"] = (
            "2026-06-01T12:00:00.000000+00:00"
        )
    get_playlist.reset_mock()
    await restarted.refresh()

    if expect_fetch:
        get_playlist.assert_awaited_once_with(playlist_id)
    else:
        get_playlist.assert_not_awaited()


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
    with patch.object(restarted, "_get_or_resolve_track", AsyncMock()) as resolve:
        await restarted.load_cache()
        resolve.assert_not_awaited()

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

    assert manager._playlists[mbid_one].raw_tracks == ()
    assert manager._playlists[mbid_two].raw_tracks == ()

    # Second refresh: playlist two is gone
    cast("AsyncMock", manager.api.get_createdfor_playlists).return_value = [
        _playlist_data(mbid_one, "Playlist One", "weekly-jams"),
    ]
    await manager.refresh()

    assert mbid_one in manager._playlists
    assert mbid_two not in manager._playlists


async def test_musicbrainz_links_resolve_via_provider_item() -> None:
    """MusicBrainz links fetch the linked provider's actual playable track."""
    manager = _manager()
    musicbrainz = MagicMock()
    musicbrainz.get_recording_details = AsyncMock(return_value=MagicMock(relations=[MagicMock()]))
    cast("MagicMock", manager.provider.mass.get_provider).return_value = musicbrainz
    mapping = ProviderMapping(
        item_id="synthetic-track-id",
        provider_domain="synthetic_music",
        provider_instance="synthetic_music--test",
    )
    provider_track = Track(
        item_id="synthetic-track-id",
        provider="synthetic_music--test",
        name="Synthetic Track",
        provider_mappings=set(),
        external_ids=set(),
    )
    cast("Any", manager.provider.mass.music.tracks).get_provider_item.return_value = provider_track
    cast("Any", manager.provider.mass.music).providers = [_streaming_provider()]

    with (
        patch(
            "music_assistant.providers.listenbrainz.playlists.relation_urls",
            return_value=["https://synthetic.invalid/track/synthetic-track-id"],
        ) as get_relation_urls,
        patch(
            "music_assistant.providers.listenbrainz.playlists.provider_mappings_from_urls",
            AsyncMock(return_value=[mapping]),
        ) as get_mappings,
    ):
        resolved = await manager._get_or_resolve_track(_track())

    assert resolved is not None
    assert resolved.provider == "synthetic_music--test"
    assert resolved.item_id == "synthetic-track-id"
    assert resolved.provider_mappings == {mapping}
    assert resolved.get_external_id(ExternalID.MB_RECORDING) == RECORDING_MBID
    musicbrainz.get_recording_details.assert_awaited_once_with(RECORDING_MBID)
    get_relation_urls.assert_called_once_with(
        musicbrainz.get_recording_details.return_value.relations
    )
    get_mappings.assert_awaited_once_with(
        manager.provider.mass,
        ["https://synthetic.invalid/track/synthetic-track-id"],
        MediaType.TRACK,
        exclude_domains=set(),
    )
    cast("Any", manager.provider.mass.music.tracks).get_provider_item.assert_awaited_once_with(
        "synthetic-track-id",
        "synthetic_music--test",
        allow_fallback=False,
        strict_provider_instance=True,
    )
    cast("AsyncMock", manager.provider.mass.music.tracks.search).assert_not_awaited()


async def test_track_without_musicbrainz_provider_mapping_stays_unresolved() -> None:
    """A recording without a link to a loaded provider remains eligible for a later retry."""
    manager = _manager()
    musicbrainz = MagicMock()
    musicbrainz.get_recording_details = AsyncMock(return_value=MagicMock(relations=[]))
    cast("MagicMock", manager.provider.mass.get_provider).return_value = musicbrainz

    relation_url = "https://catalog.synthetic.invalid/track/synthetic-track-id"
    with (
        patch(
            "music_assistant.providers.listenbrainz.playlists.relation_urls",
            return_value=[relation_url],
        ),
        patch(
            "music_assistant.providers.listenbrainz.playlists.provider_mappings_from_urls",
            AsyncMock(return_value=[]),
        ),
    ):
        resolved = await manager._get_or_resolve_track(_track())

    assert resolved is None
    assert RECORDING_MBID not in manager._resolved_tracks
    cast("AsyncMock", manager.provider.mass.cache.set).assert_not_awaited()
    cast("AsyncMock", manager.provider.mass.music.tracks.search).assert_not_awaited()


async def test_resolved_track_from_search_carries_recording_mbid() -> None:
    """A track found via streaming-service search retains the recording MBID in external_ids."""
    manager = _manager()
    cast("Any", manager.provider.mass.music).providers = [_streaming_provider()]
    musicbrainz = MagicMock()
    musicbrainz.get_recording_details = AsyncMock(return_value=MagicMock(relations=[]))
    cast("MagicMock", manager.provider.mass.get_provider).return_value = musicbrainz

    streaming_track = Track(
        item_id="synthetic-streaming-id",
        provider="synthetic_music--test",
        name="Synthetic Track",
        provider_mappings=set(),
        external_ids=set(),
    )
    cast("Any", manager.provider.mass.music.tracks).search = AsyncMock(
        return_value=[streaming_track]
    )

    with patch(
        "music_assistant.providers.listenbrainz.playlists.provider_mappings_from_urls",
        AsyncMock(return_value=[]),
    ):
        resolved = await manager._get_or_resolve_track(_track())

    assert resolved is not None
    assert resolved.get_external_id(ExternalID.MB_RECORDING) == RECORDING_MBID


async def test_cached_track_gets_recording_mbid_added() -> None:
    """A track loaded from cache gets the recording MBID added to its external_ids."""
    manager = _manager()
    cached_track = Track(
        item_id="synthetic-cache-id",
        provider="synthetic_music--test",
        name="Synthetic Cache Track",
        provider_mappings=set(),
        external_ids=set(),
    )
    cast("AsyncMock", manager.provider.mass.cache.get).return_value = cached_track

    resolved = await manager._get_or_resolve_track(_track())

    assert resolved is not None
    assert resolved.get_external_id(ExternalID.MB_RECORDING) == RECORDING_MBID


@pytest.mark.parametrize("provider_error", [MediaNotFoundError, MusicAssistantError])
async def test_musicbrainz_link_provider_failure_falls_through_to_search(
    provider_error: type[MusicAssistantError],
) -> None:
    """Missing items and provider failures both fall through to playable search results."""
    manager = _manager()
    musicbrainz = MagicMock()
    musicbrainz.get_recording_details = AsyncMock(return_value=MagicMock(relations=[MagicMock()]))
    cast("MagicMock", manager.provider.mass.get_provider).return_value = musicbrainz
    mapping = ProviderMapping(
        item_id="synthetic-track-id",
        provider_domain="synthetic_music",
        provider_instance="synthetic_music--test",
    )
    cast(
        "AsyncMock", manager.provider.mass.music.tracks.get_provider_item
    ).side_effect = provider_error("Synthetic provider failure")
    # The name-based search still finds a playable match
    search_track = Track(
        item_id="synthetic-search-id",
        provider="synthetic_music--test",
        name="Synthetic Track",
        provider_mappings=set(),
        external_ids=set(),
    )
    cast("Any", manager.provider.mass.music).providers = [_streaming_provider()]
    cast("Any", manager.provider.mass.music.tracks).search = AsyncMock(return_value=[search_track])

    with (
        patch(
            "music_assistant.providers.listenbrainz.playlists.relation_urls",
            return_value=["https://synthetic.invalid/track/synthetic-track-id"],
        ),
        patch(
            "music_assistant.providers.listenbrainz.playlists.provider_mappings_from_urls",
            AsyncMock(return_value=[mapping]),
        ),
    ):
        resolved = await manager._get_or_resolve_track(_track())

    assert resolved is not None
    # The search track is returned, not the MusicBrainz-linked placeholder
    assert resolved.item_id == "synthetic-search-id"
    assert resolved.get_external_id(ExternalID.MB_RECORDING) == RECORDING_MBID
    cast("AsyncMock", manager.provider.mass.music.tracks.get_provider_item).assert_awaited_once()
    cast("Any", manager.provider.mass.music.tracks).search.assert_awaited_once()


async def test_search_skips_non_matching_results() -> None:
    """A search result whose name doesn't match is skipped in favor of a matching one."""
    manager = _manager()
    musicbrainz = MagicMock()
    musicbrainz.get_recording_details = AsyncMock(return_value=MagicMock(relations=[]))
    cast("MagicMock", manager.provider.mass.get_provider).return_value = musicbrainz

    # First result is a same-titled cover by a different artist with a different title;
    # second is the real match. With no artist on the source track, name is the only
    # verifier — the first result's name simply doesn't match.
    cover_track = Track(
        item_id="synthetic-cover-id",
        provider="synthetic_music--test",
        name="Different Title",
        provider_mappings=set(),
        external_ids=set(),
    )
    matching_track = Track(
        item_id="synthetic-match-id",
        provider="synthetic_music--test",
        name="Synthetic Track",
        provider_mappings=set(),
        external_ids=set(),
    )
    cast("Any", manager.provider.mass.music).providers = [_streaming_provider()]
    cast("Any", manager.provider.mass.music.tracks).search = AsyncMock(
        return_value=[cover_track, matching_track]
    )

    with patch(
        "music_assistant.providers.listenbrainz.playlists.provider_mappings_from_urls",
        AsyncMock(return_value=[]),
    ):
        resolved = await manager._get_or_resolve_track(_track())

    assert resolved is not None
    assert resolved.item_id == "synthetic-match-id"
    assert resolved.get_external_id(ExternalID.MB_RECORDING) == RECORDING_MBID


async def test_search_with_no_matching_results_returns_none() -> None:
    """When no search result matches by name, resolution returns None."""
    manager = _manager()
    musicbrainz = MagicMock()
    musicbrainz.get_recording_details = AsyncMock(return_value=MagicMock(relations=[]))
    cast("MagicMock", manager.provider.mass.get_provider).return_value = musicbrainz

    nonmatching_track = Track(
        item_id="synthetic-other-id",
        provider="synthetic_music--test",
        name="Completely Different Song",
        provider_mappings=set(),
        external_ids=set(),
    )
    cast("Any", manager.provider.mass.music).providers = [_streaming_provider()]
    cast("Any", manager.provider.mass.music.tracks).search = AsyncMock(
        return_value=[nonmatching_track]
    )

    with patch(
        "music_assistant.providers.listenbrainz.playlists.provider_mappings_from_urls",
        AsyncMock(return_value=[]),
    ):
        resolved = await manager._get_or_resolve_track(_track())

    assert resolved is None
    assert RECORDING_MBID not in manager._resolved_tracks
