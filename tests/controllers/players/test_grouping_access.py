"""
Tests that the grouping commands follow the calling user's player filter.

A grouping command needs access to every player whose grouping it changes: adding needs the
target player and every player added, removing needs the target player or the player removed,
and a target player that is synced to another player is detached first, so it needs access too.
A command on a member of a group player changes that group, so the group counts as a target.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.auth import User, UserRole
from music_assistant_models.enums import PlayerFeature, PlayerType
from music_assistant_models.errors import InsufficientPermissions

from music_assistant.controllers.players import PlayerController
from music_assistant.controllers.webserver.helpers.auth_middleware import current_user
from tests.common import MockPlayer, MockProvider

type Command = Callable[[PlayerController], Awaitable[None]]


@pytest.fixture
def controller() -> PlayerController:
    """
    Build a players controller over three ungrouped players that can group with each other.

    The actual member change is stubbed, so a test can see whether one was made and with
    which players.
    """
    mass = MagicMock()
    mass.closing = False
    mass.config.get_raw_player_config_value = MagicMock(
        side_effect=lambda _player_id, _key, default=None: default
    )
    mass.config.get_raw_core_config_value = MagicMock(return_value="GLOBAL")
    controller = PlayerController(mass)
    provider = MockProvider("test", instance_id="test", mass=mass)
    for player_id in ("a", "b", "c"):
        player = MockPlayer(provider, player_id, f"Player {player_id.upper()}")
        player._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        player._attr_can_group_with = {"a", "b", "c"} - {player_id}
        controller._players[player_id] = player
    mass.players = controller
    for registered in controller._players.values():
        registered.set_initialized()
        registered.update_state(signal_event=False)
    controller._handle_set_members = AsyncMock()  # type: ignore[method-assign]
    return controller


@contextmanager
def _user(player_filter: list[str], role: UserRole = UserRole.USER) -> Iterator[None]:
    """Run the block as a user limited to the given players (none means unrestricted)."""
    token = current_user.set(
        User(user_id="user_1", username="limited", role=role, player_filter=player_filter)
    )
    try:
        yield
    finally:
        current_user.reset(token)


def _sync(controller: PlayerController, leader_id: str, member_id: str) -> None:
    """Sync the given member to the given leader."""
    leader = controller._players[leader_id]
    leader._attr_group_members = [leader_id, member_id]
    for player in controller._players.values():
        player.update_state(signal_event=False)


def _set_members(
    target: str, add: list[str] | None = None, remove: list[str] | None = None
) -> Command:
    """Return the players/cmd/set_members API command with the given arguments."""
    return lambda controller: controller._api_cmd_set_members(target, add, remove)


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(_set_members("a", add=["b"]), id="set_members-add-other-to-own"),
        pytest.param(_set_members("b", add=["a"]), id="set_members-add-own-to-other"),
        pytest.param(_set_members("a", add=["c", "b"]), id="set_members-one-other-of-many"),
        pytest.param(lambda c: c.cmd_group("b", "a"), id="group-other-to-own"),
        pytest.param(lambda c: c.cmd_group("a", "b"), id="group-own-to-other"),
        pytest.param(lambda c: c.cmd_group_many("a", ["b"]), id="group_many-other-to-own"),
        pytest.param(lambda c: c.cmd_group_many("b", ["a"]), id="group_many-own-to-other"),
    ],
)
async def test_grouping_another_player_is_refused(
    controller: PlayerController, command: Command
) -> None:
    """A user limited to some players can not group in a player outside them, either way."""
    with _user(["a", "c"]), pytest.raises(InsufficientPermissions, match="Player B"):
        await command(controller)

    controller._handle_set_members.assert_not_awaited()  # type: ignore[attr-defined]
    assert all(not p.state.group_members for p in controller._players.values())


async def test_removing_a_player_from_another_players_group_is_refused(
    controller: PlayerController,
) -> None:
    """Taking a player out of a group needs access to the group's leader or that player."""
    _sync(controller, "b", "c")

    with _user(["a"]), pytest.raises(InsufficientPermissions, match="Player C"):
        await controller._api_cmd_set_members("b", player_ids_to_remove=["c"])

    controller._handle_set_members.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(_set_members("b"), id="set_members-nothing"),
        pytest.param(lambda c: c.cmd_group_many("b", []), id="group_many-nothing"),
        pytest.param(_set_members("b", remove=["a"]), id="set_members-remove-non-member"),
    ],
)
async def test_a_synced_player_the_user_can_not_use_stays_synced(
    controller: PlayerController, command: Command
) -> None:
    """A request on a synced player outside the user's players does not detach it."""
    _sync(controller, "c", "b")

    with _user(["a"]), pytest.raises(InsufficientPermissions, match="Player B"):
        await command(controller)

    controller._handle_set_members.assert_not_awaited()  # type: ignore[attr-defined]
    assert controller._players["b"].synced_to == "c"


def _capture(controller: PlayerController, *member_ids: str) -> None:
    """Register a powered group player "g" that captures the given members."""
    provider = MockProvider("group", instance_id="group", mass=cast("MagicMock", controller.mass))
    group = MockPlayer(provider, "g", "Group G", player_type=PlayerType.GROUP)
    group._attr_powered = True
    group._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
    group._attr_group_members = list(member_ids)
    group._cache.clear()
    group.set_initialized()
    controller._players["g"] = group
    # the members derive their active group from the group's state
    group.update_state(signal_event=False)
    for member_id in member_ids:
        controller._players[member_id].update_state(force_update=True, signal_event=False)


@pytest.mark.parametrize(
    ("command", "refused"),
    [
        pytest.param(_set_members("a", remove=["b"]), "Player B", id="remove-other"),
        pytest.param(_set_members("a", add=["c"]), "Group G", id="add-own"),
    ],
)
async def test_a_group_the_user_can_not_use_is_left_alone(
    controller: PlayerController, command: Command, refused: str
) -> None:
    """A command on a member of a group player changes that group, so it needs access to it."""
    _capture(controller, "a", "b")
    assert controller._players["a"].state.active_group == "g"

    with _user(["a", "c"]), pytest.raises(InsufficientPermissions, match=refused):
        await command(controller)

    controller._handle_set_members.assert_not_awaited()  # type: ignore[attr-defined]


async def test_own_player_leaves_a_group_the_user_can_not_use(
    controller: PlayerController,
) -> None:
    """A user may take their own player out of a group player through one of its members."""
    _capture(controller, "a", "b")

    with _user(["b"]):
        await controller._api_cmd_set_members("a", player_ids_to_remove=["b"])

    controller._handle_set_members.assert_awaited_once_with(  # type: ignore[attr-defined]
        controller._players["g"], None, ["b"]
    )


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(_set_members("a", add=["c"]), id="set_members"),
        pytest.param(lambda c: c.cmd_group("c", "a"), id="group"),
        pytest.param(lambda c: c.cmd_group_many("a", ["c"]), id="group_many"),
    ],
)
async def test_grouping_own_players_is_permitted(
    controller: PlayerController, command: Command
) -> None:
    """A user may group the players they have access to."""
    with _user(["a", "c"]):
        await command(controller)

    controller._handle_set_members.assert_awaited_once_with(  # type: ignore[attr-defined]
        controller._players["a"], ["c"], None
    )


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(_set_members("b", remove=["a"]), id="set_members"),
        pytest.param(lambda c: c.cmd_ungroup("a"), id="ungroup"),
    ],
)
async def test_own_player_leaves_a_leader_the_user_can_not_use(
    controller: PlayerController, command: Command
) -> None:
    """A user may always take a player of their own out of a group."""
    _sync(controller, "b", "a")

    with _user(["a"]):
        await command(controller)

    controller._handle_set_members.assert_awaited_once_with(  # type: ignore[attr-defined]
        controller._players["b"], None, ["a"]
    )


async def test_a_member_the_user_can_not_use_is_removed_from_their_leader(
    controller: PlayerController,
) -> None:
    """Whoever may use the leader may drop a member from its group."""
    _sync(controller, "a", "b")

    with _user(["a"]):
        await controller._api_cmd_set_members("a", player_ids_to_remove=["b"])

    controller._handle_set_members.assert_awaited_once_with(  # type: ignore[attr-defined]
        controller._players["a"], None, ["b"]
    )


@pytest.mark.parametrize(
    "user",
    [
        pytest.param(lambda: _user([]), id="empty-filter"),
        pytest.param(lambda: _user(["a"], role=UserRole.ADMIN), id="admin"),
        pytest.param(nullcontext, id="no-user"),
    ],
)
async def test_an_unrestricted_caller_may_group_any_player(
    controller: PlayerController, user: Callable[[], AbstractContextManager[object]]
) -> None:
    """A user without a player filter, an admin and a call without a user are not limited."""
    with user():
        await controller._api_cmd_set_members("a", player_ids_to_add=["b"])

    controller._handle_set_members.assert_awaited_once_with(  # type: ignore[attr-defined]
        controller._players["a"], ["b"], None
    )
