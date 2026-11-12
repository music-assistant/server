"""Tests for the album links of a Local files track whose album tag changed or was removed."""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import Album, Artist, ProviderMapping, Track, UniqueList

from music_assistant.constants import DB_TABLE_ALBUM_TRACKS
from music_assistant.mass import MusicAssistant
from music_assistant.providers.filesystem_local import LocalFileSystemProvider

FS_INSTANCE = "filesystem_local--test"
STREAM_INSTANCE = "spotify--test"
TRACK_FILE = "Bert Kaempfert/Black Beauty - Bert Kaempfert.mp3"


def _mapping(item_id: str, instance: str = FS_INSTANCE) -> ProviderMapping:
    """Build a mapping on the given provider instance."""
    domain = "spotify" if instance == STREAM_INSTANCE else "filesystem_local"
    return ProviderMapping(item_id=item_id, provider_domain=domain, provider_instance=instance)


def _artist() -> Artist:
    """Build the artist of the test tracks and albums."""
    return Artist(
        item_id="Bert Kaempfert",
        provider=FS_INSTANCE,
        name="Bert Kaempfert",
        provider_mappings={_mapping("Bert Kaempfert")},
    )


def _album(name: str, *extra: ProviderMapping) -> Album:
    """Build a Local files album named after its folder, optionally on other providers too."""
    return Album(
        item_id=name,
        provider=FS_INSTANCE,
        name=name,
        artists=UniqueList([_artist()]),
        provider_mappings={_mapping(name), *extra},
    )


def _track(album: Album | None, *extra: ProviderMapping) -> Track:
    """Build the track of the test file as parsed from its tags."""
    return Track(
        item_id=TRACK_FILE,
        provider=FS_INSTANCE,
        name="Black Beauty",
        artists=UniqueList([_artist()]),
        album=album,
        provider_mappings={_mapping(TRACK_FILE), *extra},
    )


def _provider(mass: MusicAssistant) -> LocalFileSystemProvider:
    """Build a Local files provider on the given instance."""
    provider = LocalFileSystemProvider.__new__(LocalFileSystemProvider)
    provider.config = MagicMock()
    provider.config.instance_id = FS_INSTANCE
    provider.mass = mass
    provider.logger = logging.getLogger(__name__)
    return provider


async def _linked_albums(mass: MusicAssistant, track: Track) -> set[str]:
    """Return the names of the library albums the library track is linked to."""
    rows = await mass.music.database.get_rows(
        DB_TABLE_ALBUM_TRACKS, {"track_id": int(track.item_id)}
    )
    return {(await mass.music.albums.get_library_item(row["album_id"])).name for row in rows}


async def _rescan(mass: MusicAssistant, rescanned: Track) -> None:
    """Write a changed file's parsed track to the library the way a sync does."""
    prior = await mass.music.tracks.get_library_item_by_prov_id(TRACK_FILE, FS_INSTANCE)
    assert prior is not None
    await mass.music.tracks.add_item_to_library(rescanned, overwrite_existing=True)
    await _provider(mass)._unlink_stale_albums(rescanned, prior)


@pytest.fixture(name="mass")
def mass_fixture(music_mass: MusicAssistant) -> MusicAssistant:
    """Run on a library-only instance with a real database."""
    music_mass.cache.delete = AsyncMock()  # type: ignore[method-assign]
    return music_mass


async def test_removed_album_tag_unlinks_the_old_album(mass: MusicAssistant) -> None:
    """A file whose album tag was removed is no longer listed on its old album."""
    db_track = await mass.music.tracks.add_item_to_library(_track(_album("Salute")))
    assert await _linked_albums(mass, db_track) == {"Salute"}

    await _rescan(mass, _track(None))

    assert await _linked_albums(mass, db_track) == set()


async def test_changed_album_tag_keeps_only_the_new_album(mass: MusicAssistant) -> None:
    """A file whose album tag changed is listed on its new album only."""
    db_track = await mass.music.tracks.add_item_to_library(_track(_album("Salute")))

    await _rescan(mass, _track(_album("Black Beauty")))

    assert await _linked_albums(mass, db_track) == {"Black Beauty"}


@pytest.mark.parametrize("track_on_other_source", [True, False])
async def test_an_album_shared_with_another_source_is_kept(
    mass: MusicAssistant, track_on_other_source: bool
) -> None:
    """An album also on a streaming service is not unlinked, so it is never removed as orphan."""
    extra = (_mapping("sp", STREAM_INSTANCE),) if track_on_other_source else ()
    db_track = await mass.music.tracks.add_item_to_library(
        _track(_album("Salute", _mapping("sp-album", STREAM_INSTANCE)), *extra)
    )

    await _rescan(mass, _track(None))

    assert await _linked_albums(mass, db_track) == {"Salute"}


async def test_a_track_with_two_files_keeps_its_albums(mass: MusicAssistant) -> None:
    """With a second file on the track, it can not be told which file placed it on the album."""
    db_track = await mass.music.tracks.add_item_to_library(
        _track(_album("Salute"), _mapping("Compilations/Salute/26 Black Beauty.mp3"))
    )

    await _rescan(mass, _track(None))

    assert await _linked_albums(mass, db_track) == {"Salute"}


async def test_get_album_of_a_file_without_album_tag_is_not_found() -> None:
    """An album whose only file no longer names it is not found, rather than failing."""
    provider = LocalFileSystemProvider.__new__(LocalFileSystemProvider)
    provider.config = MagicMock()
    provider.config.instance_id = FS_INSTANCE
    provider._album_folder_exists = AsyncMock(return_value=True)  # type: ignore[method-assign]

    async def _tracks(_album_id: str) -> AsyncGenerator[Track]:
        yield Track(
            item_id="1",
            provider="library",
            name="Black Beauty",
            provider_mappings={_mapping(TRACK_FILE)},
        )

    provider._iter_album_tracks = _tracks  # type: ignore[method-assign, assignment]
    provider.resolve = AsyncMock()  # type: ignore[method-assign]
    provider._parse_tags = AsyncMock()  # type: ignore[method-assign]
    provider._parse_track = AsyncMock(return_value=_track(None))  # type: ignore[method-assign]

    with pytest.raises(MediaNotFoundError):
        await provider.get_album("Bert Kaempfert/Salute")
