"""
Tests that queue control honours the player filter while exempting the caller's own client player.

A user restricted to a set of players may still control the client player they connected on
(browser, desktop or mobile app), which registers itself and is not in their stored filter.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from music_assistant_models.auth import User, UserRole
from music_assistant_models.errors import InsufficientPermissions

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.webserver.helpers.auth_middleware import (
    current_user,
    sendspin_player_id,
)

OWN_PLAYER = "browser_session_1"
ALLOWED_PLAYER = "kitchen"
OTHER_PLAYER = "living_room"


@contextmanager
def _restricted_user(allowed_players: list[str], own_player: str | None = None) -> Iterator[None]:
    """Run the block as a restricted user, optionally connected on ``own_player``."""
    user_token = current_user.set(
        User(
            user_id="user_1",
            username="restricted",
            role=UserRole.USER,
            player_filter=allowed_players,
        )
    )
    player_token = sendspin_player_id.set(own_player)
    try:
        yield
    finally:
        sendspin_player_id.reset(player_token)
        current_user.reset(user_token)


def _check(queue_id: str) -> None:
    """Run the queue permission check for the given player in the current context."""
    controller = PlayerQueuesController.__new__(PlayerQueuesController)
    controller._check_player_permission(queue_id)


def test_control_of_a_player_outside_the_filter_is_refused() -> None:
    """A restricted user may not control a player they are not allowed to use."""
    with _restricted_user([ALLOWED_PLAYER]), pytest.raises(InsufficientPermissions):
        _check(OTHER_PLAYER)


def test_control_of_an_allowed_player_is_permitted() -> None:
    """A player inside the filter stays controllable."""
    with _restricted_user([ALLOWED_PLAYER]):
        _check(ALLOWED_PLAYER)


def test_control_of_the_own_client_player_is_permitted() -> None:
    """The client player the user connected on is controllable even when not in the filter."""
    with _restricted_user([ALLOWED_PLAYER], own_player=OWN_PLAYER):
        _check(OWN_PLAYER)


def test_the_client_exemption_does_not_extend_to_other_players() -> None:
    """Connecting a client player grants no access to any other player outside the filter."""
    with (
        _restricted_user([ALLOWED_PLAYER], own_player=OWN_PLAYER),
        pytest.raises(InsufficientPermissions),
    ):
        _check(OTHER_PLAYER)


def test_an_unrestricted_user_may_control_any_player() -> None:
    """A user without a filter is unaffected by the check."""
    with _restricted_user([]):
        _check(OTHER_PLAYER)
