"""Tests for the artists controller."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

from music_assistant_models.enums import AlbumType, ExternalID, ImageType
from music_assistant_models.media_items import (
    Album,
    AlbumSummary,
    Artist,
    ItemMapping,
    MediaItemImage,
    ProviderMapping,
    Track,
    UniqueList,
)

from .helpers import create_album, create_track

if TYPE_CHECKING:
    import pytest

    from music_assistant.mass import MusicAssistant

ARTIST_MBID = "aa1b2c3d-1c99-4c1b-b0f1-9f2c1b2a3d44"
ARTIST_IMAGE = "http://images/artist1.jpg"


def _artist_stub() -> Artist:
    """Return the bare artist providers embed in their track payloads."""
    return Artist(
        item_id="track1_artist",
        provider="spotify_1",
        name="Test Artist",
        provider_mappings={
            ProviderMapping(
                item_id="track1_artist",
                provider_domain="spotify",
                provider_instance="spotify_1",
            )
        },
    )


def _track_on_album(
    item_id: str, artist_item_id: str, artist_name: str, album: Album, in_library: bool = True
) -> Track:
    """Return a provider track on the given album, credited to a single artist."""
    track = create_track("spotify_1", item_id, name=item_id, isrc=f"ISRC{item_id}")
    for mapping in track.provider_mappings:
        mapping.in_library = in_library
    track.artists = UniqueList(
        [
            Artist(
                item_id=artist_item_id,
                provider="spotify_1",
                name=artist_name,
                provider_mappings={
                    ProviderMapping(
                        item_id=artist_item_id,
                        provider_domain="spotify",
                        provider_instance="spotify_1",
                    )
                },
            )
        ]
    )
    track.album = album
    return track


def _detailed_artist() -> Artist:
    """Return an artist carrying the details only a full provider fetch delivers."""
    artist = _artist_stub()
    artist.external_ids = {(ExternalID.MB_ARTIST, ARTIST_MBID)}
    artist.metadata.description = "A biography"
    artist.metadata.genres = {"rock"}
    artist.metadata.images = UniqueList(
        [MediaItemImage(type=ImageType.THUMB, path=ARTIST_IMAGE, provider="spotify_1")]
    )
    return artist


async def test_track_overwrite_keeps_artist_details(mass: MusicAssistant) -> None:
    """A track update carrying a bare artist stub must not blank that artist's details."""
    db_artist = await mass.music.artists.add_item_to_library(_detailed_artist())
    db_track = await mass.music.tracks.add_item_to_library(create_track("spotify_1", "track1"))

    update = create_track("spotify_1", "track1")
    update.artists = UniqueList([_artist_stub()])
    await mass.music.tracks.update_item_in_library(db_track.item_id, update, overwrite=True)

    refreshed = await mass.music.artists.get_library_item(db_artist.item_id)
    assert refreshed.external_ids == {(ExternalID.MB_ARTIST, ARTIST_MBID)}
    assert refreshed.metadata.description == "A biography"
    assert refreshed.metadata.genres == {"rock"}
    assert [image.path for image in refreshed.metadata.images or []] == [ARTIST_IMAGE]


async def test_track_overwrite_keeps_other_provider_artist_mapping(mass: MusicAssistant) -> None:
    """A track update must not unlink its artist from the other providers it matched."""
    db_artist = await mass.music.artists.add_item_to_library(_artist_stub())
    await mass.music.artists.add_provider_mappings(
        db_artist.item_id,
        [
            ProviderMapping(
                item_id="tidal_artist1",
                provider_domain="tidal",
                provider_instance="tidal_1",
            )
        ],
    )
    db_track = await mass.music.tracks.add_item_to_library(create_track("spotify_1", "track1"))

    update = create_track("spotify_1", "track1")
    update.artists = UniqueList([_artist_stub()])
    await mass.music.tracks.update_item_in_library(db_track.item_id, update, overwrite=True)

    refreshed = await mass.music.artists.get_library_item(db_artist.item_id)
    assert {mapping.provider_instance for mapping in refreshed.provider_mappings} == {
        "spotify_1",
        "tidal_1",
    }


async def test_overwrite_update_replaces_artist_details(mass: MusicAssistant) -> None:
    """An overwrite update carrying details still replaces the stored ones."""
    db_artist = await mass.music.artists.add_item_to_library(_detailed_artist())

    update = _detailed_artist()
    update.external_ids = {(ExternalID.MB_ARTIST, "11111111-2222-3333-4444-555555555555")}
    update.metadata.description = "A better biography"
    update.metadata.images = UniqueList(
        [MediaItemImage(type=ImageType.THUMB, path="http://images/new.jpg", provider="spotify_1")]
    )
    await mass.music.artists.update_item_in_library(db_artist.item_id, update, overwrite=True)

    refreshed = await mass.music.artists.get_library_item(db_artist.item_id)
    assert refreshed.external_ids == {
        (ExternalID.MB_ARTIST, "11111111-2222-3333-4444-555555555555")
    }
    assert refreshed.metadata.description == "A better biography"
    assert [image.path for image in refreshed.metadata.images or []] == ["http://images/new.jpg"]


def test_artist_from_library_item_mapping_has_no_self_mapping(mass: MusicAssistant) -> None:
    """A library item mapping has no provider of its own, so it gets no provider mapping."""
    item = ItemMapping(item_id="42", provider="library", name="Test Artist")

    artist = mass.music.artists.artist_from_item_mapping(item)

    assert artist.provider == "library"
    assert artist.item_id == "42"
    assert artist.provider_mappings == set()


def test_artist_from_provider_item_mapping_keeps_mapping(
    mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A provider item mapping is converted into a real, resolvable provider mapping."""
    monkeypatch.setattr(
        mass,
        "get_provider",
        lambda _: SimpleNamespace(domain="spotify", instance_id="spotify_1"),
    )
    item = ItemMapping(item_id="abc", provider="spotify_1", name="Test Artist")

    artist = mass.music.artists.artist_from_item_mapping(item)

    assert artist.provider_mappings == {
        ProviderMapping(item_id="abc", provider_domain="spotify", provider_instance="spotify_1")
    }


async def test_appears_on_lists_albums_of_library_tracks(mass: MusicAssistant) -> None:
    """An artist appears on the albums of its library tracks, newest first, but not its own."""
    compilation = create_album(
        "spotify_1", "compilation", "Compilation", "Various Artists", "various"
    )
    compilation.album_type = AlbumType.COMPILATION
    compilation.year = 2001
    for mapping in compilation.provider_mappings:
        mapping.in_library = True
    await mass.music.albums.add_item_to_library(compilation)
    # an album only known through a library track still counts
    soundtrack = create_album("spotify_1", "soundtrack", "Soundtrack", "Host Artist", "host")
    soundtrack.year = 2010
    own = create_album("spotify_1", "own", "Own Album", "Guest Artist", "guest")
    for index, album in enumerate((compilation, soundtrack, own)):
        await mass.music.tracks.add_item_to_library(
            _track_on_album(f"track{index}", "guest", "Guest Artist", album)
        )
    # a track that is not in the library does not count
    unliked = create_album("spotify_1", "unliked", "Unliked Album", "Host Artist", "host")
    await mass.music.tracks.add_item_to_library(
        _track_on_album("track3", "guest", "Guest Artist", unliked, in_library=False)
    )
    guest = await mass.music.artists.get_library_item_by_prov_id("guest", "spotify_1")
    host = await mass.music.artists.get_library_item_by_prov_id("host", "spotify_1")
    assert guest is not None
    assert host is not None

    albums = await mass.music.artists.appears_on(guest.item_id, "library")

    assert [album.name for album in albums] == ["Soundtrack", "Compilation"]
    assert all(isinstance(album, AlbumSummary) for album in albums)
    assert albums[1].album_type == AlbumType.COMPILATION
    assert [artist.name for artist in albums[1].artists] == ["Various Artists"]
    # the provider filter applies to the tracks
    filtered = await mass.music.artists.appears_on(guest.item_id, "library", "spotify_1")
    assert [album.name for album in filtered] == ["Soundtrack", "Compilation"]
    assert await mass.music.artists.appears_on(guest.item_id, "library", "tidal_1") == []
    # an album artist without any track credits appears on nothing
    assert await mass.music.artists.appears_on(host.item_id, "library") == []


async def test_appears_on_provider_artist_is_empty(mass: MusicAssistant) -> None:
    """Appears on is only derived from the library, so a provider artist has none."""
    assert await mass.music.artists.appears_on("guest", "spotify_1") == []
