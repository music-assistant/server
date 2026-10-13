"""Tests for the tracks the Local files provider lists for an album in the library."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.media_items import Album, ProviderMapping, Track

from music_assistant.providers.filesystem_local import LocalFileSystemProvider

INSTANCE_ID = "filesystem_local--test"
OTHER_INSTANCE_ID = "filesystem_local--other"


def _mapping(item_id: str, instance: str = INSTANCE_ID) -> ProviderMapping:
    """Build a mapping of a track on a Local files instance."""
    return ProviderMapping(
        item_id=item_id, provider_domain="filesystem_local", provider_instance=instance
    )


def _row(*mappings: ProviderMapping, item_id: str = "1") -> Track:
    """Build a library track with the given copies."""
    return Track(
        item_id=item_id, provider="library", name="Come Together", provider_mappings=set(mappings)
    )


def _provider(album_id: str, rows: list[Track]) -> LocalFileSystemProvider:
    """Build a provider whose library holds the album under the given id, with the given tracks."""
    provider = LocalFileSystemProvider.__new__(LocalFileSystemProvider)
    provider.config = MagicMock()
    provider.config.instance_id = INSTANCE_ID
    provider.mass = MagicMock()
    provider.mass.music.albums.get_library_item_by_prov_id = AsyncMock(
        return_value=Album(
            item_id="7",
            provider="library",
            name="Abbey Road",
            provider_mappings={_mapping(album_id)},
        )
    )
    provider.mass.music.albums.get_library_album_tracks = AsyncMock(return_value=rows)
    return provider


async def _listed(provider: LocalFileSystemProvider, album_id: str) -> list[set[str]]:
    """Return the item ids each listed track of the album carries."""
    return [
        {mapping.item_id for mapping in track.provider_mappings}
        for track in await provider.get_album_tracks(album_id)
    ]


@pytest.mark.parametrize(
    "path",
    [
        "Beatles/Abbey Road (1969)/01.flac",
        "Beatles/Abbey Road (1969)/CD1/01.flac",
        "Beatles/Abbey Road (1969)/Abbey Road.cue::track01",
    ],
    ids=["album folder", "disc folder", "cue sheet"],
)
async def test_a_track_lists_only_its_file_in_the_album_folder(path: str) -> None:
    """The copy of the same recording in another album's folder is no track of this album."""
    row = _row(
        _mapping(path),
        _mapping("Beatles/Abbey Road (2009)/01.flac"),
        _mapping("Beatles/Abbey Road (1969)/01.flac", OTHER_INSTANCE_ID),
        ProviderMapping(item_id="t1", provider_domain="tidal", provider_instance="tidal--a"),
    )
    provider = _provider("Beatles/Abbey Road (1969)", [row])

    assert await _listed(provider, "Beatles/Abbey Road (1969)") == [{path}]
    # the library track itself keeps all of its copies
    assert len(row.provider_mappings) == 4


async def test_a_track_with_its_file_in_another_albums_folder_only_is_left_out() -> None:
    """A track of the album whose only file here sits in another album's folder is not listed."""
    rows = [
        _row(_mapping("Beatles/Abbey Road (1969)/01.flac")),
        # merged with the remaster's file, and missing from this rip
        _row(_mapping("Beatles/Abbey Road (2009)/07.flac"), item_id="7"),
    ]
    provider = _provider("Beatles/Abbey Road (1969)", rows)

    assert await _listed(provider, "Beatles/Abbey Road (1969)") == [
        {"Beatles/Abbey Road (1969)/01.flac"}
    ]


async def test_an_album_id_that_is_no_folder_keeps_the_tracks_files() -> None:
    """An album id made up of the artist and album name keeps the tracks' files here."""
    row = _row(
        _mapping("Various/01 Come Together.mp3"),
        _mapping("Singles/Come Together.flac"),
        _mapping("Various/01 Come Together.mp3", OTHER_INSTANCE_ID),
    )
    provider = _provider("The Beatles/Abbey Road", [row])

    assert await _listed(provider, "The Beatles/Abbey Road") == [
        {"Various/01 Come Together.mp3", "Singles/Come Together.flac"}
    ]


async def test_a_track_with_no_file_on_this_provider_is_not_listed() -> None:
    """A library track of the album that only another provider has is not this provider's."""
    row = _row(_mapping("Beatles/Abbey Road (1969)/01.flac", OTHER_INSTANCE_ID))
    provider = _provider("Beatles/Abbey Road (1969)", [row])

    assert await _listed(provider, "Beatles/Abbey Road (1969)") == []
