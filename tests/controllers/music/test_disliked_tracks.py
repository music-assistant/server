"""Tests for the disliked-track keys and the hard filter that keeps them out of playback."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

from music_assistant_models.auth import User, UserRole
from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import ProviderMapping

from music_assistant.controllers.music.favorites import (
    DislikedTrackKeys,
    filter_disliked,
    with_user_favorites,
    without_disliked_tracks,
)

from .helpers import create_track

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

    item_ids, provider_keys = await store.disliked_track_keys(USER_A)

    assert item_ids == {int(disliked.item_id)}
    assert provider_keys == {
        (mapping.provider_instance, mapping.item_id) for mapping in disliked.provider_mappings
    }
    assert len(provider_keys) == 2
    # nobody's playback is gated by another user's dislike
    assert await store.disliked_track_keys(USER_B) == (set(), set())


def test_filter_disliked_drops_a_track_by_library_id_or_by_mapping() -> None:
    """A dislike matches a library item by its id and a provider item by its mapping."""
    by_library_id = create_track("library", "7", name="Known By Id")
    by_mapping = create_track(PROV_A, "disliked-elsewhere", name="Known By Mapping")
    kept = create_track(PROV_A, "fine", name="Still Fine")
    mapping = next(iter(by_mapping.provider_mappings))
    keys: DislikedTrackKeys = ({7}, {(mapping.provider_domain, mapping.item_id)})

    assert filter_disliked([by_library_id, by_mapping, kept], keys) == [kept]

    # a user without dislikes gets the candidates back untouched
    candidates = [by_library_id, by_mapping, kept]
    assert filter_disliked(candidates, (set(), set())) is candidates


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
