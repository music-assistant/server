"""Tests for the disliked-track keys and the hard filter that keeps them out of playback."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

from music_assistant_models.auth import User, UserRole
from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import Artist, ItemMapping, ProviderMapping, UniqueList

from music_assistant.constants import DB_TABLE_PROVIDER_MAPPINGS
from music_assistant.controllers.music.favorites import (
    DislikedTrackKeys,
    filter_disliked,
    with_user_favorites,
    without_disliked_tracks,
)

from .helpers import create_album, create_track

if TYPE_CHECKING:
    from music_assistant_models.media_items import Track

    from music_assistant.mass import MusicAssistant

PROV_A = "spotify--1"
PROV_B = "subsonic--1"
USER_A = "user-a"
USER_B = "user-b"


async def _add_track(mass: MusicAssistant, name: str, *, second_source: bool = False) -> Track:
    """Add a library track, optionally one that a second music source holds as well."""
    track = create_track(PROV_A, uuid4().hex, name=name, isrc=uuid4().hex)
    if second_source:
        track.provider_mappings.add(
            ProviderMapping(item_id=uuid4().hex, provider_domain=PROV_B, provider_instance=PROV_B)
        )
    for mapping in track.provider_mappings:
        mapping.in_library = True
    return await mass.music.tracks.add_item_to_library(track)


async def test_disliked_track_keys_are_the_users_own(music_mass_module: MusicAssistant) -> None:
    """A dislike yields the track's library id and every instance key it is known by."""
    mass = music_mass_module
    disliked = await _add_track(mass, "Never Again", second_source=True)
    liked = await _add_track(mass, "Play It Again")
    store = mass.music.favorites
    await store.set(MediaType.TRACK, int(disliked.item_id), False, [USER_A])
    await store.set(MediaType.TRACK, int(liked.item_id), True, [USER_A])
    # the same track the other user likes, so the query is scoped by user and by state
    await store.set(MediaType.TRACK, int(disliked.item_id), True, [USER_B])
    # a cleared state is not a dislike
    cleared = await _add_track(mass, "Cleared Again")
    await store.set(MediaType.TRACK, int(cleared.item_id), None, [USER_A])

    item_ids, provider_keys = await store.disliked_track_keys(USER_A)

    assert item_ids == {(MediaType.TRACK, int(disliked.item_id))}
    assert provider_keys == {
        (MediaType.TRACK, mapping.provider_instance, mapping.item_id)
        for mapping in disliked.provider_mappings
    }
    assert len(provider_keys) == 2
    # nobody's playback is gated by another user's dislike
    assert await store.disliked_track_keys(USER_B) == (set(), set())
    # a disliked track that lost every mapping still counts by its library id
    await mass.music.database.delete(
        DB_TABLE_PROVIDER_MAPPINGS,
        {"media_type": MediaType.TRACK.value, "item_id": int(disliked.item_id)},
    )
    assert await store.disliked_track_keys(USER_A) == (
        {(MediaType.TRACK, int(disliked.item_id))},
        set(),
    )


async def test_disliked_track_keys_cover_albums_and_artists(
    music_mass_module: MusicAssistant,
) -> None:
    """A disliked album or artist yields its keys; an album's tracks drop from library or source."""
    mass = music_mass_module
    store = mass.music.favorites
    # a user of its own: every test track shares the disliked artist
    user_id = uuid4().hex
    track = create_track(PROV_A, uuid4().hex, name="On A Disliked Album", isrc=uuid4().hex)
    track.album = create_album(PROV_A, uuid4().hex, name="Disliked Album")
    library_track = await mass.music.tracks.add_item_to_library(track)
    album = library_track.album
    artist = library_track.artists[0]
    assert album is not None
    await store.set(MediaType.ALBUM, int(album.item_id), False, [user_id])

    assert await without_disliked_tracks(mass, user_id, [library_track, track]) == []

    await store.set(MediaType.ARTIST, int(artist.item_id), False, [user_id])
    item_ids, provider_keys = await store.disliked_track_keys(user_id)

    # the tracks of a disliked album count as disliked tracks
    assert item_ids == {
        (MediaType.TRACK, int(library_track.item_id)),
        (MediaType.ALBUM, int(album.item_id)),
        (MediaType.ARTIST, int(artist.item_id)),
    }
    # the shared artist is known by the artist mapping of every test track
    assert provider_keys >= {
        (MediaType.ALBUM, PROV_A, track.album.item_id),
        (MediaType.ARTIST, PROV_A, track.artists[0].item_id),
    }


async def test_a_disliked_album_drops_a_track_that_shows_another_album(
    music_mass_module: MusicAssistant,
) -> None:
    """A track on more than one album is dropped for a dislike of the album it does not show."""
    mass = music_mass_module
    store = mass.music.favorites
    user_id = uuid4().hex
    track_id = uuid4().hex
    track = create_track(PROV_A, track_id, name="On Two Albums", isrc=uuid4().hex)
    track.album = create_album(PROV_A, uuid4().hex, name="Original Album")
    await mass.music.tracks.add_item_to_library(track)
    on_compilation = create_track(PROV_A, track_id, name="On Two Albums", isrc=uuid4().hex)
    on_compilation.album = create_album(PROV_A, uuid4().hex, name="Compilation")
    compilation = await mass.music.albums.add_item_to_library(on_compilation.album)
    library_track = await mass.music.tracks.add_item_to_library(on_compilation)
    assert library_track.album is not None
    assert library_track.album.item_id != compilation.item_id
    await store.set(MediaType.ALBUM, int(compilation.item_id), False, [user_id])

    assert await without_disliked_tracks(mass, user_id, [library_track, track]) == []


def test_filter_disliked_drops_a_track_by_library_id_or_by_mapping() -> None:
    """A dislike matches a library item by its id and a provider item by its mapping."""
    by_library_id = create_track("library", "7", name="Known By Id")
    by_mapping = create_track(PROV_A, "disliked-elsewhere", name="Known By Mapping")
    kept = create_track(PROV_A, "fine", name="Still Fine")
    mapping = next(iter(by_mapping.provider_mappings))
    keys: DislikedTrackKeys = (
        {(MediaType.TRACK, 7)},
        {(MediaType.TRACK, mapping.provider_instance, mapping.item_id)},
    )

    assert filter_disliked([by_library_id, by_mapping, kept], keys) == [kept]

    # a user without dislikes gets the candidates back untouched
    candidates = [by_library_id, by_mapping, kept]
    assert filter_disliked(candidates, (set(), set())) is candidates


def test_filter_disliked_drops_a_track_by_its_album_or_artist() -> None:
    """A disliked album or artist matches by library id, by reference or by mapping."""
    by_library_album = create_track("library", "1", name="On Library Album 3")
    by_library_album.artists = UniqueList()
    by_library_album.album = ItemMapping(
        item_id="3", provider="library", name="Album 3", media_type=MediaType.ALBUM
    )
    by_library_artist = create_track("library", "2", name="By Library Artist 5")
    by_library_artist.artists = UniqueList(
        [ItemMapping(item_id="5", provider="library", name="Artist 5", media_type=MediaType.ARTIST)]
    )
    # a provider reference that carries neither mappings nor its media type
    by_album_reference = create_track(PROV_A, "on-album", name="On Provider Album")
    by_album_reference.album = ItemMapping(item_id="disliked-album", provider=PROV_A, name="Album")
    # a full artist, disliked on another source it is mapped on
    by_artist_mapping = create_track(PROV_A, "by", name="By Provider Artist")
    artist = by_artist_mapping.artists[0]
    assert isinstance(artist, Artist)
    artist.provider_mappings.add(
        ProviderMapping(item_id="artist-on-b", provider_domain=PROV_B, provider_instance=PROV_B)
    )
    # the library ids of a disliked album and artist are no track's
    kept = create_track("library", "3", name="Track 3")
    kept.artists = UniqueList()
    keys: DislikedTrackKeys = (
        {(MediaType.ALBUM, 3), (MediaType.ARTIST, 5)},
        {(MediaType.ALBUM, PROV_A, "disliked-album"), (MediaType.ARTIST, PROV_B, "artist-on-b")},
    )

    tracks = [by_library_album, by_library_artist, by_album_reference, by_artist_mapping, kept]
    assert filter_disliked(tracks, keys) == [kept]


async def test_without_disliked_drops_the_users_dislikes(music_mass_module: MusicAssistant) -> None:
    """A candidate list is filtered for a user, and left alone for an anonymous queue."""
    mass = music_mass_module
    store = mass.music.favorites
    disliked = await _add_track(mass, "Skipped For A")
    liked = await _add_track(mass, "Kept For A")
    await store.set(MediaType.TRACK, int(disliked.item_id), False, [USER_A])

    kept = await without_disliked_tracks(mass, USER_A, [disliked, liked])
    assert [x.item_id for x in kept] == [liked.item_id]

    untouched = await without_disliked_tracks(mass, None, [disliked, liked])
    assert [x.item_id for x in untouched] == [disliked.item_id, liked.item_id]


async def test_with_user_favorites_covers_every_media_type(
    music_mass_module: MusicAssistant,
) -> None:
    """One call stamps the user's state on items of different media types."""
    mass = music_mass_module
    track = await _add_track(mass, "Liked Track")
    album = await mass.music.albums.add_item_to_library(create_album(PROV_A, uuid4().hex))
    await mass.music.favorites.set(MediaType.TRACK, int(track.item_id), True, [USER_A])
    await mass.music.favorites.set(MediaType.ALBUM, int(album.item_id), False, [USER_A])

    user_a = User(user_id=USER_A, username=USER_A, role=UserRole.USER)
    items = await with_user_favorites(mass, user_a, [track, album])
    assert [item.favorite for item in items] == [True, False]


async def test_with_user_favorites_applies_the_asking_users_state(
    music_mass_module: MusicAssistant,
) -> None:
    """A track list filled for somebody else ends up carrying the asking user's state."""
    mass = music_mass_module
    store = mass.music.favorites
    liked = await _add_track(mass, "Liked By A")
    disliked = await _add_track(mass, "Disliked By A")
    plain = await _add_track(mass, "Nothing From A")
    await store.set(MediaType.TRACK, int(liked.item_id), True, [USER_A])
    await store.set(MediaType.TRACK, int(disliked.item_id), False, [USER_A])
    await store.set(MediaType.TRACK, int(plain.item_id), True, [USER_B])
    for track in (liked, disliked, plain):
        # the state of whoever filled the cached list
        track.favorite = True

    user_a = User(user_id=USER_A, username=USER_A, role=UserRole.USER)
    tracks = await with_user_favorites(mass, user_a, [liked, disliked, plain])
    assert [x.favorite for x in tracks] == [True, False, None]

    tracks = await with_user_favorites(mass, None, [liked, disliked, plain])
    assert [x.favorite for x in tracks] == [None, None, None]
