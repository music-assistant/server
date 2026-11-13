"""Tests that the party listen-in commands follow the calling user's player filter."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.errors import InsufficientPermissions

from music_assistant.helpers.shared_playback import SharedPlaybackSession
from music_assistant.providers.party import (
    CONF_ENABLE_GUEST_ACCESS,
    CONF_PARTY_MODE,
    PartyPlugin,
)
from tests.common import as_user, grouping_player_controller


@pytest.fixture
async def plugin() -> PartyPlugin:
    """
    Build a party plugin around venue player "v", with private client player "w".

    The players "a" and "b" are regular players that can group with the venue.
    """
    controller = grouping_player_controller("v", "a", "b", "w")
    web_player = controller._players["w"]
    web_player._attr_private = True
    web_player.update_state(force_update=True, signal_event=False)
    plugin = PartyPlugin.__new__(PartyPlugin)
    plugin.mass = controller.mass
    plugin.logger = MagicMock()
    plugin.config = MagicMock()
    plugin.config.get_value.side_effect = {
        CONF_ENABLE_GUEST_ACCESS: True,
        CONF_PARTY_MODE: "venue",
    }.__getitem__
    plugin._unregister_handles = []
    plugin._session = await SharedPlaybackSession.create_venue(plugin.mass, "v")
    plugin._session_lock = asyncio.Lock()
    plugin._get_or_create_session_locked = AsyncMock(  # type: ignore[method-assign]
        return_value=plugin._session
    )
    await plugin.loaded_in_mass()
    return plugin


def _api(plugin: PartyPlugin, command: str) -> Callable[..., Awaitable[Any]]:
    """Return the handler the server runs for the given API command."""
    return cast("MagicMock", plugin.mass).command_handlers[command].target  # type: ignore[no-any-return]


def _set_members(plugin: PartyPlugin) -> AsyncMock:
    """Return the stubbed member change of the players controller."""
    return cast("AsyncMock", plugin.mass.players._handle_set_members)


async def test_listen_in_with_another_player_is_refused(plugin: PartyPlugin) -> None:
    """A guest can not join a player outside their players to the party audio."""
    with as_user(["a"], client_player_id="w"), pytest.raises(InsufficientPermissions):
        await _api(plugin, "party/listen_in")("b")

    cast("AsyncMock", plugin._get_or_create_session_locked).assert_not_awaited()
    _set_members(plugin).assert_not_awaited()
    assert plugin._session is not None
    assert not plugin._session._guest_listeners


async def test_listen_in_with_own_client_player_is_permitted(plugin: PartyPlugin) -> None:
    """A guest may join the client player they are connected on to the party audio."""
    with as_user(["a"], client_player_id="w"):
        await _api(plugin, "party/listen_in")("w")

    players = plugin.mass.players
    _set_members(plugin).assert_awaited_once_with(players._players["v"], ["w"], None)


async def test_stop_listen_in_with_another_player_is_refused(plugin: PartyPlugin) -> None:
    """A guest can not take a player outside their players out of the party audio."""
    with as_user(["a"], client_player_id="w"), pytest.raises(InsufficientPermissions):
        await _api(plugin, "party/stop_listen_in")("b")

    _set_members(plugin).assert_not_awaited()


async def test_stop_listen_in_with_permitted_player_detaches_it(plugin: PartyPlugin) -> None:
    """A guest may take a player within their players out of the party audio."""
    players = plugin.mass.players
    players._players["v"]._attr_group_members = ["v", "a"]
    for player in players._players.values():
        player.update_state(signal_event=False)

    with as_user(["a"], client_player_id="w"):
        await _api(plugin, "party/stop_listen_in")("a")

    _set_members(plugin).assert_awaited_once_with(players._players["v"], None, ["a"])
