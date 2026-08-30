"""Tests that imported album tracks are filed under the album they came from."""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

from music_assistant_models.enums import ProviderType
from music_assistant_models.media_items import (
    Album,
    AudioFormat,
    ItemMapping,
    ProviderMapping,
    Track,
)

from music_assistant.constants import CONF_ENTRY_LIBRARY_SYNC_ALBUM_TRACKS, CONF_LOG_LEVEL
from music_assistant.controllers.music.media.base import TrackSyncDetails
from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from collections.abc import Sequence

ALBUM_ID = "album_1"


class AlbumTracksProvider(MusicProvider):
    """Provider that leaves the parent album off the tracks in its album listing."""

    album_tracks: list[Track]
    album: Album
    get_album_calls: int = 0

    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """Return the configured album tracks."""
        return self.album_tracks

    async def get_album(self, prov_album_id: str) -> Album:
        """Return the configured album and record the call."""
        self.get_album_calls += 1
        return self.album


def _provider_mapping(item_id: str) -> ProviderMapping:
    """Return a provider mapping for the test provider instance."""
    return ProviderMapping(
        item_id=item_id,
        provider_domain="test",
        provider_instance="test--1",
        audio_format=AudioFormat(),
    )


def _build_album() -> Album:
    """Return the provider album the imported tracks belong to."""
    return Album(
        item_id=ALBUM_ID,
        provider="test",
        name="Album One",
        provider_mappings={_provider_mapping(ALBUM_ID)},
    )


def _build_tracks(count: int = 3) -> list[Track]:
    """Return provider tracks without an album."""
    return [
        Track(
            item_id=f"track_{index}",
            provider="test",
            name=f"Track {index}",
            provider_mappings={_provider_mapping(f"track_{index}")},
        )
        for index in range(1, count + 1)
    ]


def _build_mass(sync_details: TrackSyncDetails | None = None) -> MagicMock:
    """Return a mocked mass whose track controller records every imported track."""
    mass = MagicMock()
    tracks = mass.music.tracks
    tracks.get_library_item_sync_details = AsyncMock(return_value=sync_details)

    library_track = MagicMock()
    library_track.item_id = 1
    tracks.add_item_to_library = AsyncMock(return_value=library_track)
    tracks.update_item_in_library = AsyncMock(return_value=library_track)
    mass.music.genres.sync_media_item_genres = AsyncMock()
    return mass


def _build_provider(mass: MagicMock) -> AlbumTracksProvider:
    """Return a provider instance wired to the given (mocked) mass."""
    manifest = MagicMock()
    manifest.type = ProviderType.MUSIC
    manifest.domain = "test"
    config = MagicMock()
    config.instance_id = "test--1"
    config.domain = "test"
    values = {
        CONF_LOG_LEVEL: "GLOBAL",
        CONF_ENTRY_LIBRARY_SYNC_ALBUM_TRACKS.key: True,
    }
    config.get_value.side_effect = lambda key, default=None: values.get(key, default)
    provider = AlbumTracksProvider(mass, manifest, config)
    provider.album = _build_album()
    provider.album_tracks = _build_tracks()
    return provider


def _added_tracks(mass: MagicMock) -> Sequence[Track]:
    """Return the tracks that were added to the library."""
    return [call.args[0] for call in mass.music.tracks.add_item_to_library.await_args_list]


async def test_import_attaches_the_parent_album() -> None:
    """A track whose provider listing omits the album is still filed under that album."""
    mass = _build_mass()
    provider = _build_provider(mass)

    await provider.import_album_tracks(ALBUM_ID, "Album One", provider.album)

    added = _added_tracks(mass)
    assert len(added) == 3
    for track in added:
        assert track.album is not None
        assert track.album.item_id == ALBUM_ID
    # the album was supplied by the caller, so it was not fetched again
    assert provider.get_album_calls == 0


async def test_album_is_resolved_once_when_not_supplied() -> None:
    """Without a supplied album the provider is asked for it once, not per track."""
    mass = _build_mass()
    provider = _build_provider(mass)

    await provider.import_album_tracks(ALBUM_ID)

    assert provider.get_album_calls == 1
    assert all(track.album is not None for track in _added_tracks(mass))


async def test_tracks_that_already_have_an_album_keep_it() -> None:
    """Tracks that come in with their own album are left untouched."""
    mass = _build_mass()
    provider = _build_provider(mass)
    own_album = ItemMapping.from_item(
        Album(
            item_id="other_album",
            provider="test",
            name="Other Album",
            provider_mappings={_provider_mapping("other_album")},
        )
    )
    for track in provider.album_tracks:
        track.album = own_album

    await provider.import_album_tracks(ALBUM_ID)

    assert provider.get_album_calls == 0
    assert all(track.album is own_album for track in _added_tracks(mass))


async def test_missing_album_link_is_backfilled() -> None:
    """An earlier import that stored a track without its album is repaired."""
    sync_details = TrackSyncDetails(
        item_id=1,
        favorite=False,
        date_added=datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC),
        provider_mappings=set(),
        has_album=False,
        has_artists=True,
    )
    mass = _build_mass(sync_details)
    provider = _build_provider(mass)

    # the provider mappings already match, so the missing album link is the only trigger
    with patch.object(provider, "_check_provider_mappings", return_value=True):
        await provider.import_album_tracks(ALBUM_ID, "Album One", provider.album)

    assert mass.music.tracks.update_item_in_library.await_count == 3
    for call in mass.music.tracks.update_item_in_library.await_args_list:
        assert call.args[1].album.item_id == ALBUM_ID
