"""
Tests that queue control exempts the caller's own private client player from the player filter.

A user restricted to a set of players may still control the private client player (browser
session, desktop or mobile app) they connected on, which registers itself and is not in their
filter. The exemption only applies to that private player, so a restricted user cannot reach a
shared speaker by announcing its id as their client id.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from music_assistant_models.auth import User, UserRole
from music_assistant_models.errors import InsufficientPermissions

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.webserver.helpers.auth_middleware import (
    current_user,
    sendspin_player_id,
)

ALLOWED_PLAYER = "kitchen"
OTHER_PLAYER = "living_room"
OWN_CLIENT = "browser_session_1"


def _player(player_id: str, *, private: bool) -> SimpleNamespace:
    """Build a stand-in player carrying just what the permission check reads."""
    return SimpleNamespace(player_id=player_id, private=private)


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


def _check(queue_id: str, *players: SimpleNamespace) -> None:
    """Run the queue permission check for the given player against a registry of players."""
    registry = {player.player_id: player for player in players}
    controller = PlayerQueuesController.__new__(PlayerQueuesController)
    controller.mass = SimpleNamespace(  # type: ignore[assignment]
        players=SimpleNamespace(get_player=registry.get)
    )
    controller._check_player_permission(queue_id)


def test_control_of_the_own_client_player_is_permitted() -> None:
    """The private client player the user connected on is controllable even when filtered out."""
    with _restricted_user([ALLOWED_PLAYER], own_player=OWN_CLIENT):
        _check(OWN_CLIENT, _player(OWN_CLIENT, private=True))


def test_a_shared_speaker_claimed_as_the_client_player_is_refused() -> None:
    """Announcing a shared speaker's id as the client id does not grant access to it."""
    # the bound client id is not proof of ownership, so a non-private player claimed this way
    # must still be refused
    with (
        _restricted_user([ALLOWED_PLAYER], own_player=OTHER_PLAYER),
        pytest.raises(InsufficientPermissions),
    ):
        _check(OTHER_PLAYER, _player(OTHER_PLAYER, private=False))
