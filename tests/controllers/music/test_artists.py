"""Tests for the artists controller."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import AlbumType, ExternalID, ImageType, ProviderFeature
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

from music_assistant.controllers.music.media.artists import ArtistsController

from .helpers import create_album, create_track

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

ARTIST_MBID = "aa1b2c3d-1c99-4c1b-b0f1-9f2c1b2a3d44"
ARTIST_IMAGE = "http://images/artist1.jpg"


@pytest.fixture(name="mass")
def mass_fixture(music_mass: MusicAssistant) -> MusicAssistant:
    """Run on a library-only instance: these tests only touch the library."""
    return music_mass


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


def _spotify_mapping(item_id: str) -> ProviderMapping:
    """Return a spotify provider mapping for the given provider item id."""
    return ProviderMapping(
        item_id=item_id, provider_domain="spotify", provider_instance="spotify_1"
    )


async def _add_artist_with_two_spellings(mass: MusicAssistant) -> Artist:
    """Store a library artist mapped to two provider spellings and holding a Discogs id."""
    return await mass.music.artists.add_item_to_library(
        Artist(
            item_id="0",
            provider="library",
            name="Pyotr Ilyich Tchaikovsky",
            sort_name="Tchaikovsky, Pyotr Ilyich",
            provider_mappings={
                _spotify_mapping("tchaikovsky_full"),
                _spotify_mapping("tchaikovsky_short"),
            },
            external_ids={(ExternalID.DISCOGS, "1234")},
        )
    )


def _short_spelling() -> Artist:
    """Return the provider artist of the short spelling of the library artist."""
    return Artist(
        item_id="tchaikovsky_short",
        provider="spotify_1",
        name="Tchaikovsky",
        provider_mappings={_spotify_mapping("tchaikovsky_short")},
    )


async def _assert_artist_unchanged(mass: MusicAssistant, artist: Artist) -> None:
    """Assert that the stored library artist still matches the given one."""
    stored = await mass.music.artists.get_library_item(artist.item_id)
    assert stored.name == artist.name
    assert stored.sort_name == artist.sort_name
    assert stored.external_ids == artist.external_ids
    assert {x.item_id for x in stored.provider_mappings} == {
        x.item_id for x in artist.provider_mappings
    }


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


async def test_track_overwrite_keeps_the_library_artist(mass: MusicAssistant) -> None:
    """An overwriting track update links its artist without rewriting the library artist."""
    artist = await _add_artist_with_two_spellings(mass)
    db_track = await mass.music.tracks.add_item_to_library(create_track("spotify_1", "track1"))

    update = create_track("spotify_1", "track1")
    update.artists = UniqueList(
        [ItemMapping(item_id="tchaikovsky_short", provider="spotify_1", name="Tchaikovsky")]
    )
    stored = await mass.music.tracks.update_item_in_library(
        db_track.item_id, update, overwrite=True
    )

    await _assert_artist_unchanged(mass, artist)
    assert [x.item_id for x in stored.artists] == [artist.item_id]


async def test_album_overwrite_keeps_the_library_artist(mass: MusicAssistant) -> None:
    """An overwriting album update links its artist without rewriting the library artist."""
    artist = await _add_artist_with_two_spellings(mass)
    db_album = await mass.music.albums.add_item_to_library(create_album("spotify_1", "album1"))

    update = create_album("spotify_1", "album1")
    update.artists = UniqueList([_short_spelling()])
    stored = await mass.music.albums.update_item_in_library(
        db_album.item_id, update, overwrite=True
    )

    await _assert_artist_unchanged(mass, artist)
    assert [x.item_id for x in stored.artists] == [artist.item_id]


async def test_item_overwrite_adds_a_new_mbid_to_the_library_artist(mass: MusicAssistant) -> None:
    """An update whose artist carries a new MusicBrainz id adds it to the library artist."""
    artist = await _add_artist_with_two_spellings(mass)
    db_track = await mass.music.tracks.add_item_to_library(create_track("spotify_1", "track1"))
    spelling = _short_spelling()
    spelling.mbid = ARTIST_MBID

    update = create_track("spotify_1", "track1")
    update.artists = UniqueList([spelling])
    await mass.music.tracks.update_item_in_library(db_track.item_id, update, overwrite=True)

    refreshed = await mass.music.artists.get_library_item(artist.item_id)
    assert refreshed.external_ids == {*artist.external_ids, (ExternalID.MB_ARTIST, ARTIST_MBID)}
    assert refreshed.name == artist.name
    assert refreshed.sort_name == artist.sort_name


async def test_item_overwrite_keeps_the_stored_mbid_of_the_library_artist(
    mass: MusicAssistant,
) -> None:
    """An update whose artist carries another MusicBrainz id leaves the stored one."""
    artist = await _add_artist_with_two_spellings(mass)
    await mass.music.artists.set_external_ids(
        artist.item_id, {*artist.external_ids, (ExternalID.MB_ARTIST, ARTIST_MBID)}
    )
    db_track = await mass.music.tracks.add_item_to_library(create_track("spotify_1", "track1"))
    spelling = _short_spelling()
    spelling.mbid = "11111111-2222-3333-4444-555555555555"

    update = create_track("spotify_1", "track1")
    update.artists = UniqueList([spelling])
    await mass.music.tracks.update_item_in_library(db_track.item_id, update, overwrite=True)

    refreshed = await mass.music.artists.get_library_item(artist.item_id)
    assert refreshed.external_ids == {*artist.external_ids, (ExternalID.MB_ARTIST, ARTIST_MBID)}


async def test_item_overwrite_links_a_new_spelling_matched_by_mbid(
    mass: MusicAssistant,
) -> None:
    """An update whose artist is a new spelling matched by MusicBrainz id adds it as a mapping."""
    artist = await _add_artist_with_two_spellings(mass)
    await mass.music.artists.set_external_ids(
        artist.item_id, {*artist.external_ids, (ExternalID.MB_ARTIST, ARTIST_MBID)}
    )
    db_album = await mass.music.albums.add_item_to_library(create_album("spotify_1", "album1"))
    new_spelling = Artist(
        item_id="tchaikovsky_new",
        provider="spotify_1",
        name="Tchaikovsky",
        provider_mappings={_spotify_mapping("tchaikovsky_new")},
    )
    new_spelling.mbid = ARTIST_MBID

    update = create_album("spotify_1", "album1")
    update.artists = UniqueList([new_spelling])
    stored = await mass.music.albums.update_item_in_library(
        db_album.item_id, update, overwrite=True
    )

    refreshed = await mass.music.artists.get_library_item(artist.item_id)
    assert {x.item_id for x in refreshed.provider_mappings} == {
        *(x.item_id for x in artist.provider_mappings),
        "tchaikovsky_new",
    }
    assert refreshed.name == artist.name
    assert refreshed.sort_name == artist.sort_name
    assert [x.item_id for x in stored.artists] == [artist.item_id]


async def test_item_overwrite_keeps_the_ids_of_an_artist_matched_by_mbid(
    mass: MusicAssistant,
) -> None:
    """A new spelling matched by MusicBrainz id adds no id of a type the library artist holds."""
    artist = await _add_artist_with_two_spellings(mass)
    await mass.music.artists.set_external_ids(
        artist.item_id, {*artist.external_ids, (ExternalID.MB_ARTIST, ARTIST_MBID)}
    )
    db_album = await mass.music.albums.add_item_to_library(create_album("spotify_1", "album1"))
    new_spelling = Artist(
        item_id="tchaikovsky_new",
        provider="spotify_1",
        name="Tchaikovsky",
        provider_mappings={_spotify_mapping("tchaikovsky_new")},
        external_ids={(ExternalID.DISCOGS, "9999")},
    )
    new_spelling.mbid = ARTIST_MBID

    update = create_album("spotify_1", "album1")
    update.artists = UniqueList([new_spelling])
    await mass.music.albums.update_item_in_library(db_album.item_id, update, overwrite=True)

    refreshed = await mass.music.artists.get_library_item(artist.item_id)
    assert refreshed.external_ids == {*artist.external_ids, (ExternalID.MB_ARTIST, ARTIST_MBID)}
    assert "tchaikovsky_new" in {x.item_id for x in refreshed.provider_mappings}
    assert refreshed.name == artist.name


async def test_item_overwrite_without_new_artist_ids_writes_no_artist(
    mass: MusicAssistant,
) -> None:
    """An update whose artists bring nothing new leaves the library artists untouched."""
    await _add_artist_with_two_spellings(mass)
    db_track = await mass.music.tracks.add_item_to_library(create_track("spotify_1", "track1"))

    update = create_track("spotify_1", "track1")
    update.artists = UniqueList([_short_spelling()])
    with patch.object(mass.music.artists, "_update_library_item", AsyncMock()) as artist_update:
        await mass.music.tracks.update_item_in_library(db_track.item_id, update, overwrite=True)

    artist_update.assert_not_called()


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


async def test_album_artists_only_needs_an_album_in_the_library(mass: MusicAssistant) -> None:
    """An artist only counts as album artist when one of its own albums is in the library."""
    library_album = create_album("spotify_1", "kept", "Kept Album", "Album Artist", "album_artist")
    for mapping in library_album.provider_mappings:
        mapping.in_library = True
    # this album is only stored because one of its tracks is in the library
    track = _track_on_album(
        "hit",
        "track_artist",
        "Track Artist",
        create_album("spotify_1", "hits", "Hits Album", "Track Artist", "track_artist"),
    )
    for artist in (*library_album.artists, *track.artists):
        assert isinstance(artist, Artist)
        for mapping in artist.provider_mappings:
            mapping.in_library = True
    await mass.music.albums.add_item_to_library(library_album)
    await mass.music.tracks.add_item_to_library(track)
    artists = mass.music.artists
    assert {artist.name for artist in await artists.library_items()} == {
        "Album Artist",
        "Track Artist",
    }

    album_artists = await artists.library_items(album_artists_only=True)

    assert [artist.name for artist in album_artists] == ["Album Artist"]
    assert await artists.library_count(album_artists_only=True) == 1


@pytest.mark.parametrize(
    ("feature", "library_method", "provider_method"),
    [
        (
            ProviderFeature.ARTIST_TOPTRACKS,
            "get_library_artist_toptracks",
            "get_provider_artist_toptracks",
        ),
        (
            ProviderFeature.ARTIST_TOPALBUMS,
            "get_library_artist_topalbums",
            "get_provider_artist_topalbums",
        ),
        (
            ProviderFeature.SIMILAR_ARTISTS,
            "get_library_artist_similar_artists",
            "get_provider_artist_similar_artists",
        ),
    ],
)
async def test_library_artist_listings_query_one_instance_per_streaming_domain(
    feature: ProviderFeature, library_method: str, provider_method: str
) -> None:
    """Instances of one streaming provider return the same catalog, so only one is queried."""
    artist = Artist(
        item_id="1",
        provider="library",
        name="Test Artist",
        provider_mappings={
            ProviderMapping(
                item_id=f"artist_{instance}", provider_domain=domain, provider_instance=instance
            )
            for domain, instance in (
                ("spotify", "spotify_1"),
                ("spotify", "spotify_2"),
                ("filesystem_local", "filesystem_local_1"),
                ("filesystem_local", "filesystem_local_2"),
            )
        },
    )
    providers = {
        instance: SimpleNamespace(
            domain=instance.rsplit("_", maxsplit=1)[0],
            is_streaming_provider=instance.startswith("spotify"),
            supported_features={feature},
        )
        for instance in ("spotify_1", "spotify_2", "filesystem_local_1", "filesystem_local_2")
    }
    mass = MagicMock()
    mass.get_provider = MagicMock(side_effect=lambda instance, **_kwargs: providers[instance])
    mass.get_providers_supporting_feature = MagicMock(return_value=[])
    ctrl = ArtistsController.__new__(ArtistsController)
    ctrl.mass = mass
    ctrl.logger = logging.getLogger("test.artists.listings")
    provider_listing = AsyncMock(return_value=[])
    with patch.multiple(
        ctrl,
        get_library_item=AsyncMock(return_value=artist),
        **{provider_method: provider_listing},
    ):
        await getattr(ctrl, library_method)("1")

    queried = {call.args[1] for call in provider_listing.await_args_list}
    # one Spotify instance, and both (non-streaming) filesystem instances
    assert queried == {"spotify_1", "filesystem_local_1", "filesystem_local_2"}
