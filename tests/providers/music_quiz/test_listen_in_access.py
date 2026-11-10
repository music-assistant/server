"""Tests that the Music Quiz listen-in commands follow the calling user's player filter."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.errors import InsufficientPermissions

from music_assistant.helpers.shared_playback import SharedPlaybackSession
from music_assistant.providers.music_quiz import MusicQuizPlugin
from music_assistant.providers.music_quiz.models import (
    MusicQuizAnswerType,
    MusicQuizConfig,
    MusicQuizGame,
)
from tests.common import as_user, grouping_player_controller


@pytest.fixture
async def plugin() -> MusicQuizPlugin:
    """
    Build a Music Quiz plugin with a game around venue player "v", and client player "w".

    The players "a" and "b" are regular players that can group with the venue.
    """
    controller = grouping_player_controller("v", "a", "b", "w")
    web_player = controller._players["w"]
    web_player._attr_private = True
    web_player.update_state(force_update=True, signal_event=False)
    plugin = MusicQuizPlugin.__new__(MusicQuizPlugin)
    plugin.mass = controller.mass
    plugin.logger = MagicMock()
    plugin._unregister_handles = []
    plugin._game = MusicQuizGame(
        config=MusicQuizConfig(venue_player_id="v"),
        quiz_type="guess_the_song",
        answer_type=MusicQuizAnswerType.MULTIPLE_CHOICE,
    )
    plugin._playback_lock = asyncio.Lock()
    plugin._playback_session = await SharedPlaybackSession.create_venue(plugin.mass, "v")
    plugin._get_or_create_session_locked = AsyncMock(  # type: ignore[method-assign]
        return_value=plugin._playback_session
    )
    plugin._migrate_legacy_playback_preference = AsyncMock()  # type: ignore[method-assign]
    await plugin.loaded_in_mass()
    return plugin


def _api(plugin: MusicQuizPlugin, command: str) -> Callable[..., Awaitable[Any]]:
    """Return the handler the server runs for the given API command."""
    return cast("MagicMock", plugin.mass).command_handlers[command].target  # type: ignore[no-any-return]


def _set_members(plugin: MusicQuizPlugin) -> AsyncMock:
    """Return the stubbed member change of the players controller."""
    return cast("AsyncMock", plugin.mass.players._handle_set_members)


async def test_listen_in_with_another_player_is_refused(plugin: MusicQuizPlugin) -> None:
    """A guest can not join a player outside their players to the game audio."""
    with as_user(["a"], client_player_id="w"), pytest.raises(InsufficientPermissions):
        await _api(plugin, "music_quiz/listen_in")("b")

    cast("AsyncMock", plugin._get_or_create_session_locked).assert_not_awaited()
    _set_members(plugin).assert_not_awaited()
    assert plugin._playback_session is not None
    assert not plugin._playback_session._guest_listeners


async def test_listen_in_with_own_client_player_is_permitted(plugin: MusicQuizPlugin) -> None:
    """A guest may join the client player they are connected on to the game audio."""
    with as_user(["a"], client_player_id="w"):
        await _api(plugin, "music_quiz/listen_in")("w")

    players = plugin.mass.players
    _set_members(plugin).assert_awaited_once_with(players._players["v"], ["w"], None)


async def test_stop_listen_in_with_another_player_is_refused(plugin: MusicQuizPlugin) -> None:
    """A guest can not take a player outside their players out of the game audio."""
    with as_user(["a"], client_player_id="w"), pytest.raises(InsufficientPermissions):
        await _api(plugin, "music_quiz/stop_listen_in")("b")

    _set_members(plugin).assert_not_awaited()


async def test_stop_listen_in_with_permitted_player_detaches_it(plugin: MusicQuizPlugin) -> None:
    """A guest may take a player within their players out of the game audio."""
    players = plugin.mass.players
    players._players["v"]._attr_group_members = ["v", "a"]
    for player in players._players.values():
        player.update_state(signal_event=False)

    with as_user(["a"], client_player_id="w"):
        await _api(plugin, "music_quiz/stop_listen_in")("a")

    _set_members(plugin).assert_awaited_once_with(players._players["v"], None, ["a"])
