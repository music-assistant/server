"""
Tests that the queue commands honor the player filter of the calling user.

A user restricted to a set of players may only read and control the queues of those players.
They may still control the private client player (browser session, desktop or mobile app) they
connected on, which registers itself and is not in their filter. The exemption only applies to
that private player, so a restricted user cannot reach a shared speaker by announcing its id as
their client id.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from inspect import isawaitable
from types import SimpleNamespace
from typing import Any

import pytest
from music_assistant_models.auth import User, UserRole
from music_assistant_models.enums import PlaybackState, RepeatMode
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


def _controller(*queue_ids: str) -> PlayerQueuesController:
    """Build a queue controller holding a queue (with one item) for each of the given ids."""
    controller = PlayerQueuesController.__new__(PlayerQueuesController)
    controller.mass = SimpleNamespace(  # type: ignore[assignment]
        players=SimpleNamespace(
            get_player=lambda _player_id: None,
            get_group_and_player_lock=lambda _player_id: nullcontext(),
        )
    )
    queue_data: dict[str, Any] = {
        queue_id: SimpleNamespace(
            queue=SimpleNamespace(queue_id=queue_id, state=PlaybackState.IDLE, extra_attributes={}),
            items=["item"],
            play_action_refcount=0,
        )
        for queue_id in queue_ids
    }
    controller._queue_data = queue_data
    controller.signal_update = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    return controller


async def _run(
    command: Callable[[PlayerQueuesController, str], Any],
    controller: PlayerQueuesController,
    queue_id: str,
) -> None:
    """Run a queue command, awaiting it when it is a coroutine."""
    result = command(controller, queue_id)
    if isawaitable(result):
        await result


@contextmanager
def _user(role: UserRole, player_filter: list[str]) -> Iterator[None]:
    """Run the block as a user with the given role and player filter."""
    token = current_user.set(
        User(user_id="user_2", username="someone", role=role, player_filter=player_filter)
    )
    try:
        yield
    finally:
        current_user.reset(token)


QUEUE_COMMANDS: dict[str, Callable[[PlayerQueuesController, str], Any]] = {
    "get": lambda controller, queue_id: controller.get_for_api(queue_id),
    "items": lambda controller, queue_id: controller.items_for_api(queue_id),
    "get_active_queue": lambda controller, queue_id: controller.get_active_queue_for_api(queue_id),
    "shuffle": lambda controller, queue_id: controller.set_shuffle(queue_id, True),
    "autoplay": lambda controller, queue_id: controller.set_autoplay(queue_id, True),
    "repeat": lambda controller, queue_id: controller.set_repeat(queue_id, RepeatMode.ALL),
    "crossfade": lambda controller, queue_id: controller.set_crossfade(queue_id, True),
    "overlay": lambda controller, queue_id: controller.set_overlay(queue_id, enabled=True),
    "set_playback_speed": lambda controller, queue_id: controller.set_playback_speed(queue_id, 1.5),
    "move_item": lambda controller, queue_id: controller.move_item(queue_id, "item"),
    "move_item_end": lambda controller, queue_id: controller.move_item_end(queue_id, "item"),
    "delete_item": lambda controller, queue_id: controller.delete_item(queue_id, 0),
    "clear": lambda controller, queue_id: controller.clear(queue_id),
    "save_as_playlist": lambda controller, queue_id: controller.save_as_playlist(queue_id, "x"),
    "play_pause": lambda controller, queue_id: controller.play_pause(queue_id),
    "skip": lambda controller, queue_id: controller.skip(queue_id),
    "seek": lambda controller, queue_id: controller.seek(queue_id, 10),
    "transfer_from": lambda controller, queue_id: controller.transfer_queue(
        queue_id, ALLOWED_PLAYER
    ),
    "transfer_to": lambda controller, queue_id: controller.transfer_queue(ALLOWED_PLAYER, queue_id),
}


@pytest.mark.parametrize("command", QUEUE_COMMANDS)
async def test_a_queue_outside_the_filter_is_refused(command: str) -> None:
    """Every queue command refuses a queue outside the player filter of the user."""
    controller = _controller(ALLOWED_PLAYER, OTHER_PLAYER)
    with _restricted_user([ALLOWED_PLAYER]), pytest.raises(InsufficientPermissions):
        await _run(QUEUE_COMMANDS[command], controller, OTHER_PLAYER)


@pytest.mark.parametrize(
    ("role", "player_filter", "expected"),
    [
        (UserRole.USER, [ALLOWED_PLAYER], [ALLOWED_PLAYER]),
        (UserRole.USER, [], [ALLOWED_PLAYER, OTHER_PLAYER]),
        (UserRole.ADMIN, [ALLOWED_PLAYER], [ALLOWED_PLAYER, OTHER_PLAYER]),
    ],
)
def test_all_queues_lists_only_the_queues_inside_the_filter(
    role: UserRole, player_filter: list[str], expected: list[str]
) -> None:
    """Listing the queues leaves out those outside the filter of a restricted user."""
    controller = _controller(ALLOWED_PLAYER, OTHER_PLAYER)
    with _user(role, player_filter):
        assert [queue.queue_id for queue in controller.all_for_api()] == expected


def test_an_internal_call_without_a_user_is_permitted() -> None:
    """A call without a user in context (a server-side caller) is not restricted."""
    controller = _controller(ALLOWED_PLAYER, OTHER_PLAYER)
    assert len(controller.items_for_api(OTHER_PLAYER)) == 1
