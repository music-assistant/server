"""Tests that the sleep timer commands follow the calling user's player filter."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from music_assistant_models.auth import UserRole
from music_assistant_models.enums import SleepTimerMode
from music_assistant_models.errors import InsufficientPermissions

from music_assistant.controllers.players import PlayerController
from tests.common import as_user, grouping_player_controller

type Command = Callable[[PlayerController, str], Any]

COMMANDS: dict[str, Command] = {
    "get": lambda c, player_id: _api(c, "players/sleep_timer/get")(player_id),
    "set": lambda c, player_id: _api(c, "players/sleep_timer/set")(player_id, 60),
    "set_end_of_item": lambda c, player_id: _api(c, "players/sleep_timer/set")(
        player_id, mode=SleepTimerMode.END_OF_ITEM
    ),
    "clear": lambda c, player_id: _api(c, "players/sleep_timer/clear")(player_id),
}


@pytest.fixture
def controller() -> PlayerController:
    """Build a players controller over players "a" and "b" and a private client player "w"."""
    controller = grouping_player_controller("a", "b", "w")
    controller._players["w"]._attr_private = True
    return controller


@pytest.mark.parametrize("command", COMMANDS.values(), ids=COMMANDS.keys())
def test_a_timer_on_another_player_is_refused(
    controller: PlayerController, command: Command
) -> None:
    """A user limited to another player can not read, set or clear the timer of the player."""
    controller.set_sleep_timer("b", 30)
    expires_at = controller._players["b"].sleep_timer_expires_at
    mass = cast("MagicMock", controller.mass)
    mass.reset_mock()
    controller._get_player_with_redirect = MagicMock()  # type: ignore[method-assign]

    with as_user(["a"]), pytest.raises(InsufficientPermissions):
        command(controller, "b")

    controller._get_player_with_redirect.assert_not_called()
    assert controller._players["b"].sleep_timer_expires_at == expires_at
    mass.call_later.assert_not_called()
    mass.cancel_timer.assert_not_called()
    mass.player_queues.set_sleep_timer_target.assert_not_called()
    mass.player_queues.drop_sleep_timer_target.assert_not_called()


@pytest.mark.parametrize(
    "user",
    [
        pytest.param(lambda: as_user(["b"]), id="limited_to_the_player"),
        pytest.param(lambda: as_user([]), id="unlimited"),
        pytest.param(lambda: as_user(["a"], role=UserRole.ADMIN), id="admin"),
        pytest.param(nullcontext, id="no_user"),
    ],
)
def test_a_user_who_may_use_the_player_sets_and_clears_its_timer(
    controller: PlayerController, user: Callable[[], AbstractContextManager[object]]
) -> None:
    """A user who may use the player, or a call without a user, reads, sets and clears its timer."""
    player = controller._players["b"]
    with user():
        expires_at = _api(controller, "players/sleep_timer/set")("b", 60)
        assert _api(controller, "players/sleep_timer/get")("b") == expires_at
        assert player.sleep_timer_expires_at == expires_at
        _api(controller, "players/sleep_timer/clear")("b")

    assert player.sleep_timer_expires_at is None


def test_the_users_own_client_player_is_permitted(controller: PlayerController) -> None:
    """The private client player the user connected on passes, although it is not in the filter."""
    player = controller._players["w"]
    with as_user(["a"], client_player_id="w"):
        expires_at = _api(controller, "players/sleep_timer/set")("w", 60)
        assert _api(controller, "players/sleep_timer/get")("w") == expires_at
        assert player.sleep_timer_expires_at == expires_at
        _api(controller, "players/sleep_timer/clear")("w")

    assert player.sleep_timer_expires_at is None


def _api(controller: PlayerController, command: str) -> Callable[..., Any]:
    """Return the handler the server runs for the given API command."""
    return cast("MagicMock", controller.mass).command_handlers[command].target  # type: ignore[no-any-return]
