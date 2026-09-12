"""Tests for who may see, play, edit and share a Music Assistant playlist."""

from __future__ import annotations

from contextlib import ExitStack
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from music_assistant_models.access import PlaylistAccess
from music_assistant_models.auth import User, UserRole
from music_assistant_models.enums import MediaType, ProviderFeature, ProviderSharing
from music_assistant_models.errors import (
    InsufficientPermissions,
    InvalidDataError,
    MediaNotFoundError,
)
from music_assistant_models.media_items import Genre, Playlist, ProviderMapping

from music_assistant.constants import (
    DB_TABLE_GENRE_MEDIA_ITEM_MAPPING,
    DB_TABLE_PLAYLISTS,
    HOMEASSISTANT_SYSTEM_USER,
)
from music_assistant.controllers.music.constants import CACHE_CATEGORY_SEARCH_RESULTS

if TYPE_CHECKING:
    from collections.abc import Iterator

    from music_assistant.controllers.music.media.playlists import PlaylistController
    from music_assistant.mass import MusicAssistant

# the server fixture below is module scoped, so the tests must share its event loop
pytestmark = pytest.mark.asyncio(loop_scope="module")

OWNER = User(user_id="user-owner", username="owner", role=UserRole.USER)
MEMBER = User(user_id="user-member", username="member", role=UserRole.USER)
GUEST = User(user_id="user-guest", username="guest", role=UserRole.GUEST)
ADMIN = User(user_id="user-admin", username="admin", role=UserRole.ADMIN)
HA_SYSTEM = User(user_id="user-ha", username=HOMEASSISTANT_SYSTEM_USER, role=UserRole.SERVICE)
USERS = {user.user_id: user for user in (OWNER, MEMBER, GUEST, ADMIN, HA_SYSTEM)}


@pytest.fixture
async def playlists(
    music_mass_module: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> PlaylistController:
    """Return the playlist controller of a database-only server with a mocked user store."""
    monkeypatch.setattr(
        music_mass_module.webserver.auth, "get_user", AsyncMock(side_effect=USERS.get)
    )
    # the database-only server runs no cache database
    monkeypatch.setattr(music_mass_module.cache, "delete", AsyncMock())
    return music_mass_module.music.playlists


def _as_user(user: User | None) -> ExitStack:
    """Run the enclosed block as the given user (None for an internal caller)."""
    stack = ExitStack()
    for module in ("media.playlists", "media.base", "controller"):
        stack.enter_context(
            patch(
                f"music_assistant.controllers.music.{module}.get_current_user",
                return_value=user,
            )
        )
    return stack


def _playlist(
    name: str,
    access: PlaylistAccess | None = None,
    provider_domain: str = "builtin",
    item_id: str | None = None,
) -> Playlist:
    """Build a provider playlist ready to be added to the library."""
    item_id = item_id or uuid4().hex
    return Playlist(
        item_id=item_id,
        provider=provider_domain,
        name=name,
        provider_mappings={
            ProviderMapping(
                item_id=item_id,
                provider_domain=provider_domain,
                provider_instance=provider_domain,
                in_library=True,
            )
        },
        owner="Music Assistant",
        is_editable=True,
        access=access,
    )


async def _add(playlists: PlaylistController, playlist: Playlist) -> Playlist:
    """Add the playlist to the library as an internal caller and return the library item."""
    with _as_user(None):
        return await playlists.add_item_to_library(playlist)


async def _visible_ids(playlists: PlaylistController, user: User | None) -> set[str]:
    """Return the library ids the given user sees in the playlist listing."""
    with _as_user(user):
        return {item.item_id for item in await playlists.library_items(limit=1000)}


def _sharing_cases() -> Iterator[Any]:
    """Yield (record, user, visible) cases, one per sharing mode and viewer."""
    private = PlaylistAccess(owner=OWNER.user_id)
    selected = PlaylistAccess(
        owner=OWNER.user_id, sharing=ProviderSharing.SELECTED, shared_users=[MEMBER.user_id]
    )
    members = PlaylistAccess(owner=OWNER.user_id, sharing=ProviderSharing.MEMBERS)
    everyone = PlaylistAccess(owner=OWNER.user_id, sharing=ProviderSharing.EVERYONE)
    yield pytest.param(private, OWNER, True, id="private-owner")
    yield pytest.param(private, MEMBER, False, id="private-member")
    yield pytest.param(private, ADMIN, False, id="private-admin")
    yield pytest.param(selected, MEMBER, True, id="selected-listed-member")
    yield pytest.param(selected, GUEST, False, id="selected-other-user")
    yield pytest.param(members, MEMBER, True, id="members-member")
    yield pytest.param(members, ADMIN, True, id="members-admin")
    yield pytest.param(members, GUEST, False, id="members-guest")
    yield pytest.param(everyone, GUEST, True, id="everyone-guest")
    yield pytest.param(None, GUEST, True, id="household-guest")


@pytest.mark.parametrize(("access", "user", "visible"), list(_sharing_cases()))
async def test_listing_follows_the_access_record(
    playlists: PlaylistController, access: PlaylistAccess | None, user: User, visible: bool
) -> None:
    """A listing only holds the playlists the calling user may see, and counts the same way."""
    added = await _add(playlists, _playlist("Listing", access))

    assert (added.item_id in await _visible_ids(playlists, user)) is visible
    with _as_user(user):
        count = await playlists.library_count()
    assert count == len(await _visible_ids(playlists, user))


async def test_listing_is_unfiltered_for_internal_callers(playlists: PlaylistController) -> None:
    """Without a user context every playlist is listed, the sync and other internals rely on it."""
    added = await _add(playlists, _playlist("Internal", PlaylistAccess(owner=OWNER.user_id)))

    assert added.item_id in await _visible_ids(playlists, None)


async def test_access_record_survives_the_library_round_trip(
    playlists: PlaylistController,
) -> None:
    """The record is served on the full item and on the summary item alike."""
    access = PlaylistAccess(
        owner=OWNER.user_id,
        sharing=ProviderSharing.SELECTED,
        shared_users=[MEMBER.user_id],
        collaborative=True,
    )
    added = await _add(playlists, _playlist("Round trip", access))

    assert added.access == access
    with _as_user(OWNER):
        summaries = await playlists.library_items(search="Round trip")
        full_items = await playlists.library_items(search="Round trip", summary=False)
    assert [x.access for x in summaries if x.item_id == added.item_id] == [access]
    assert [x.access for x in full_items if x.item_id == added.item_id] == [access]


async def test_only_a_music_assistant_playlist_keeps_a_record(
    playlists: PlaylistController,
) -> None:
    """A playlist of a music service follows the access of that service instead."""
    added = await _add(
        playlists,
        _playlist("Service", PlaylistAccess(owner=OWNER.user_id), provider_domain="spotify"),
    )

    assert added.access is None


async def test_unreadable_record_hides_the_playlist(
    playlists: PlaylistController, music_mass_module: MusicAssistant
) -> None:
    """A record that can not be read hides its playlist instead of exposing it."""
    added = await _add(playlists, _playlist("Broken", PlaylistAccess(owner=OWNER.user_id)))
    await music_mass_module.music.database.update(
        DB_TABLE_PLAYLISTS, {"item_id": int(added.item_id)}, {"access": "{not json"}
    )

    assert added.item_id not in await _visible_ids(playlists, OWNER)


async def test_get_and_tracks_hide_a_playlist_the_caller_may_not_see(
    playlists: PlaylistController,
) -> None:
    """A single-item fetch behaves as if the playlist does not exist, by library and builtin id."""
    added = await _add(playlists, _playlist("Hidden", PlaylistAccess(owner=OWNER.user_id)))
    builtin_id = next(iter(added.provider_mappings)).item_id

    with _as_user(OWNER):
        assert (await playlists.get(added.item_id, "library")).item_id == added.item_id
        assert [x async for x in playlists.tracks(builtin_id, "builtin")] == []
    with _as_user(MEMBER), pytest.raises(MediaNotFoundError):
        await playlists.get(added.item_id, "library")
    with _as_user(MEMBER), pytest.raises(MediaNotFoundError):
        _ = [x async for x in playlists.tracks(added.item_id, "library")]
    with _as_user(MEMBER), pytest.raises(MediaNotFoundError):
        _ = [x async for x in playlists.tracks(builtin_id, "builtin")]


async def test_internal_callers_see_every_playlist(playlists: PlaylistController) -> None:
    """Without a user context (metadata refresh, background tasks) nothing is hidden."""
    added = await _add(playlists, _playlist("Internal", PlaylistAccess(owner=OWNER.user_id)))
    builtin_id = next(iter(added.provider_mappings)).item_id

    with _as_user(None):
        assert (await playlists.get(added.item_id, "library")).item_id == added.item_id
        assert [x async for x in playlists.tracks(builtin_id, "builtin")] == []
        playlists.check_removal_allowed(added)


async def test_sync_update_keeps_the_record(playlists: PlaylistController) -> None:
    """A provider sync re-adds the playlist without a record, which must not drop it."""
    access = PlaylistAccess(owner=OWNER.user_id, sharing=ProviderSharing.MEMBERS)
    added = await _add(playlists, _playlist("Resynced", access))
    builtin_id = next(iter(added.provider_mappings)).item_id

    resynced = await _add(playlists, _playlist("Resynced", item_id=builtin_id))

    assert resynced.item_id == added.item_id
    assert resynced.access == access


async def test_get_library_item_command_hides_hidden_playlists(
    playlists: PlaylistController, music_mass_module: MusicAssistant
) -> None:
    """The library lookup command does not hand out a playlist the caller may not see."""
    added = await _add(playlists, _playlist("Looked up", PlaylistAccess(owner=OWNER.user_id)))
    lookup = music_mass_module.music.get_library_item_by_prov_id

    with _as_user(OWNER):
        found = await lookup(MediaType.PLAYLIST, added.item_id, "library")
    assert found is not None
    assert found.item_id == added.item_id
    with _as_user(MEMBER):
        assert await lookup(MediaType.PLAYLIST, added.item_id, "library") is None


async def test_genre_overview_hides_hidden_playlists(
    playlists: PlaylistController, music_mass_module: MusicAssistant
) -> None:
    """The playlists row of a genre overview only holds what the caller may see."""
    added = await _add(playlists, _playlist("Genre bound", PlaylistAccess(owner=OWNER.user_id)))
    genre = await music_mass_module.music.genres.add_item_to_library(
        Genre(item_id="0", provider="library", name=f"Genre {uuid4().hex}", provider_mappings=set())
    )
    await music_mass_module.music.database.insert(
        DB_TABLE_GENRE_MEDIA_ITEM_MAPPING,
        {
            "genre_id": int(genre.item_id),
            "media_id": int(added.item_id),
            "media_type": MediaType.PLAYLIST.value,
        },
    )

    with _as_user(OWNER):
        owner_rows = await music_mass_module.music.genres.get_overview(genre.item_id)
    with _as_user(MEMBER):
        member_rows = await music_mass_module.music.genres.get_overview(genre.item_id)

    assert [x.item_id for row in owner_rows for x in row.items] == [added.item_id]
    assert member_rows == []


async def test_search_results_are_cached_per_user(
    playlists: PlaylistController, music_mass_module: MusicAssistant
) -> None:
    """A cached search never serves one user the private playlists of another."""
    added = await _add(playlists, _playlist("Cachedsearch", PlaylistAccess(owner=OWNER.user_id)))
    cache_keys: list[str] = []

    async def fake_cache_get(key: str, **_kwargs: Any) -> None:
        cache_keys.append(key)

    # an empty library search retries through the translations controller
    translations = MagicMock()
    translations.reverse_lookup_media_names = AsyncMock(return_value=[])
    with (
        patch.object(music_mass_module.cache, "get", fake_cache_get),
        patch.object(music_mass_module.cache, "set", AsyncMock()),
        patch.object(music_mass_module, "translations", translations, create=True),
    ):
        with _as_user(OWNER):
            owner_results = await music_mass_module.music.search(
                "Cachedsearch", providers=["library"]
            )
        with _as_user(MEMBER):
            member_results = await music_mass_module.music.search(
                "Cachedsearch", providers=["library"]
            )
        # the same member, demoted to guest, gets an entry of its own as well
        with _as_user(User(user_id=MEMBER.user_id, username=MEMBER.username, role=UserRole.GUEST)):
            await music_mass_module.music.search("Cachedsearch", providers=["library"])

    assert [x.item_id for x in owner_results.playlists] == [added.item_id]
    assert member_results.playlists == []
    assert len(set(cache_keys)) == 3


async def test_favorite_removal_hides_hidden_playlists(
    playlists: PlaylistController, music_mass_module: MusicAssistant
) -> None:
    """The shared favorite flag of a playlist is only changed by someone who may see it."""
    added = await _add(playlists, _playlist("Favorite", PlaylistAccess(owner=OWNER.user_id)))
    await playlists.set_favorite(added.item_id, True)
    remove = music_mass_module.music.remove_item_from_favorites

    with _as_user(MEMBER), pytest.raises(MediaNotFoundError):
        await remove(MediaType.PLAYLIST, added.item_id)
    assert (await playlists.get_library_item(added.item_id)).favorite is True
    with _as_user(OWNER):
        await remove(MediaType.PLAYLIST, added.item_id)
    assert (await playlists.get_library_item(added.item_id)).favorite is False


async def test_sync_lookups_stay_unfiltered(playlists: PlaylistController) -> None:
    """The lookups the library sync uses find a hidden playlist, so it is never added twice."""
    added = await _add(playlists, _playlist("Synced", PlaylistAccess(owner=OWNER.user_id)))

    with _as_user(MEMBER):
        found = await playlists.get_library_item_by_prov_mappings(added.provider_mappings)
    assert found is not None
    assert found.item_id == added.item_id


@pytest.mark.parametrize(
    ("user", "expected"),
    [
        pytest.param(OWNER, PlaylistAccess(owner=OWNER.user_id), id="member"),
        pytest.param(HA_SYSTEM, None, id="home-assistant-system-user"),
        pytest.param(None, None, id="internal"),
    ],
)
async def test_create_playlist_records_the_creator_as_owner(
    playlists: PlaylistController,
    music_mass_module: MusicAssistant,
    user: User | None,
    expected: PlaylistAccess | None,
) -> None:
    """A new Music Assistant playlist is private to its creator; system callers make household ones."""
    provider = MagicMock()
    provider.domain = provider.instance_id = "builtin"
    provider.name = "Music Assistant"
    provider.supported_features = {
        ProviderFeature.PLAYLIST_CREATE_TRACKS,
        ProviderFeature.PLAYLIST_TRACKS_EDIT,
    }
    provider.create_playlist = AsyncMock(return_value=_playlist("Created"))

    with (
        _as_user(user),
        patch.object(music_mass_module, "get_provider", return_value=provider),
        patch("music_assistant.controllers.music.media.playlists.MusicProvider", MagicMock),
    ):
        created = await playlists.create_playlist("Created", media_types=[MediaType.TRACK])

    assert created.access == expected


async def test_owner_shares_its_playlist(
    playlists: PlaylistController, music_mass_module: MusicAssistant
) -> None:
    """The owner decides who may see its playlist and whether they may edit it."""
    added = await _add(playlists, _playlist("Mine", PlaylistAccess(owner=OWNER.user_id)))

    with _as_user(OWNER):
        updated = await playlists.set_access(
            added.item_id,
            ProviderSharing.SELECTED,
            owner=OWNER.user_id,
            shared_users=[MEMBER.user_id, OWNER.user_id, MEMBER.user_id],
            collaborative=True,
        )

    assert updated.access == PlaylistAccess(
        owner=OWNER.user_id,
        sharing=ProviderSharing.SELECTED,
        shared_users=[MEMBER.user_id],
        collaborative=True,
    )
    assert added.item_id in await _visible_ids(playlists, MEMBER)
    # cached search results held the playlists a user could see before the change
    dropped = music_mass_module.cache.delete
    assert isinstance(dropped, AsyncMock)
    dropped.assert_awaited_once_with(None, category=CACHE_CATEGORY_SEARCH_RESULTS, provider="music")


async def test_set_access_refuses_everyone_but_the_owner_or_an_admin(
    playlists: PlaylistController,
) -> None:
    """A member the playlist is shared with may not change its sharing or owner."""
    added = await _add(
        playlists,
        _playlist("Shared", PlaylistAccess(owner=OWNER.user_id, sharing=ProviderSharing.MEMBERS)),
    )

    with _as_user(MEMBER), pytest.raises(InsufficientPermissions) as err:
        await playlists.set_access(added.item_id, ProviderSharing.EVERYONE, owner=OWNER.user_id)
    assert err.value.translation_key == "playlist_not_owned"
    with _as_user(OWNER), pytest.raises(InsufficientPermissions):
        await playlists.set_access(added.item_id, ProviderSharing.PRIVATE, owner=MEMBER.user_id)
    with _as_user(ADMIN):
        updated = await playlists.set_access(
            added.item_id, ProviderSharing.PRIVATE, owner=MEMBER.user_id
        )
    assert updated.access == PlaylistAccess(owner=MEMBER.user_id)
    # a library manager repairs a private playlist it can not see itself
    with _as_user(ADMIN):
        repaired = await playlists.set_access(
            added.item_id, ProviderSharing.MEMBERS, owner=OWNER.user_id
        )
    assert repaired.access == PlaylistAccess(owner=OWNER.user_id, sharing=ProviderSharing.MEMBERS)


async def test_set_access_validates_the_users_on_the_record(
    playlists: PlaylistController,
) -> None:
    """Unknown users are refused; a guest and the Home Assistant system user can not own one."""
    added = await _add(playlists, _playlist("Validated"))

    with _as_user(ADMIN), pytest.raises(InvalidDataError):
        await playlists.set_access(added.item_id, ProviderSharing.PRIVATE, owner="nobody")
    with _as_user(ADMIN), pytest.raises(InvalidDataError):
        await playlists.set_access(added.item_id, ProviderSharing.PRIVATE, owner=GUEST.user_id)
    with _as_user(ADMIN), pytest.raises(InvalidDataError):
        await playlists.set_access(
            added.item_id, ProviderSharing.SELECTED, owner=OWNER.user_id, shared_users=["nobody"]
        )
    with _as_user(ADMIN), pytest.raises(InvalidDataError):
        await playlists.set_access(added.item_id, ProviderSharing.PRIVATE, owner=HA_SYSTEM.user_id)
    with _as_user(MEMBER), pytest.raises(InsufficientPermissions):
        # a household playlist is only handed out by an admin
        await playlists.set_access(added.item_id, ProviderSharing.PRIVATE, owner=MEMBER.user_id)


async def test_set_access_keeps_an_ownerless_playlist_visible(
    playlists: PlaylistController,
) -> None:
    """A playlist without an owner must be shared with someone, and only an admin edits it."""
    added = await _add(playlists, _playlist("Curated"))

    with _as_user(ADMIN), pytest.raises(InvalidDataError):
        await playlists.set_access(added.item_id, ProviderSharing.PRIVATE)
    with _as_user(ADMIN), pytest.raises(InvalidDataError):
        await playlists.set_access(added.item_id, ProviderSharing.SELECTED, shared_users=[])
    with _as_user(ADMIN):
        curated = await playlists.set_access(added.item_id, ProviderSharing.MEMBERS)
    assert curated.access == PlaylistAccess(owner=None, sharing=ProviderSharing.MEMBERS)
    assert added.item_id in await _visible_ids(playlists, MEMBER)
    with _as_user(MEMBER), pytest.raises(InsufficientPermissions):
        await playlists.add_playlist_tracks(added.item_id, ["library://track/1"])


async def test_set_access_refuses_a_music_service_playlist(playlists: PlaylistController) -> None:
    """A playlist of a music service keeps following the sharing of that service."""
    added = await _add(playlists, _playlist("Service", provider_domain="spotify"))

    with _as_user(ADMIN), pytest.raises(InvalidDataError) as err:
        await playlists.set_access(added.item_id, ProviderSharing.PRIVATE, owner=OWNER.user_id)
    assert err.value.translation_key == "playlist_follows_source"


@pytest.mark.parametrize(
    ("access", "user", "allowed"),
    [
        pytest.param(None, MEMBER, True, id="household-anyone"),
        pytest.param(
            PlaylistAccess(owner=OWNER.user_id, sharing=ProviderSharing.MEMBERS),
            OWNER,
            True,
            id="shared-owner",
        ),
        pytest.param(
            PlaylistAccess(owner=OWNER.user_id, sharing=ProviderSharing.MEMBERS),
            MEMBER,
            False,
            id="shared-member",
        ),
        pytest.param(
            PlaylistAccess(owner=OWNER.user_id, sharing=ProviderSharing.MEMBERS),
            ADMIN,
            True,
            id="shared-admin",
        ),
        pytest.param(
            PlaylistAccess(
                owner=OWNER.user_id, sharing=ProviderSharing.MEMBERS, collaborative=True
            ),
            MEMBER,
            True,
            id="collaborative-member",
        ),
        pytest.param(
            PlaylistAccess(owner=OWNER.user_id, collaborative=True),
            MEMBER,
            False,
            id="collaborative-but-private",
        ),
        pytest.param(PlaylistAccess(owner=OWNER.user_id), ADMIN, True, id="private-admin"),
    ],
)
async def test_editing_the_items_needs_the_owner_unless_collaborative(
    playlists: PlaylistController,
    music_mass_module: MusicAssistant,
    access: PlaylistAccess | None,
    user: User,
    allowed: bool,
) -> None:
    """Adding and removing items is for the owner, or for everyone once the flag is set."""
    added = await _add(playlists, _playlist("Edited", access))
    expected_error: type[Exception] = InsufficientPermissions
    if access is not None and access.sharing == ProviderSharing.PRIVATE and user != OWNER:
        expected_error = MediaNotFoundError

    with (
        _as_user(user),
        patch.object(music_mass_module.tasks, "run_background_task") as run_task,
    ):
        if allowed:
            await playlists.add_playlist_tracks(added.item_id, ["library://track/1"])
            await playlists.remove_playlist_tracks(added.item_id, (1,))
            assert run_task.call_count == 2
        else:
            with pytest.raises(expected_error):
                await playlists.add_playlist_tracks(added.item_id, ["library://track/1"])
            with pytest.raises(expected_error):
                await playlists.remove_playlist_tracks(added.item_id, (1,))
            run_task.assert_not_called()


async def test_removal_needs_the_owner_even_when_collaborative(
    playlists: PlaylistController,
) -> None:
    """Deleting a personal playlist stays with its owner or an admin."""
    access = PlaylistAccess(
        owner=OWNER.user_id, sharing=ProviderSharing.MEMBERS, collaborative=True
    )
    added = await _add(playlists, _playlist("Removed", access))
    household = await _add(playlists, _playlist("Household"))

    private = await _add(playlists, _playlist("Private", PlaylistAccess(owner=OWNER.user_id)))

    with _as_user(MEMBER), pytest.raises(InsufficientPermissions):
        playlists.check_removal_allowed(added)
    with _as_user(MEMBER), pytest.raises(MediaNotFoundError):
        # a hidden playlist is not even confirmed to exist
        playlists.check_removal_allowed(private)
    with _as_user(ADMIN):
        # a library manager is never masked
        playlists.check_removal_allowed(private)
    with _as_user(MEMBER):
        playlists.check_removal_allowed(household)
    with _as_user(OWNER):
        playlists.check_removal_allowed(added)
    with _as_user(ADMIN):
        playlists.check_removal_allowed(added)


async def test_adding_a_hidden_playlist_as_source_is_refused_inside_the_task(
    playlists: PlaylistController, music_mass_module: MusicAssistant
) -> None:
    """The items to add are resolved as the caller, so a hidden playlist can not be copied."""
    hidden = await _add(playlists, _playlist("Hidden source", PlaylistAccess(owner=OWNER.user_id)))
    target = await _add(playlists, _playlist("Target", PlaylistAccess(owner=MEMBER.user_id)))
    read_only = await _add(
        playlists,
        _playlist(
            "Read only", PlaylistAccess(owner=OWNER.user_id, sharing=ProviderSharing.MEMBERS)
        ),
    )
    provider = MagicMock()
    provider.domain = provider.instance_id = "builtin"
    provider.available = True
    provider.supported_features = {ProviderFeature.PLAYLIST_TRACKS_EDIT}
    provider.get_playlist_tracks = AsyncMock(return_value=[])

    with (
        patch.object(music_mass_module, "get_provider", return_value=provider),
        patch("music_assistant.controllers.music.media.playlists.MusicProvider", MagicMock),
    ):
        with pytest.raises(MediaNotFoundError):
            await playlists._handle_add_playlist_tracks(
                target.item_id, [f"library://playlist/{hidden.item_id}"], MEMBER.user_id
            )
        # the right to edit the target is checked again when the task runs
        with pytest.raises(InsufficientPermissions):
            await playlists._handle_add_playlist_tracks(read_only.item_id, [], MEMBER.user_id)
        # as is the account itself: disabled, removed or without the right to change the library
        with pytest.raises(InsufficientPermissions):
            await playlists._handle_add_playlist_tracks(target.item_id, [], "user-gone")
        with pytest.raises(InsufficientPermissions):
            await playlists._handle_add_playlist_tracks(target.item_id, [], GUEST.user_id)


async def test_library_add_command_ignores_a_supplied_record(
    music_mass_module: MusicAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Adding a playlist through the generic library command never sets its owner or sharing."""
    monkeypatch.setattr(music_mass_module.metadata, "update_metadata", AsyncMock())

    with _as_user(MEMBER):
        added = await music_mass_module.music.add_item_to_library(
            _playlist("Crafted", PlaylistAccess(owner=OWNER.user_id))
        )

    assert isinstance(added, Playlist)
    assert added.access is None


async def test_library_add_of_a_matching_item_needs_the_right_to_edit(
    playlists: PlaylistController,
) -> None:
    """Re-adding a provider item that matches a personal playlist rewrites it only for its owner."""
    access = PlaylistAccess(
        owner=OWNER.user_id, sharing=ProviderSharing.MEMBERS, collaborative=True
    )
    added = await _add(playlists, _playlist("Original", access))
    builtin_id = next(iter(added.provider_mappings)).item_id

    # a collaborator may change the items, not the record itself
    with _as_user(MEMBER), pytest.raises(InsufficientPermissions):
        await playlists.add_item_to_library(
            _playlist("Hijacked", item_id=builtin_id), overwrite_existing=True
        )
    assert (await playlists.get_library_item(added.item_id)).name == "Original"
    with _as_user(OWNER):
        renamed = await playlists.add_item_to_library(
            _playlist("Renamed", item_id=builtin_id), overwrite_existing=True
        )
    assert renamed.item_id == added.item_id
    assert renamed.name == "Renamed"
    assert renamed.access == added.access


async def test_collaborator_edit_completes_its_bookkeeping(
    playlists: PlaylistController, music_mass_module: MusicAssistant
) -> None:
    """The refresh marker an item change leaves behind needs no right to rewrite the record."""
    access = PlaylistAccess(
        owner=OWNER.user_id, sharing=ProviderSharing.MEMBERS, collaborative=True
    )
    added = await _add(playlists, _playlist("Collaborative", access))
    provider = MagicMock()
    provider.domain = provider.instance_id = "builtin"
    provider.available = True
    provider.supported_features = {ProviderFeature.PLAYLIST_TRACKS_EDIT}
    provider.remove_playlist_tracks = AsyncMock()

    with (
        patch.object(music_mass_module, "get_provider", return_value=provider),
        patch("music_assistant.controllers.music.media.playlists.MusicProvider", MagicMock),
    ):
        await playlists._handle_remove_playlist_tracks(added.item_id, (1,), MEMBER.user_id)

    provider.remove_playlist_tracks.assert_awaited_once()
    assert (await playlists.get_library_item(added.item_id)).metadata.last_refresh is None


async def test_release_user_playlists(
    playlists: PlaylistController, music_mass_module: MusicAssistant
) -> None:
    """A removed user's playlists become household playlists and it leaves every share list."""
    owned = await _add(playlists, _playlist("Owned", PlaylistAccess(owner=MEMBER.user_id)))
    sole_recipient = await _add(
        playlists,
        _playlist(
            "Sole recipient",
            PlaylistAccess(sharing=ProviderSharing.SELECTED, shared_users=[MEMBER.user_id]),
        ),
    )
    shared_with = await _add(
        playlists,
        _playlist(
            "Shared with",
            PlaylistAccess(
                owner=OWNER.user_id,
                sharing=ProviderSharing.SELECTED,
                shared_users=[MEMBER.user_id, GUEST.user_id],
            ),
        ),
    )
    untouched = await _add(playlists, _playlist("Untouched", PlaylistAccess(owner=OWNER.user_id)))
    unreadable = await _add(
        playlists, _playlist("Unreadable", PlaylistAccess(owner=MEMBER.user_id))
    )
    await music_mass_module.music.database.update(
        DB_TABLE_PLAYLISTS,
        {"item_id": int(unreadable.item_id)},
        {"access": '{"owner": "user-member", "shared_users": "not a list"}'},
    )

    await playlists.release_user_playlists(MEMBER.user_id)

    assert (await playlists.get_library_item(owned.item_id)).access is None
    # nobody would be left who may see the ownerless playlist, so it becomes household
    assert (await playlists.get_library_item(sole_recipient.item_id)).access is None
    assert (await playlists.get_library_item(shared_with.item_id)).access == PlaylistAccess(
        owner=OWNER.user_id, sharing=ProviderSharing.SELECTED, shared_users=[GUEST.user_id]
    )
    assert (await playlists.get_library_item(untouched.item_id)).access == untouched.access
    # the record only we write never looks like this, so the row must not outlive the test
    await playlists.remove_item_from_library(unreadable.item_id)
