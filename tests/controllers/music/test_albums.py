"""Tests for the albums controller."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.enums import ExternalID, ImageType
from music_assistant_models.media_items import (
    MediaItemImage,
    ProviderMapping,
    UniqueList,
)

from .helpers import create_album, create_track

if TYPE_CHECKING:
    import pytest
    from music_assistant_models.media_items import Album

    from music_assistant.mass import MusicAssistant

RELEASE_GROUP_MBID = "7d4d1f70-1c99-4c1b-b0f1-9f2c1b2a3d44"
ALBUM_IMAGE = "http://images/album1.jpg"


def _detailed_album(item_id: str = "album1") -> Album:
    """Return an album carrying the details only a full provider fetch delivers."""
    album = create_album("spotify_1", item_id)
    album.year = 1999
    album.external_ids = {(ExternalID.MB_RELEASEGROUP, RELEASE_GROUP_MBID)}
    album.metadata.genres = {"rock"}
    album.metadata.images = UniqueList(
        [MediaItemImage(type=ImageType.THUMB, path=ALBUM_IMAGE, provider="spotify_1")]
    )
    return album


def _tidal_mapping() -> ProviderMapping:
    """Return the album mapping cross-provider matching would have added."""
    return ProviderMapping(
        item_id="tidal_album1", provider_domain="tidal", provider_instance="tidal_1"
    )


async def test_overwrite_update_keeps_artists_when_none_are_given(
    mass: MusicAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """An overwrite update carrying no artists must not clear the stored ones."""
    db_album = await mass.music.albums.add_item_to_library(create_album("spotify_1", "album1"))

    update = create_album("spotify_1", "album1", artist_name=None)
    await mass.music.albums.update_item_in_library(db_album.item_id, update, overwrite=True)

    refreshed = await mass.music.albums.get_library_item(db_album.item_id)
    assert [artist.name for artist in refreshed.artists] == ["Test Artist"]
    assert "Ignoring request to clear all artists" in caplog.text


async def test_overwrite_update_warns_when_no_provider_mappings_are_given(
    mass: MusicAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """An overwrite update carrying no provider mappings must emit a warning."""
    db_album = await mass.music.albums.add_item_to_library(create_album("spotify_1", "album1"))

    update = create_album("spotify_1", "album1")
    update.provider_mappings = set()
    await mass.music.albums.update_item_in_library(db_album.item_id, update, overwrite=True)

    refreshed = await mass.music.albums.get_library_item(db_album.item_id)
    assert refreshed.provider_mappings == db_album.provider_mappings
    assert (
        f"Ignoring request to clear all provider mappings of album item id {db_album.item_id}"
        in caplog.text
    )


async def test_overwrite_update_replaces_artists(mass: MusicAssistant) -> None:
    """An overwrite update carrying artists still replaces the stored ones."""
    db_album = await mass.music.albums.add_item_to_library(create_album("spotify_1", "album1"))

    # a distinct artist id, so the stored relation is replaced rather than renamed
    update = create_album(
        "spotify_1", "album1", artist_name="Other Artist", artist_item_id="other_artist"
    )
    await mass.music.albums.update_item_in_library(db_album.item_id, update, overwrite=True)

    refreshed = await mass.music.albums.get_library_item(db_album.item_id)
    assert [artist.name for artist in refreshed.artists] == ["Other Artist"]


async def test_track_overwrite_keeps_album_artists(mass: MusicAssistant) -> None:
    """A track update carrying an artist-less album must not clear that album's artists."""
    db_album = await mass.music.albums.add_item_to_library(create_album("spotify_1", "album1"))
    track = create_track("spotify_1", "track1")
    track.album = create_album("spotify_1", "album1")
    db_track = await mass.music.tracks.add_item_to_library(track)

    # a provider that builds an album object without artists (as qqmusic does)
    update = create_track("spotify_1", "track1")
    update.album = create_album("spotify_1", "album1", artist_name=None)
    await mass.music.tracks.update_item_in_library(db_track.item_id, update, overwrite=True)

    refreshed = await mass.music.albums.get_library_item(db_album.item_id)
    assert [artist.name for artist in refreshed.artists] == ["Test Artist"]


async def test_track_overwrite_keeps_album_details(mass: MusicAssistant) -> None:
    """A track update carrying a bare album stub must not blank that album's details."""
    db_album = await mass.music.albums.add_item_to_library(_detailed_album())
    track = create_track("spotify_1", "track1")
    track.album = create_album("spotify_1", "album1")
    db_track = await mass.music.tracks.add_item_to_library(track)

    # a provider that embeds a bare album stub in every track (as emby does)
    update = create_track("spotify_1", "track1")
    update.album = create_album("spotify_1", "album1")
    await mass.music.tracks.update_item_in_library(db_track.item_id, update, overwrite=True)

    refreshed = await mass.music.albums.get_library_item(db_album.item_id)
    assert refreshed.year == 1999
    assert refreshed.external_ids == {(ExternalID.MB_RELEASEGROUP, RELEASE_GROUP_MBID)}
    assert refreshed.metadata.genres == {"rock"}
    assert [image.path for image in refreshed.metadata.images or []] == [ALBUM_IMAGE]


async def test_track_overwrite_keeps_other_provider_album_mapping(mass: MusicAssistant) -> None:
    """A track update must not unlink its album from the other providers it matched."""
    db_album = await mass.music.albums.add_item_to_library(create_album("spotify_1", "album1"))
    await mass.music.albums.add_provider_mappings(db_album.item_id, [_tidal_mapping()])
    track = create_track("spotify_1", "track1")
    track.album = create_album("spotify_1", "album1")
    db_track = await mass.music.tracks.add_item_to_library(track)

    update = create_track("spotify_1", "track1")
    update.album = create_album("spotify_1", "album1")
    await mass.music.tracks.update_item_in_library(db_track.item_id, update, overwrite=True)

    refreshed = await mass.music.albums.get_library_item(db_album.item_id)
    assert {mapping.provider_instance for mapping in refreshed.provider_mappings} == {
        "spotify_1",
        "tidal_1",
    }


async def test_overwrite_update_replaces_album_details(mass: MusicAssistant) -> None:
    """An overwrite update carrying details still replaces the stored ones."""
    db_album = await mass.music.albums.add_item_to_library(_detailed_album())

    update = _detailed_album()
    update.year = 2001
    update.external_ids = {(ExternalID.MB_RELEASEGROUP, "11111111-2222-3333-4444-555555555555")}
    update.metadata.genres = {"jazz"}
    update.metadata.images = UniqueList(
        [MediaItemImage(type=ImageType.THUMB, path="http://images/new.jpg", provider="spotify_1")]
    )
    await mass.music.albums.update_item_in_library(db_album.item_id, update, overwrite=True)

    refreshed = await mass.music.albums.get_library_item(db_album.item_id)
    assert refreshed.year == 2001
    assert refreshed.external_ids == {
        (ExternalID.MB_RELEASEGROUP, "11111111-2222-3333-4444-555555555555")
    }
    assert refreshed.metadata.genres == {"jazz"}
    assert [image.path for image in refreshed.metadata.images or []] == ["http://images/new.jpg"]


async def test_overwrite_update_replaces_only_its_own_provider_mapping(
    mass: MusicAssistant,
) -> None:
    """An overwrite replaces the mapping of its own provider and keeps the others."""
    db_album = await mass.music.albums.add_item_to_library(create_album("spotify_1", "album1"))
    await mass.music.albums.add_provider_mappings(db_album.item_id, [_tidal_mapping()])

    # the same album under a new id on its own provider, as a moved folder produces
    update = create_album("spotify_1", "album2")
    await mass.music.albums.update_item_in_library(db_album.item_id, update, overwrite=True)

    refreshed = await mass.music.albums.get_library_item(db_album.item_id)
    assert {
        (mapping.provider_instance, mapping.item_id) for mapping in refreshed.provider_mappings
    } == {("spotify_1", "album2"), ("tidal_1", "tidal_album1")}


async def test_track_overwrite_keeps_album_details_with_an_empty_image_list(
    mass: MusicAssistant,
) -> None:
    """An empty image list is not metadata, so it must not replace the stored details."""
    full = _detailed_album()
    full.metadata.description = "About this album"
    db_album = await mass.music.albums.add_item_to_library(full)
    track = create_track("spotify_1", "track1")
    track.album = create_album("spotify_1", "album1")
    db_track = await mass.music.tracks.add_item_to_library(track)

    # a provider that always assigns an image list, empty or not (as spotify does)
    update = create_track("spotify_1", "track1")
    stub = create_album("spotify_1", "album1")
    stub.metadata.images = UniqueList([])
    update.album = stub
    await mass.music.tracks.update_item_in_library(db_track.item_id, update, overwrite=True)

    refreshed = await mass.music.albums.get_library_item(db_album.item_id)
    assert refreshed.metadata.genres == {"rock"}
    assert refreshed.metadata.description == "About this album"
    assert [image.path for image in refreshed.metadata.images or []] == [ALBUM_IMAGE]


async def test_track_overwrite_keeps_album_version(mass: MusicAssistant) -> None:
    """A track update carrying a version-less album must not blank the stored edition."""
    full = create_album("spotify_1", "album1")
    full.version = "Deluxe Edition"
    db_album = await mass.music.albums.add_item_to_library(full)
    track = create_track("spotify_1", "track1")
    track.album = create_album("spotify_1", "album1")
    db_track = await mass.music.tracks.add_item_to_library(track)

    update = create_track("spotify_1", "track1")
    update.album = create_album("spotify_1", "album1")
    await mass.music.tracks.update_item_in_library(db_track.item_id, update, overwrite=True)

    refreshed = await mass.music.albums.get_library_item(db_album.item_id)
    assert refreshed.version == "Deluxe Edition"


async def test_merge_update_keeps_the_stored_year_and_version(mass: MusicAssistant) -> None:
    """A second provider matching an existing album does not restate its release."""
    full = _detailed_album()
    full.version = "Deluxe Edition"
    db_album = await mass.music.albums.add_item_to_library(full)

    # the same album on another provider, carrying a reissue year and no edition
    update = create_album("tidal_1", "tidal_album1")
    update.year = 2011
    await mass.music.albums.update_item_in_library(db_album.item_id, update)

    refreshed = await mass.music.albums.get_library_item(db_album.item_id)
    assert refreshed.year == 1999
    assert refreshed.version == "Deluxe Edition"
