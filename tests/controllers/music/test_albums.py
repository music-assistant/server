"""Tests for the albums controller."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest
from music_assistant_models.enums import ExternalID, ImageType
from music_assistant_models.helpers import set_global_cache_values
from music_assistant_models.media_items import (
    ItemMapping,
    MediaItemImage,
    ProviderMapping,
    UniqueList,
)

from .helpers import create_album, create_track

if TYPE_CHECKING:
    from music_assistant_models.media_items import Album, Track

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


def _album_track(provider_instance: str, name: str, track_number: int, available: bool) -> Track:
    """Return a provider album track, playable or not."""
    track = create_track(provider_instance, f"{provider_instance}_{track_number}", name=name)
    track.track_number = track_number
    for mapping in track.provider_mappings:
        mapping.available = available
    return track


@pytest.mark.parametrize("unplayable", ["qobuz_1", "spotify_1"])
async def test_album_tracks_prefer_a_playable_copy(mass: MusicAssistant, unplayable: str) -> None:
    """A track one provider cannot play is filled in from another provider that can."""
    playable = "spotify_1" if unplayable == "qobuz_1" else "qobuz_1"
    album = create_album("qobuz_1", "album_q")
    album.provider_mappings.add(
        ProviderMapping(item_id="album_s", provider_domain="spotify", provider_instance="spotify_1")
    )
    library_album = await mass.music.albums.add_item_to_library(album)
    await set_global_cache_values({"available_providers": {"qobuz_1", "spotify_1"}})
    provider_tracks = {
        unplayable: [
            _album_track(unplayable, "Shared", 1, available=False),
            _album_track(unplayable, "Bonus", 2, available=False),
        ],
        playable: [_album_track(playable, "Shared", 1, available=True)],
    }

    with patch.object(
        mass.music.albums,
        "_get_provider_album_tracks",
        AsyncMock(side_effect=lambda _item_id, instance: provider_tracks[instance]),
    ):
        tracks = await mass.music.albums.tracks(library_album.item_id, "library")

    # whichever provider is asked first, the shared track comes from the one that can play it
    assert [(track.name, track.provider, track.available) for track in tracks] == [
        ("Shared", playable, True),
        ("Bonus", unplayable, False),
    ]


def test_album_from_library_item_mapping_has_no_self_mapping(mass: MusicAssistant) -> None:
    """A library item mapping has no provider of its own, so it gets no provider mapping."""
    item = ItemMapping(item_id="42", provider="library", name="Test Album")

    album = mass.music.albums.album_from_item_mapping(item)

    assert album.provider == "library"
    assert album.item_id == "42"
    assert album.provider_mappings == set()


def test_album_from_provider_item_mapping_keeps_mapping(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A provider item mapping is converted into a real, resolvable provider mapping."""
    monkeypatch.setattr(
        mass,
        "get_provider",
        lambda _: SimpleNamespace(domain="spotify", instance_id="spotify_1"),
    )
    item = ItemMapping(item_id="abc", provider="spotify_1", name="Test Album")

    album = mass.music.albums.album_from_item_mapping(item)

    assert album.provider_mappings == {
        ProviderMapping(item_id="abc", provider_domain="spotify", provider_instance="spotify_1")
    }
