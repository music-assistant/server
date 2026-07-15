"""Tests for the party plugin."""

from __future__ import annotations

import asyncio
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.auth import Scope
from music_assistant_models.enums import MediaType, PlaybackState
from music_assistant_models.errors import InvalidDataError

from music_assistant.helpers.shared_playback import SharedPlaybackMode
from music_assistant.providers.party import (
    CONF_ENABLE_ADD_QUEUE,
    CONF_ENABLE_BOOST,
    CONF_ENABLE_GUEST_ACCESS,
    CONF_PARTY_MODE,
    CONF_PREVENT_DUPLICATE_TRACKS,
    PartyPlugin,
)


def _create_party_plugin() -> PartyPlugin:
    """Create a minimally configured party plugin for unit tests."""
    plugin = PartyPlugin.__new__(PartyPlugin)
    plugin.mass = MagicMock()
    plugin.mass.music = MagicMock()
    plugin.mass.player_queues = MagicMock()
    plugin.logger = MagicMock()
    plugin.config = MagicMock()
    plugin._queue_lock = asyncio.Lock()
    plugin._session = None
    plugin._session_lock = asyncio.Lock()
    plugin.get_party_player = AsyncMock(return_value="party_queue")  # type: ignore[method-assign]
    config_values = {
        CONF_ENABLE_GUEST_ACCESS: True,
        CONF_ENABLE_BOOST: True,
        CONF_ENABLE_ADD_QUEUE: True,
        CONF_PREVENT_DUPLICATE_TRACKS: True,
    }
    plugin.config.get_value.side_effect = config_values.__getitem__
    return plugin


@pytest.mark.asyncio
async def test_add_to_queue_rechecks_duplicates_during_priority_insert() -> None:
    """Reject a duplicate that appears after the initial queue lookup."""
    plugin = _create_party_plugin()
    player_queues = cast("MagicMock", plugin.mass.player_queues)
    music = cast("MagicMock", plugin.mass.music)
    uri = "spotify://track/123"

    queue = MagicMock()
    queue.state = PlaybackState.PLAYING
    queue.current_index = 0
    queue.index_in_buffer = 0
    player_queues.get.return_value = queue
    player_queues.items.return_value = []
    player_queues.load = AsyncMock()

    async def mutate_queue_during_resolve(_uri: str) -> MagicMock:
        media_item = MagicMock()
        media_item.media_type = MediaType.TRACK
        player_queues.items.return_value = [MagicMock(uri=uri, extra_attributes={})]
        return media_item

    music.get_item_by_uri = AsyncMock(side_effect=mutate_queue_during_resolve)
    queue_item = MagicMock()
    queue_item.extra_attributes = {}

    with (
        patch("music_assistant.providers.party.build_queue_item", return_value=queue_item),
        pytest.raises(InvalidDataError, match="already in the queue"),
    ):
        await plugin.add_to_queue(uri)

    player_queues.load.assert_not_awaited()


def _create_session_test_plugin(mode: str) -> PartyPlugin:
    """Create a party plugin with a real get_party_player for session tests."""
    plugin = PartyPlugin.__new__(PartyPlugin)
    plugin.mass = MagicMock()
    plugin.logger = MagicMock()
    plugin.config = MagicMock()
    plugin._session = None
    plugin._session_lock = asyncio.Lock()
    config_values = {
        CONF_ENABLE_GUEST_ACCESS: True,
        CONF_PARTY_MODE: mode,
    }
    plugin.config.get_value.side_effect = config_values.__getitem__
    return plugin


@pytest.mark.asyncio
async def test_get_party_player_remote_mode_returns_session_queue() -> None:
    """In remote mode the party player is the session's virtual player queue."""
    plugin = _create_session_test_plugin(SharedPlaybackMode.REMOTE.value)
    session = MagicMock()
    session.queue_id = "sendspin_virtual_party"
    plugin._get_session = AsyncMock(return_value=session)  # type: ignore[method-assign]

    assert await plugin.get_party_player() == "sendspin_virtual_party"


@pytest.mark.asyncio
async def test_get_party_player_remote_mode_no_session() -> None:
    """In remote mode without a session (sendspin missing) no queue is returned."""
    plugin = _create_session_test_plugin(SharedPlaybackMode.REMOTE.value)
    plugin._get_session = AsyncMock(return_value=None)  # type: ignore[method-assign]

    assert await plugin.get_party_player() is None


@pytest.mark.asyncio
async def test_listen_in_without_session_raises() -> None:
    """Listen-in is rejected when no session is available (venue auto mode)."""
    plugin = _create_session_test_plugin(SharedPlaybackMode.VENUE.value)
    plugin._get_or_create_session_locked = AsyncMock(return_value=None)  # type: ignore[method-assign]

    with (
        pytest.raises(InvalidDataError, match="not available"),
    ):
        await plugin.listen_in("web_player_1")


@pytest.mark.asyncio
async def test_listen_in_attaches_guest_player() -> None:
    """Listen-in attaches the guest's web player to the session."""
    plugin = _create_session_test_plugin(SharedPlaybackMode.REMOTE.value)
    session = MagicMock()
    session.queue_id = "sendspin_virtual_party"

    async def _assert_locked(_web_player_id: str) -> None:
        assert plugin._session_lock.locked()

    session.add_guest_listener = AsyncMock(side_effect=_assert_locked)
    plugin._get_or_create_session_locked = AsyncMock(return_value=session)  # type: ignore[method-assign]

    result = await plugin.listen_in("web_player_1")

    assert result == {"success": True, "queue_id": "sendspin_virtual_party"}
    session.add_guest_listener.assert_awaited_once_with("web_player_1")


@pytest.mark.parametrize("mode", [SharedPlaybackMode.VENUE.value, SharedPlaybackMode.REMOTE.value])
@pytest.mark.asyncio
async def test_get_party_config_exposes_mode(mode: str) -> None:
    """get_party_config surfaces the configured playback mode to the guest frontend."""
    plugin = _create_party_plugin()
    cast("MagicMock", plugin.config.get_value).side_effect = {CONF_PARTY_MODE: mode}.get

    config = await plugin.get_party_config()

    assert config.mode == mode


@pytest.mark.asyncio
async def test_guest_readable_commands_use_guest_scope() -> None:
    """party/url and party/config stay on a guest-readable scope, never a host-only one."""
    plugin = _create_party_plugin()
    plugin._unregister_handles = []

    await plugin.loaded_in_mass()

    scopes = {
        call.args[0]: call.kwargs["required_scope"]
        for call in cast("MagicMock", plugin.mass.register_api_command).call_args_list
    }
    assert scopes["party/url"] == Scope.PROVIDERS_READ
    assert scopes["party/config"] == Scope.PROVIDERS_READ
    assert scopes["party/listen_in"] == Scope.PLAYERS_CONTROL
    assert scopes["party/stop_listen_in"] == Scope.PLAYERS_CONTROL
    assert scopes["party/can_listen_in"] == Scope.PLAYERS_CONTROL
