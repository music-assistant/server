"""Tests for the per-user favorites (like, dislike, unset) of library items."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from music_assistant_models.auth import User, UserRole
from music_assistant_models.config_entries import ProviderAccess
from music_assistant_models.enums import EventType, MediaType, ProviderSharing
from music_assistant_models.favorite_update import FavoriteUpdate

from music_assistant.constants import DB_TABLE_FAVORITES
from music_assistant.controllers.music.favorites import PENDING_USER_ID
from music_assistant.controllers.webserver.helpers.auth_middleware import (
    current_user,
    impersonated_user,
)
from tests.common import set_music_source_access

from .helpers import create_track

if TYPE_CHECKING:
    from music_assistant_models.media_items import Track

    from music_assistant.mass import MusicAssistant

pytestmark = pytest.mark.asyncio

GET_CURRENT_USER = "music_assistant.controllers.music.media.base.get_current_user"
PROV_OWNED = "spotify--1"
PROV_HOUSEHOLD = "subsonic--1"
USER_A = "user-a"
USER_B = "user-b"


@pytest.fixture
async def favorites_mass(
    music_mass_module: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> MusicAssistant:
    """Return the library-only instance with a stand-in for the (unset up) cache."""
    monkeypatch.setattr(music_mass_module.cache, "delete", AsyncMock())
    # remove_item_from_library routes audio analysis cleanup through the AA controller
    streams = MagicMock()
    streams.audio_analysis.delete_audio_analysis = AsyncMock()
    monkeypatch.setattr(music_mass_module, "streams", streams, raising=False)
    # the store remembers the users of a sync burst; every test names its own
    music_mass_module.music.favorites._users = None
    return music_mass_module


def _user(user_id: str) -> User:
    """Return a plain member user with the given id."""
    return User(user_id=user_id, username=user_id, role=UserRole.USER)


def _known_users(mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch, *user_ids: str) -> None:
    """Let the (mocked) user store hold exactly the given users."""
    monkeypatch.setattr(
        mass.webserver.auth,
        "list_users",
        AsyncMock(return_value=[_user(user_id) for user_id in user_ids]),
    )


async def _add_track(mass: MusicAssistant, name: str, instance: str = PROV_OWNED) -> Track:
    """Add a library track that the given music source holds in its library."""
    track = create_track(instance, uuid4().hex, name=name, isrc=uuid4().hex)
    for mapping in track.provider_mappings:
        mapping.in_library = True
    return await mass.music.tracks.add_item_to_library(track)


async def _favorite_rows(mass: MusicAssistant, item_id: str) -> list[dict[str, Any]]:
    """Return the stored favorites rows of a library track."""
    return [
        dict(row)
        for row in await mass.music.database.get_rows(
            DB_TABLE_FAVORITES, {"media_type": MediaType.TRACK.value, "item_id": int(item_id)}
        )
    ]


async def test_favorites_are_personal(favorites_mass: MusicAssistant) -> None:
    """A like of one user is not visible to another, and neither is a dislike."""
    mass = favorites_mass
    liked = await _add_track(mass, "Personal Liked")
    disliked = await _add_track(mass, "Personal Disliked")
    await mass.music.tracks.set_favorite(liked.item_id, True, [USER_A])
    await mass.music.tracks.set_favorite(disliked.item_id, False, [USER_A])

    with patch(GET_CURRENT_USER, return_value=_user(USER_A)):
        assert [x.item_id for x in await mass.music.tracks.library_items(favorite=True)] == [
            liked.item_id
        ]
        assert [x.item_id for x in await mass.music.tracks.library_items(favorite=False)] == [
            disliked.item_id
        ]
        assert await mass.music.tracks.library_count(favorite_only=True) == 1
        assert (await mass.music.tracks.get_library_item(liked.item_id)).favorite is True
        assert (await mass.music.tracks.get_library_item(disliked.item_id)).favorite is False

    with patch(GET_CURRENT_USER, return_value=_user(USER_B)):
        assert await mass.music.tracks.library_items(favorite=True) == []
        assert await mass.music.tracks.library_items(favorite=False) == []
        assert await mass.music.tracks.library_count(favorite_only=True) == 0
        assert (await mass.music.tracks.get_library_item(liked.item_id)).favorite is None
        # the summary listing carries the same state as the full item
        summary = await mass.music.tracks.library_items(search="Personal Liked", summary=True)
        assert [x.favorite for x in summary] == [None]


async def test_favorites_follow_the_impersonated_user(favorites_mass: MusicAssistant) -> None:
    """A listing on behalf of another user serves that user's favorites, not the session's."""
    mass = favorites_mass
    liked_by_a = await _add_track(mass, "Impersonation Liked By A")
    liked_by_b = await _add_track(mass, "Impersonation Liked By B")
    await mass.music.tracks.set_favorite(liked_by_a.item_id, True, [USER_A])
    await mass.music.tracks.set_favorite(liked_by_b.item_id, True, [USER_B])

    # the context vars themselves: patching get_current_user would bypass the lookup under test
    session_token = current_user.set(_user(USER_A))
    impersonation_token = impersonated_user.set(_user(USER_B))
    try:
        likes = await mass.music.tracks.library_items(favorite=True, search="Impersonation")
        # a favorite filter binds the user of the favorite field too, so check that unfiltered
        listed = await mass.music.tracks.library_items(search="Impersonation")
    finally:
        impersonated_user.reset(impersonation_token)
        current_user.reset(session_token)

    assert [x.item_id for x in likes] == [liked_by_b.item_id]
    assert {x.item_id: x.favorite for x in listed} == {
        liked_by_a.item_id: None,
        liked_by_b.item_id: True,
    }


async def test_unset_favorite_keeps_a_row_and_announces_it(
    favorites_mass: MusicAssistant,
) -> None:
    """Clearing a like leaves an explicit row behind and is announced per user."""
    mass = favorites_mass
    track = await _add_track(mass, "Unset Me")
    await mass.music.tracks.set_favorite(track.item_id, True, [USER_A, USER_B])

    with patch.object(mass, "signal_event", MagicMock()) as signal_event:
        await mass.music.tracks.set_favorite(track.item_id, None, [USER_A])

    rows = {row["user_id"]: row["favorite"] for row in await _favorite_rows(mass, track.item_id)}
    # the row stays, so a provider sync can not re-like what the user just removed
    assert rows == {USER_A: None, USER_B: 1}
    assert signal_event.call_count == 1
    event_type, object_id, payload = signal_event.call_args.args
    assert event_type == EventType.FAVORITE_UPDATED
    assert object_id == track.uri
    assert payload == FavoriteUpdate(
        uri=str(track.uri),
        media_type=MediaType.TRACK,
        item_id=track.item_id,
        favorite=None,
        user_id=USER_A,
    )
    with patch(GET_CURRENT_USER, return_value=_user(USER_A)):
        assert (await mass.music.tracks.get_library_item(track.item_id)).favorite is None


async def test_provider_favorite_goes_to_the_owner_of_the_source(
    favorites_mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A source with an owner reports favorites for that user only."""
    mass = favorites_mass
    track = await _add_track(mass, "Owned Source Favorite")
    set_music_source_access(
        mass,
        {
            PROV_OWNED: ProviderAccess(owner=USER_A, sharing=ProviderSharing.PRIVATE),
            PROV_HOUSEHOLD: None,
        },
    )
    _known_users(mass, monkeypatch, USER_A, USER_B)

    await mass.music.favorites.record_from_provider(
        PROV_OWNED, MediaType.TRACK, int(track.item_id), True
    )

    assert {row["user_id"] for row in await _favorite_rows(mass, track.item_id)} == {USER_A}


async def test_provider_favorite_of_a_household_source_goes_to_everyone(
    favorites_mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A source of the whole home reports favorites for every user."""
    mass = favorites_mass
    track = await _add_track(mass, "Household Source Favorite")
    set_music_source_access(
        mass,
        {
            PROV_OWNED: ProviderAccess(owner=USER_A, sharing=ProviderSharing.PRIVATE),
            PROV_HOUSEHOLD: None,
        },
    )
    _known_users(mass, monkeypatch, USER_A, USER_B)

    await mass.music.favorites.record_from_provider(
        PROV_HOUSEHOLD, MediaType.TRACK, int(track.item_id), True
    )

    assert {row["user_id"] for row in await _favorite_rows(mass, track.item_id)} == {
        USER_A,
        USER_B,
    }


async def test_provider_favorite_of_a_source_serving_nobody_goes_nowhere(
    favorites_mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A private source that lost its owner serves nobody, so its favorites reach nobody."""
    mass = favorites_mass
    track = await _add_track(mass, "Orphaned Source Favorite")
    set_music_source_access(
        mass, {PROV_OWNED: ProviderAccess(owner=None, sharing=ProviderSharing.PRIVATE)}
    )
    _known_users(mass, monkeypatch, USER_A, USER_B)

    await mass.music.favorites.record_from_provider(
        PROV_OWNED, MediaType.TRACK, int(track.item_id), True
    )

    assert await _favorite_rows(mass, track.item_id) == []


async def test_provider_favorite_never_overrides_the_users_own_choice(
    favorites_mass: MusicAssistant,
) -> None:
    """What the user set themselves survives a provider sync reporting otherwise."""
    mass = favorites_mass
    track = await _add_track(mass, "My Own Choice")
    set_music_source_access(
        mass, {PROV_OWNED: ProviderAccess(owner=USER_A, sharing=ProviderSharing.PRIVATE)}
    )
    await mass.music.tracks.set_favorite(track.item_id, None, [USER_A])

    await mass.music.favorites.record_from_provider(
        PROV_OWNED, MediaType.TRACK, int(track.item_id), True
    )

    rows = {row["user_id"]: row["favorite"] for row in await _favorite_rows(mass, track.item_id)}
    assert rows == {USER_A: None}


async def _park_favorite(mass: MusicAssistant, item_id: int, timestamp: int) -> None:
    """Park a like the way the library migration does, for settle_pending to hand out."""
    await mass.music.database.insert(
        DB_TABLE_FAVORITES,
        {
            "user_id": PENDING_USER_ID,
            "media_type": MediaType.TRACK.value,
            "item_id": item_id,
            "favorite": True,
            "timestamp": timestamp,
        },
    )


async def test_settle_pending_hands_migrated_favorites_to_the_users_that_hold_them(
    favorites_mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A parked favorite goes to the owner of the source holding it, or to everyone."""
    mass = favorites_mass
    owned = await _add_track(mass, "Owned Favorite")
    household = await _add_track(mass, "Household Favorite", PROV_HOUSEHOLD)
    orphaned = await _add_track(mass, "Orphaned Favorite", "subsonic--2")
    set_music_source_access(
        mass,
        {
            # shared with everyone, but its favorites are the owner's alone
            PROV_OWNED: ProviderAccess(owner=USER_A, sharing=ProviderSharing.EVERYONE),
            PROV_HOUSEHOLD: None,
            # lost its owner and serves nobody
            "subsonic--2": ProviderAccess(owner=None, sharing=ProviderSharing.PRIVATE),
        },
    )
    _known_users(mass, monkeypatch, USER_A, USER_B)
    await _park_favorite(mass, int(owned.item_id), 11)
    await _park_favorite(mass, int(household.item_id), 22)
    await _park_favorite(mass, int(orphaned.item_id), 44)
    # a favorite no source holds in its library anymore
    await _park_favorite(mass, 999_999, 33)

    # a second pass has nothing left to hand out
    for _ in range(2):
        await mass.music.favorites.settle_pending()

    def _by_user(rows: list[dict[str, Any]]) -> dict[str, int]:
        return {row["user_id"]: row["timestamp"] for row in rows}

    assert _by_user(await _favorite_rows(mass, owned.item_id)) == {USER_A: 11}
    assert _by_user(await _favorite_rows(mass, household.item_id)) == {USER_A: 22, USER_B: 22}
    assert await _favorite_rows(mass, orphaned.item_id) == []
    assert _by_user(await _favorite_rows(mass, "999999")) == {USER_A: 33, USER_B: 33}
    assert not await mass.music.database.get_rows(
        DB_TABLE_FAVORITES, {"user_id": PENDING_USER_ID}, limit=1
    )


async def test_a_new_library_item_carries_the_state_its_source_reports(
    favorites_mass: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An item liked on the source it is added from is liked for that source's owner."""
    mass = favorites_mass
    set_music_source_access(
        mass, {PROV_OWNED: ProviderAccess(owner=USER_A, sharing=ProviderSharing.EVERYONE)}
    )
    _known_users(mass, monkeypatch, USER_A, USER_B)
    track = create_track(PROV_OWNED, uuid4().hex, name="Liked At The Source", isrc=uuid4().hex)
    track.favorite = True

    library_track = await mass.music.tracks.add_item_to_library(track)

    rows = {
        row["user_id"]: row["favorite"] for row in await _favorite_rows(mass, library_track.item_id)
    }
    assert rows == {USER_A: 1}


async def test_clearing_likes_keeps_the_dislikes(favorites_mass: MusicAssistant) -> None:
    """When no source holds an item anymore its likes go, a dislike is the user's own."""
    mass = favorites_mass
    track = await _add_track(mass, "Gone From Every Library")
    await mass.music.tracks.set_favorite(track.item_id, True, [USER_A])
    await mass.music.tracks.set_favorite(track.item_id, False, [USER_B])

    await mass.music.favorites.clear_likes(MediaType.TRACK, int(track.item_id))

    rows = {row["user_id"]: row["favorite"] for row in await _favorite_rows(mass, track.item_id)}
    assert rows == {USER_B: 0}


async def test_removing_an_item_drops_its_favorites(favorites_mass: MusicAssistant) -> None:
    """The favorites of a removed library item do not outlive it."""
    mass = favorites_mass
    track = await _add_track(mass, "Removed Again")
    await mass.music.tracks.set_favorite(track.item_id, True, [USER_A])

    await mass.music.tracks.remove_item_from_library(track.item_id)

    assert await _favorite_rows(mass, track.item_id) == []
