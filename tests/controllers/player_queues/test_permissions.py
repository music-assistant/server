"""Tests for PlayerQueue read permissions."""

from __future__ import annotations

from collections.abc import Generator
from typing import cast
from unittest.mock import MagicMock

import pytest
from music_assistant_models.auth import User, UserRole
from music_assistant_models.errors import InsufficientPermissions
from music_assistant_models.player_queue import PlayerQueue
from music_assistant_models.queue_item import QueueItem

from music_assistant.constants import GUEST_ACCESS_RESTRICTED_PLAYER_ID
from music_assistant.controllers.player_queues.controller import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData
from music_assistant.controllers.webserver.helpers.auth_middleware import (
    set_current_user,
    set_impersonated_user,
    set_sendspin_player_id,
)


@pytest.fixture(autouse=True)
def reset_auth_context() -> Generator[None]:
    """Reset authentication context around each test."""
    set_current_user(None)
    set_impersonated_user(None)
    set_sendspin_player_id(None)
    yield
    set_current_user(None)
    set_impersonated_user(None)
    set_sendspin_player_id(None)


def _create_controller(*queue_ids: str) -> PlayerQueuesController:
    """Create a minimally initialized queue controller."""
    controller = PlayerQueuesController.__new__(PlayerQueuesController)
    controller.mass = MagicMock()
    controller._queue_data = {
        queue_id: PlayerQueueData(
            queue=PlayerQueue(
                queue_id=queue_id,
                active=False,
                display_name=queue_id,
                available=True,
                items=1,
            ),
            items=[
                QueueItem(
                    queue_id=queue_id,
                    queue_item_id=f"{queue_id}_item",
                    name=f"{queue_id} item",
                    duration=None,
                )
            ],
        )
        for queue_id in queue_ids
    }
    return controller


def _set_user(role: UserRole, player_filter: list[str]) -> None:
    """Set the authenticated user for a permission test."""
    set_current_user(
        User(
            user_id=f"{role.value}_id",
            username=f"{role.value}_user",
            role=role,
            player_filter=player_filter,
        )
    )


def test_restricted_guest_only_sees_explicit_queues() -> None:
    """A restricted guest only sees explicitly allowed queues."""
    controller = _create_controller("host", "allowed", "web_player")
    players = cast("MagicMock", controller.mass.players)
    players.is_protocol_player.return_value = False
    _set_user(
        UserRole.GUEST,
        [GUEST_ACCESS_RESTRICTED_PLAYER_ID, "allowed"],
    )
    set_sendspin_player_id("web_player")

    queues = controller.all_for_api()

    assert {queue.queue_id for queue in queues} == {"allowed"}
    assert controller.get_for_api("allowed") is controller.get("allowed")
    assert controller.items_for_api("allowed") == controller.items("allowed")


def test_direct_queue_reads_reject_hidden_queue() -> None:
    """Direct external queue reads do not reveal a hidden queue or its items."""
    controller = _create_controller("host")
    _set_user(UserRole.GUEST, [GUEST_ACCESS_RESTRICTED_PLAYER_ID])

    with pytest.raises(InsufficientPermissions):
        controller.get_for_api("host")
    with pytest.raises(InsufficientPermissions):
        controller.items_for_api("host")


def test_restricted_guest_cannot_read_standalone_sendspin_queue() -> None:
    """A restricted guest cannot read its standalone Sendspin queue."""
    controller = _create_controller("web_player")
    players = cast("MagicMock", controller.mass.players)
    players.is_protocol_player.return_value = False
    _set_user(UserRole.GUEST, [GUEST_ACCESS_RESTRICTED_PLAYER_ID])
    set_sendspin_player_id("web_player")

    with pytest.raises(InsufficientPermissions):
        controller.get_for_api("web_player")
    with pytest.raises(InsufficientPermissions):
        controller.items_for_api("web_player")


def test_restricted_guest_can_read_protocol_sendspin_queue() -> None:
    """A restricted guest may read its Sendspin queue after it becomes a protocol child."""
    controller = _create_controller("web_player")
    players = cast("MagicMock", controller.mass.players)
    players.is_protocol_player.return_value = True
    _set_user(UserRole.GUEST, [GUEST_ACCESS_RESTRICTED_PLAYER_ID])
    set_sendspin_player_id("web_player")

    assert controller.get_for_api("web_player") is controller.get("web_player")
    assert controller.items_for_api("web_player") == controller.items("web_player")


def test_restricted_guest_cannot_claim_host_queue() -> None:
    """A restricted guest cannot claim a host queue through Sendspin."""
    controller = _create_controller("host")
    players = cast("MagicMock", controller.mass.players)
    players.is_protocol_player.return_value = False
    _set_user(UserRole.GUEST, [GUEST_ACCESS_RESTRICTED_PLAYER_ID])
    set_sendspin_player_id("host")

    assert not controller.all_for_api()
    with pytest.raises(InsufficientPermissions):
        controller.get_for_api("host")
    with pytest.raises(InsufficientPermissions):
        controller.items_for_api("host")
    with pytest.raises(InsufficientPermissions):
        controller.get_active_queue_for_api("host")


def test_filtered_user_can_read_own_sendspin_queue() -> None:
    """An ordinary filtered user may read their own Sendspin queue."""
    controller = _create_controller("host", "allowed", "web_player")
    _set_user(UserRole.USER, ["allowed"])
    set_sendspin_player_id("web_player")

    assert {queue.queue_id for queue in controller.all_for_api()} == {"allowed", "web_player"}
    assert controller.get_for_api("web_player") is controller.get("web_player")
    assert controller.items_for_api("web_player") == controller.items("web_player")


def test_filtered_user_cannot_pivot_from_own_player_to_hidden_parent() -> None:
    """An own web player cannot expose an unauthorized active parent queue."""
    controller = _create_controller("host", "web_player")
    web_player = MagicMock()
    players = cast("MagicMock", controller.mass.players)
    players.get_player.return_value = web_player
    players.get_active_queue.return_value = controller.get("host")
    _set_user(UserRole.USER, ["allowed"])
    set_sendspin_player_id("web_player")

    with pytest.raises(InsufficientPermissions):
        controller.get_active_queue_for_api("web_player")
    players.get_active_queue.assert_called_once_with(web_player)


def test_active_queue_allows_explicit_parent() -> None:
    """A restricted user may resolve an explicitly allowed parent queue."""
    controller = _create_controller("allowed")
    allowed_player = MagicMock()
    players = cast("MagicMock", controller.mass.players)
    players.get_player.return_value = allowed_player
    allowed_queue = controller.get("allowed")
    players.get_active_queue.return_value = allowed_queue
    _set_user(
        UserRole.GUEST,
        [GUEST_ACCESS_RESTRICTED_PLAYER_ID, "allowed"],
    )

    assert controller.get_active_queue_for_api("allowed") is allowed_queue


@pytest.mark.parametrize(
    ("role", "player_filter"),
    [
        (UserRole.USER, []),
        (UserRole.ADMIN, [GUEST_ACCESS_RESTRICTED_PLAYER_ID]),
    ],
)
def test_unrestricted_users_keep_existing_access(
    role: UserRole,
    player_filter: list[str],
) -> None:
    """Empty-filter users and admins retain unrestricted queue reads."""
    controller = _create_controller("host")
    _set_user(role, player_filter)

    assert controller.all_for_api() == controller.all()
    assert controller.get_for_api("host") is controller.get("host")
    assert controller.items_for_api("host") == controller.items("host")


def test_raw_queue_methods_remain_available_to_internal_callers() -> None:
    """Trusted server code keeps raw queue access under a guest request context."""
    controller = _create_controller("host")
    _set_user(UserRole.GUEST, [GUEST_ACCESS_RESTRICTED_PLAYER_ID])

    assert controller.all()
    assert controller.get("host") is not None
    assert controller.items("host")


def test_missing_user_keeps_existing_access() -> None:
    """Queue reads remain unrestricted outside an authenticated user context."""
    controller = _create_controller("host")

    assert controller.all_for_api() == controller.all()
    assert controller.get_for_api("host") is controller.get("host")
    assert controller.items_for_api("host") == controller.items("host")
