"""Tests that an overwrite from one file keeps the other files of the same provider instance."""

from __future__ import annotations

import pytest
from music_assistant_models.enums import AlbumType
from music_assistant_models.media_items import Album, Artist, ProviderMapping, Track

from music_assistant.mass import MusicAssistant

INSTANCE = "filesystem_local--abc"
CUE_ID = "TM Network/humansystem/album.flac.cue#10"
DSF_ID = "TM Network/humansystem/10 Song.dsf"

pytestmark = pytest.mark.xfail(
    strict=True, reason="support#6676: an overwrite drops same instance data, not fixed yet"
)


def _mapping(item_id: str) -> ProviderMapping:
    """Return a mapping of the given item id on the filesystem instance."""
    return ProviderMapping(
        item_id=item_id,
        provider_domain="filesystem_local",
        provider_instance=INSTANCE,
        in_library=True,
    )


def _artist(name: str) -> Artist:
    """Return an artist as the filesystem provider reports it."""
    return Artist(item_id=name, provider=INSTANCE, name=name, provider_mappings={_mapping(name)})


def _album(item_ids: list[str], artist: str = "TM Network") -> Album:
    """Return an album with a mapping per given item id."""
    album = Album(
        item_id=item_ids[0],
        provider=INSTANCE,
        name="humansystem",
        album_type=AlbumType.ALBUM,
        provider_mappings={_mapping(item_id) for item_id in item_ids},
    )
    album.artists.set([_artist(artist)])
    return album


def _track(item_ids: list[str]) -> Track:
    """Return a track with a mapping per given item id."""
    track = Track(
        item_id=item_ids[0],
        provider=INSTANCE,
        name="Song",
        duration=300,
        provider_mappings={_mapping(item_id) for item_id in item_ids},
    )
    track.artists.set([_artist("TM Network")])
    return track


async def _stored_track_ids(mass: MusicAssistant, db_id: int | str) -> set[str]:
    """Return the provider item ids stored for the library track."""
    item = await mass.music.tracks.get_library_item(db_id)
    return {mapping.item_id for mapping in item.provider_mappings}


async def test_track_overwrite_keeps_other_file(mass: MusicAssistant) -> None:
    """Updating from the DSF must not drop the CUE track of the same instance."""
    db_id = (await mass.music.tracks.add_item_to_library(_track([CUE_ID, DSF_ID]))).item_id

    await mass.music.tracks.update_item_in_library(db_id, _track([DSF_ID]), overwrite=True)

    assert await _stored_track_ids(mass, db_id) == {CUE_ID, DSF_ID}


async def test_changed_file_rescan_keeps_other_file(mass: MusicAssistant) -> None:
    """The scan re-adds a changed file with overwrite_existing."""
    db_id = (await mass.music.tracks.add_item_to_library(_track([CUE_ID]))).item_id
    assert (await mass.music.tracks.add_item_to_library(_track([DSF_ID]))).item_id == db_id

    await mass.music.tracks.add_item_to_library(_track([DSF_ID]), overwrite_existing=True)

    assert await _stored_track_ids(mass, db_id) == {CUE_ID, DSF_ID}


async def test_album_overwrite_keeps_other_folder(mass: MusicAssistant) -> None:
    """Updating from one folder must not drop the other folder of the same instance."""
    db_id = (await mass.music.albums.add_item_to_library(_album(["flac", "dsf"]))).item_id

    await mass.music.albums.update_item_in_library(db_id, _album(["dsf"]), overwrite=True)

    item = await mass.music.albums.get_library_item(db_id)
    assert {mapping.item_id for mapping in item.provider_mappings} == {"flac", "dsf"}


async def test_changed_album_artist_drops_old_album_link(mass: MusicAssistant) -> None:
    """A file whose album artist tag changes must leave its old album."""

    def _dsf(album_artist: str) -> Track:
        track = _track([DSF_ID])
        track.album = _album([f"{album_artist}/humansystem"], album_artist)
        track.disc_number, track.track_number = 0, 10
        return track

    db_id = (await mass.music.tracks.add_item_to_library(_dsf("Various Artists"))).item_id

    await mass.music.tracks.add_item_to_library(_dsf("TM Network"), overwrite_existing=True)

    rows = await mass.music.database.get_rows_from_query(
        "SELECT artists.name FROM album_tracks "
        "JOIN album_artists ON album_artists.album_id = album_tracks.album_id "
        "JOIN artists ON artists.item_id = album_artists.artist_id "
        "WHERE track_id = :track_id",
        {"track_id": int(db_id)},
    )
    assert {row["name"] for row in rows} == {"TM Network"}
