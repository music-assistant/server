"""Tests for moving an item of a playlist to another position."""

from __future__ import annotations

from contextlib import ExitStack
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from music_assistant_models.access import PlaylistAccess
from music_assistant_models.auth import User, UserRole
from music_assistant_models.enums import EventType, ProviderFeature, ProviderSharing
from music_assistant_models.errors import InsufficientPermissions, InvalidDataError
from music_assistant_models.media_items import Playlist, ProviderMapping

from music_assistant.controllers.cache import BYPASS_CACHE
from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from music_assistant.controllers.music.media.playlists import PlaylistController
    from music_assistant.mass import MusicAssistant

# the server fixture below is module scoped, so the tests must share its event loop
pytestmark = pytest.mark.asyncio(loop_scope="module")

OWNER = User(user_id="user-owner", username="owner", role=UserRole.USER)
MEMBER = User(user_id="user-member", username="member", role=UserRole.USER)
USERS = {user.user_id: user for user in (OWNER, MEMBER)}


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


async def _add(
    playlists: PlaylistController,
    is_editable: bool = True,
    access: PlaylistAccess | None = None,
) -> Playlist:
    """Add a builtin playlist to the library as an internal caller and return it."""
    item_id = uuid4().hex
    playlist = Playlist(
        item_id=item_id,
        provider="builtin",
        name="Reordered",
        provider_mappings={
            ProviderMapping(
                item_id=item_id,
                provider_domain="builtin",
                provider_instance="builtin",
                in_library=True,
            )
        },
        owner="Music Assistant",
        is_editable=is_editable,
        access=access,
    )
    with _as_user(None):
        return await playlists.add_item_to_library(playlist)


def _provider(features: set[ProviderFeature] | None = None) -> MagicMock:
    """Return a mocked music provider holding a playlist of two pages."""
    provider = MagicMock(spec=MusicProvider)
    provider.domain = provider.instance_id = "builtin"
    provider.name = "Music Assistant"
    provider.available = True
    provider.supported_features = (
        {ProviderFeature.PLAYLIST_TRACKS_EDIT, ProviderFeature.PLAYLIST_TRACKS_MOVE}
        if features is None
        else features
    )
    provider.move_playlist_track = AsyncMock()
    provider.bypass_seen = []

    async def _get_playlist_tracks(_prov_playlist_id: str, page: int = 0) -> list[MagicMock]:
        provider.bypass_seen.append(BYPASS_CACHE.get())
        return [MagicMock()] if page < 2 else []

    provider.get_playlist_tracks = AsyncMock(side_effect=_get_playlist_tracks)
    return provider


async def test_move_reaches_the_provider_and_announces_the_new_order(
    playlists: PlaylistController, music_mass_module: MusicAssistant
) -> None:
    """A move is handed to the provider, read back past the caches, then announced."""
    added = await _add(playlists)
    provider = _provider()
    prov_item_id = next(iter(added.provider_mappings)).item_id

    with (
        _as_user(OWNER),
        patch.object(music_mass_module, "get_provider", return_value=provider),
        patch.object(music_mass_module, "signal_event") as signal_event,
    ):
        await playlists.move_playlist_track(added.item_id, 3, 1)

    provider.move_playlist_track.assert_awaited_once_with(prov_item_id, 3, 1)
    # every page is read again, and every read bypasses the cache
    assert [call.kwargs["page"] for call in provider.get_playlist_tracks.await_args_list] == [
        0,
        1,
        2,
    ]
    assert provider.bypass_seen == [True, True, True]
    signal_event.assert_called_once()
    assert signal_event.call_args.args[0] == EventType.MEDIA_ITEM_UPDATED
    assert (await playlists.get_library_item(added.item_id)).metadata.last_refresh is None


async def test_move_without_the_feature_is_refused(
    playlists: PlaylistController, music_mass_module: MusicAssistant
) -> None:
    """A provider that can edit but not reorder its playlists is never asked to move."""
    added = await _add(playlists)
    provider = _provider({ProviderFeature.PLAYLIST_TRACKS_EDIT})

    with (
        _as_user(OWNER),
        patch.object(music_mass_module, "get_provider", return_value=provider),
        pytest.raises(InvalidDataError),
    ):
        await playlists.move_playlist_track(added.item_id, 1, 2)

    provider.move_playlist_track.assert_not_awaited()


async def test_move_in_a_playlist_that_is_not_editable_is_refused(
    playlists: PlaylistController, music_mass_module: MusicAssistant
) -> None:
    """A playlist the provider marks read-only is never reordered."""
    added = await _add(playlists, is_editable=False)
    provider = _provider()

    with (
        _as_user(OWNER),
        patch.object(music_mass_module, "get_provider", return_value=provider),
        pytest.raises(InvalidDataError),
    ):
        await playlists.move_playlist_track(added.item_id, 1, 2)

    provider.move_playlist_track.assert_not_awaited()


async def test_move_needs_the_right_to_edit(
    playlists: PlaylistController, music_mass_module: MusicAssistant
) -> None:
    """Reordering follows the same edit rights as adding and removing items."""
    access = PlaylistAccess(owner=OWNER.user_id, sharing=ProviderSharing.MEMBERS)
    added = await _add(playlists, access=access)
    provider = _provider()

    with (
        _as_user(MEMBER),
        patch.object(music_mass_module, "get_provider", return_value=provider),
        pytest.raises(InsufficientPermissions),
    ):
        await playlists.move_playlist_track(added.item_id, 1, 2)

    provider.move_playlist_track.assert_not_awaited()


async def test_move_to_the_same_position_changes_nothing(
    playlists: PlaylistController, music_mass_module: MusicAssistant
) -> None:
    """Moving an item onto its own position neither writes nor announces anything."""
    added = await _add(playlists)
    provider = _provider()

    with (
        _as_user(OWNER),
        patch.object(music_mass_module, "get_provider", return_value=provider),
        patch.object(music_mass_module, "signal_event") as signal_event,
    ):
        await playlists.move_playlist_track(added.item_id, 2, 2)

    provider.move_playlist_track.assert_not_awaited()
    provider.get_playlist_tracks.assert_not_awaited()
    signal_event.assert_not_called()


@pytest.mark.parametrize(("from_position", "to_position"), [(0, 1), (1, 0)])
async def test_move_below_the_first_position_is_refused(
    playlists: PlaylistController,
    music_mass_module: MusicAssistant,
    from_position: int,
    to_position: int,
) -> None:
    """Positions start at 1, so anything lower never reaches the provider."""
    added = await _add(playlists)
    provider = _provider()

    with (
        _as_user(OWNER),
        patch.object(music_mass_module, "get_provider", return_value=provider),
        pytest.raises(InvalidDataError),
    ):
        await playlists.move_playlist_track(added.item_id, from_position, to_position)

    provider.move_playlist_track.assert_not_awaited()
