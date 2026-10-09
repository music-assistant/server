"""
Tests that the queue commands follow the calling user's player filter.

A user restricted to a set of players may still control the private client player (browser
session, desktop or mobile app) they connected on, which registers itself and is not in their
filter. The exemption only applies to that private player, so a restricted user cannot reach a
shared speaker by announcing its id as their client id.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from music_assistant_models.auth import User, UserRole
from music_assistant_models.enums import RepeatMode
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


def _controller(*players: SimpleNamespace) -> PlayerQueuesController:
    """Build a bare queue controller over a registry of players, without any queue."""
    registry = {player.player_id: player for player in players}
    controller = PlayerQueuesController.__new__(PlayerQueuesController)
    controller.mass = SimpleNamespace(  # type: ignore[assignment]
        players=SimpleNamespace(get_player=registry.get)
    )
    controller._queue_data = {}
    return controller


async def _run(
    command: Callable[[PlayerQueuesController], object], *players: SimpleNamespace
) -> None:
    """Run the command, sync or async, on a bare controller over the given players."""
    result = command(_controller(*players))
    if inspect.isawaitable(result):
        await result


def _check(queue_id: str, *players: SimpleNamespace) -> None:
    """Run the queue permission check for the given player against a registry of players."""
    _controller(*players)._check_player_permission(queue_id)


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


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(lambda queues: queues.get_queue(OTHER_PLAYER), id="get"),
        pytest.param(lambda queues: queues.get_queue_items(OTHER_PLAYER), id="items"),
        pytest.param(
            lambda queues: queues.get_player_active_queue(OTHER_PLAYER), id="get_active_queue"
        ),
        pytest.param(lambda queues: queues.set_shuffle(OTHER_PLAYER, True), id="shuffle"),
        pytest.param(lambda queues: queues.set_autoplay(OTHER_PLAYER, True), id="autoplay"),
        pytest.param(lambda queues: queues.set_repeat(OTHER_PLAYER, RepeatMode.ALL), id="repeat"),
        pytest.param(lambda queues: queues.set_crossfade(OTHER_PLAYER, True), id="crossfade"),
        pytest.param(lambda queues: queues.set_overlay(OTHER_PLAYER, enabled=True), id="overlay"),
        pytest.param(
            lambda queues: queues.set_playback_speed(OTHER_PLAYER, 1.5), id="set_playback_speed"
        ),
        pytest.param(lambda queues: queues.move_item(OTHER_PLAYER, "item"), id="move_item"),
        pytest.param(lambda queues: queues.move_item_end(OTHER_PLAYER, "item"), id="move_item_end"),
        pytest.param(lambda queues: queues.delete_item(OTHER_PLAYER, "item"), id="delete_item"),
        pytest.param(lambda queues: queues.clear(OTHER_PLAYER), id="clear"),
        pytest.param(lambda queues: queues.skip(OTHER_PLAYER), id="skip"),
        pytest.param(lambda queues: queues.seek(OTHER_PLAYER, 5), id="seek"),
        pytest.param(
            lambda queues: queues.save_as_playlist(OTHER_PLAYER, "Saved"), id="save_as_playlist"
        ),
        pytest.param(
            lambda queues: queues.transfer_queue(OTHER_PLAYER, ALLOWED_PLAYER), id="transfer_from"
        ),
        pytest.param(
            lambda queues: queues.transfer_queue(ALLOWED_PLAYER, OTHER_PLAYER), id="transfer_to"
        ),
    ],
)
async def test_reading_and_changing_a_queue_needs_access_to_its_player(
    command: Callable[[PlayerQueuesController], object],
) -> None:
    """A queue of a player outside the user's filter is neither read nor changed."""
    with _restricted_user([ALLOWED_PLAYER]), pytest.raises(InsufficientPermissions):
        await _run(command, _player(OTHER_PLAYER, private=False))


def test_the_queue_listing_holds_only_the_users_players() -> None:
    """The listing leaves out the queues of players outside the user's filter."""
    controller = _controller(
        _player(ALLOWED_PLAYER, private=False), _player(OTHER_PLAYER, private=False)
    )
    controller._queue_data = {
        player_id: SimpleNamespace(queue=SimpleNamespace(queue_id=player_id))  # type: ignore[misc]
        for player_id in (ALLOWED_PLAYER, OTHER_PLAYER)
    }
    with _restricted_user([ALLOWED_PLAYER]):
        assert [queue.queue_id for queue in controller.all_queues()] == [ALLOWED_PLAYER]
    # an internal caller has no user and sees every queue
    assert len(controller.all_queues()) == 2
