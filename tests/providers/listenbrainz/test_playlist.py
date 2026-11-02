"""Tests for ListenBrainz playlist resolution."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import ExternalID, MediaType, ProviderFeature
from music_assistant_models.errors import MediaNotFoundError, MusicAssistantError
from music_assistant_models.media_items import ProviderMapping, Track

from music_assistant.providers.listenbrainz import ListenBrainzProvider
from music_assistant.providers.listenbrainz.playlist import ListenBrainzPlaylistManager

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

    assert mbid_one in manager._raw_tracks
    assert mbid_two in manager._raw_tracks

    # Second refresh: playlist two is gone
    cast("AsyncMock", manager.api.get_createdfor_playlists).return_value = [
        _playlist_data(mbid_one, "Playlist One", "weekly-jams"),
    ]
    await manager.refresh()

    assert mbid_one in manager._raw_tracks
    assert mbid_two not in manager._raw_tracks
    assert mbid_two not in manager._playlist_modified_at


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
    # Ensure search is a proper AsyncMock so we can assert it was not called
    cast("Any", manager.provider.mass.music).search = AsyncMock()

    with (
        patch(
            "music_assistant.providers.listenbrainz.playlist.relation_urls",
            return_value=["https://synthetic.invalid/track/synthetic-track-id"],
        ) as get_relation_urls,
        patch(
            "music_assistant.providers.listenbrainz.playlist.provider_mappings_from_urls",
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
    cast("AsyncMock", manager.provider.mass.music.search).assert_not_awaited()


async def test_track_without_musicbrainz_provider_mapping_stays_unresolved() -> None:
    """A recording without a link to a loaded provider remains eligible for a later retry."""
    manager = _manager()
    musicbrainz = MagicMock()
    musicbrainz.get_recording_details = AsyncMock(return_value=MagicMock(relations=[]))
    cast("MagicMock", manager.provider.mass.get_provider).return_value = musicbrainz

    relation_url = "https://catalog.synthetic.invalid/track/synthetic-track-id"
    with (
        patch(
            "music_assistant.providers.listenbrainz.playlist.relation_urls",
            return_value=[relation_url],
        ),
        patch(
            "music_assistant.providers.listenbrainz.playlist.provider_mappings_from_urls",
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
        "music_assistant.providers.listenbrainz.playlist.provider_mappings_from_urls",
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


async def test_musicbrainz_link_removed_by_provider_falls_through_to_search() -> None:
    """When the linked provider no longer has the item, resolution falls through to search."""
    manager = _manager()
    musicbrainz = MagicMock()
    musicbrainz.get_recording_details = AsyncMock(return_value=MagicMock(relations=[MagicMock()]))
    cast("MagicMock", manager.provider.mass.get_provider).return_value = musicbrainz
    mapping = ProviderMapping(
        item_id="synthetic-track-id",
        provider_domain="synthetic_music",
        provider_instance="synthetic_music--test",
    )
    # The linked provider no longer has this item → MusicBrainz path returns None
    cast(
        "AsyncMock", manager.provider.mass.music.tracks.get_provider_item
    ).side_effect = MediaNotFoundError("not found")
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
            "music_assistant.providers.listenbrainz.playlist.relation_urls",
            return_value=["https://synthetic.invalid/track/synthetic-track-id"],
        ),
        patch(
            "music_assistant.providers.listenbrainz.playlist.provider_mappings_from_urls",
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


async def test_musicbrainz_link_provider_error_falls_through_to_search() -> None:
    """A MusicAssistantError from the linked provider falls through to name-based search."""
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
    ).side_effect = MusicAssistantError("provider unavailable")
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
            "music_assistant.providers.listenbrainz.playlist.relation_urls",
            return_value=["https://synthetic.invalid/track/synthetic-track-id"],
        ),
        patch(
            "music_assistant.providers.listenbrainz.playlist.provider_mappings_from_urls",
            AsyncMock(return_value=[mapping]),
        ),
    ):
        resolved = await manager._get_or_resolve_track(_track())

    assert resolved is not None
    assert resolved.item_id == "synthetic-search-id"
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
        "music_assistant.providers.listenbrainz.playlist.provider_mappings_from_urls",
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
        "music_assistant.providers.listenbrainz.playlist.provider_mappings_from_urls",
        AsyncMock(return_value=[]),
    ):
        resolved = await manager._get_or_resolve_track(_track())

    assert resolved is None
    assert RECORDING_MBID not in manager._resolved_tracks
