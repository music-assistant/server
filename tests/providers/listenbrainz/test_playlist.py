"""Tests for ListenBrainz playlist resolution."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock, patch

from music_assistant_models.enums import ExternalID, MediaType
from music_assistant_models.media_items import ProviderMapping, Track

from music_assistant.providers.listenbrainz import ListenBrainzProvider
from music_assistant.providers.listenbrainz.playlist import ListenBrainzPlaylistManager

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

    from music_assistant.mass import MusicAssistant

RECORDING_MBID = "11111111-1111-4111-8111-111111111111"


def _manager() -> ListenBrainzPlaylistManager:
    """Return a playlist manager backed by synthetic provider dependencies."""
    provider = ListenBrainzProvider.__new__(ListenBrainzProvider)
    provider.manifest = cast("ProviderManifest", MagicMock(domain="listenbrainz"))
    provider.config = cast(
        "ProviderConfig",
        MagicMock(instance_id="listenbrainz--test", name=None),
    )
    provider.api = MagicMock()

    mass = MagicMock()
    mass.music.tracks.get_library_item_by_external_id = AsyncMock(return_value=None)
    mass.music.tracks.search = AsyncMock()
    mass.music.tracks.get_provider_item = AsyncMock()
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


async def test_musicbrainz_links_become_provider_mappings_without_provider_lookup() -> None:
    """MusicBrainz links make a track playable without querying music providers."""
    manager = _manager()
    musicbrainz = MagicMock()
    musicbrainz.get_recording_details = AsyncMock(return_value=MagicMock(relations=[MagicMock()]))
    cast("MagicMock", manager.provider.mass.get_provider).return_value = musicbrainz
    mapping = ProviderMapping(
        item_id="synthetic-track-id",
        provider_domain="synthetic_music",
        provider_instance="synthetic_music--test",
    )

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
    assert resolved.provider_mappings == {mapping}
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
    cast("AsyncMock", manager.provider.mass.music.tracks.search).assert_not_awaited()
    cast("AsyncMock", manager.provider.mass.music.tracks.get_provider_item).assert_not_awaited()


async def test_track_without_musicbrainz_provider_mapping_stays_unresolved() -> None:
    """A recording without a link to a loaded provider remains eligible for a later retry."""
    manager = _manager()
    musicbrainz = MagicMock()
    musicbrainz.get_recording_details = AsyncMock(return_value=MagicMock(relations=[]))
    cast("MagicMock", manager.provider.mass.get_provider).return_value = musicbrainz

    with patch(
        "music_assistant.providers.listenbrainz.playlist.provider_mappings_from_urls",
        AsyncMock(return_value=[]),
    ):
        resolved = await manager._get_or_resolve_track(_track())

    assert resolved is None
    assert RECORDING_MBID not in manager._resolved_tracks
    cast("AsyncMock", manager.provider.mass.cache.set).assert_not_awaited()
    cast("AsyncMock", manager.provider.mass.music.tracks.search).assert_not_awaited()
