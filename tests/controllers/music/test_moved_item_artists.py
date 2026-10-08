"""Tests that a renamed or re-tagged file does not keep the artists of its old path."""

from __future__ import annotations

from music_assistant_models.enums import ExternalID
from music_assistant_models.media_items import (
    Album,
    Artist,
    AudioFormat,
    ProviderMapping,
    Track,
    UniqueList,
)

from music_assistant.controllers.music.helpers import update_moves_single_source_item
from music_assistant.mass import MusicAssistant

INSTANCE_ID = "filesystem_local--1"
OTHER_INSTANCE_ID = "filesystem_local--2"
RECORDING_MBID = "5f4e0a1c-0000-4000-8000-000000000001"
RELEASE_MBID = "5f4e0a1c-0000-4000-8000-0000000000a1"
OLD_NAME = "Tchaikovsky"
NEW_NAME = "Pyotr Ilyich Tchaikovsky"


def _mapping(item_id: str, instance_id: str = INSTANCE_ID) -> set[ProviderMapping]:
    """Return a single provider mapping for the given provider item id."""
    return {
        ProviderMapping(
            item_id=item_id,
            provider_domain=instance_id.split("--", maxsplit=1)[0],
            provider_instance=instance_id,
            audio_format=AudioFormat(),
        )
    }


def _artist(name: str, instance_id: str = INSTANCE_ID) -> Artist:
    """Return an artist as the provider reports it, without a MusicBrainz id."""
    return Artist(
        item_id=name,
        provider=instance_id,
        name=name,
        provider_mappings=_mapping(name, instance_id),
    )


def _album(folder: str, artist: str | None, instance_id: str = INSTANCE_ID) -> Album:
    """Return an album as a filesystem provider reports it for an album folder."""
    album = Album(
        item_id=folder,
        provider=instance_id,
        name="Swan Lake",
        provider_mappings=_mapping(folder, instance_id),
        artists=UniqueList([_artist(artist, instance_id)] if artist else []),
    )
    album.mbid = RELEASE_MBID
    return album


def _track(
    folder: str, artist: str | None, instance_id: str = INSTANCE_ID, with_album: bool = True
) -> Track:
    """Return the first track of the album folder, tagged with the given artist."""
    path = f"{folder}/01.flac"
    return Track(
        item_id=path,
        provider=instance_id,
        name="Swan Lake, Op. 20: Scene",
        duration=200,
        track_number=1,
        external_ids={(ExternalID.MB_RECORDING, RECORDING_MBID)},
        provider_mappings=_mapping(path, instance_id),
        artists=UniqueList([_artist(artist, instance_id)] if artist else []),
        album=_album(folder, artist, instance_id) if with_album else None,
    )


async def _track_artists(mass: MusicAssistant, track_id: str) -> set[str]:
    """Return the names of the artists linked to a library track."""
    return {artist.name for artist in (await mass.music.tracks.get_library_item(track_id)).artists}


async def _album_artists(mass: MusicAssistant, track_id: str) -> set[str]:
    """Return the names of the artists linked to the album of a library track."""
    track = await mass.music.tracks.get_library_item(track_id)
    assert track.album
    album = await mass.music.albums.get_library_item(track.album.item_id)
    return {artist.name for artist in album.artists}


async def test_moved_file_replaces_the_artists(mass: MusicAssistant) -> None:
    """The renamed file used to leave the track and album under both spellings."""
    old = await mass.music.tracks.add_item_to_library(_track(OLD_NAME, OLD_NAME))
    new = await mass.music.tracks.add_item_to_library(_track(NEW_NAME, NEW_NAME))
    assert new.item_id == old.item_id

    await mass.music.tracks.remove_provider_mapping(old.item_id, INSTANCE_ID, f"{OLD_NAME}/01.flac")

    assert await _track_artists(mass, old.item_id) == {NEW_NAME}
    assert await _album_artists(mass, old.item_id) == {NEW_NAME}


async def test_item_of_another_provider_keeps_both_artists(mass: MusicAssistant) -> None:
    """The stored artists may have come from the other provider, so they are merged."""
    old = await mass.music.tracks.add_item_to_library(_track(OLD_NAME, OLD_NAME))
    await mass.music.tracks.add_item_to_library(_track(OLD_NAME, OLD_NAME, OTHER_INSTANCE_ID))

    await mass.music.tracks.add_item_to_library(_track(NEW_NAME, NEW_NAME))

    assert await _track_artists(mass, old.item_id) == {OLD_NAME, NEW_NAME}
    assert await _album_artists(mass, old.item_id) == {OLD_NAME, NEW_NAME}


async def test_moved_file_without_artists_keeps_the_stored_ones(mass: MusicAssistant) -> None:
    """An empty artist list says nothing about the stored artists."""
    old = await mass.music.tracks.add_item_to_library(_track(OLD_NAME, OLD_NAME, with_album=False))

    await mass.music.tracks.add_item_to_library(_track(NEW_NAME, None, with_album=False))

    assert await _track_artists(mass, old.item_id) == {OLD_NAME}


async def test_same_id_without_overwrite_still_merges(mass: MusicAssistant) -> None:
    """Only a new id replaces the artists: a plain re-add of the same item merges as before."""
    old = await mass.music.tracks.add_item_to_library(_track(OLD_NAME, OLD_NAME, with_album=False))
    re_added = _track(OLD_NAME, NEW_NAME, with_album=False)

    await mass.music.tracks.add_item_to_library(re_added)

    assert await _track_artists(mass, old.item_id) == {OLD_NAME, NEW_NAME}


async def test_merging_two_library_tracks_keeps_both_artists(mass: MusicAssistant) -> None:
    """Both files still exist after a merge, so the track keeps the artists of both."""
    target = await mass.music.tracks.add_item_to_library(
        _track(OLD_NAME, OLD_NAME, with_album=False)
    )
    source_track = _track(NEW_NAME, NEW_NAME, with_album=False)
    source_track.external_ids = set()
    source_track.name = "Another title"
    source = await mass.music.tracks.add_item_to_library(source_track)
    assert source.item_id != target.item_id

    await mass.music.tracks.merge_library_items(target.item_id, source.item_id)

    assert await _track_artists(mass, target.item_id) == {OLD_NAME, NEW_NAME}


def test_update_moves_single_source_item() -> None:
    """Only a new id on the one provider the item is stored for counts as a move."""
    old = _mapping("old.flac")
    new = _mapping("new.flac")
    other = _mapping("old.flac", OTHER_INSTANCE_ID)

    assert update_moves_single_source_item(old, new)
    assert update_moves_single_source_item(old, {*old, *new})
    assert not update_moves_single_source_item(old, old)
    assert not update_moves_single_source_item({*old, *other}, new)
    assert not update_moves_single_source_item(old, _mapping("new.flac", OTHER_INSTANCE_ID))
    assert not update_moves_single_source_item(old, set())
    assert not update_moves_single_source_item(set(), new)
