"""
Tests that the grouping commands follow the calling user's player filter.

Adding players needs access to the target player and to every player added. Removing players
needs access to the target player or to every player removed; when the target player is a
member of a group player, removing needs access to that group player as well. A target player
that is synced to another player is detached first, so a request that adds nothing needs
access to it too.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import AbstractContextManager, nullcontext
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from music_assistant_models.auth import UserRole
from music_assistant_models.enums import PlayerFeature, PlayerType
from music_assistant_models.errors import InsufficientPermissions

from music_assistant.controllers.players import PlayerController
from tests.common import MockPlayer, MockProvider, as_user, grouping_player_controller

type Command = Callable[[PlayerController], Awaitable[None]]


@pytest.fixture
def controller() -> PlayerController:
    """Build a players controller over three ungrouped players that can group together."""
    return grouping_player_controller("a", "b", "c")


def _api(controller: PlayerController, command: str) -> Callable[..., Awaitable[Any]]:
    """Return the handler the server runs for the given API command."""
    return cast("MagicMock", controller.mass).command_handlers[command].target  # type: ignore[no-any-return]


def _sync(controller: PlayerController, leader_id: str, member_id: str) -> None:
    """Sync the given member to the given leader."""
    leader = controller._players[leader_id]
    leader._attr_group_members = [leader_id, member_id]
    for player in controller._players.values():
        player.update_state(signal_event=False)


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


def _set_members(
    target: str, add: list[str] | None = None, remove: list[str] | None = None
) -> Command:
    """Return the players/cmd/set_members API command with the given arguments."""
    return lambda c: _api(c, "players/cmd/set_members")(target, add, remove)


def _group(player_id: str, target: str) -> Command:
    """Return the players/cmd/group API command with the given arguments."""
    return lambda c: _api(c, "players/cmd/group")(player_id, target)


def _group_many(target: str, child_player_ids: list[str]) -> Command:
    """Return the players/cmd/group_many API command with the given arguments."""
    return lambda c: _api(c, "players/cmd/group_many")(target, child_player_ids)


def _assert_unchanged(controller: PlayerController) -> None:
    """Assert that no member change was made."""
    controller._handle_set_members.assert_not_awaited()  # type: ignore[attr-defined]


def _assert_changed(
    controller: PlayerController,
    parent_id: str,
    add: list[str] | None = None,
    remove: list[str] | None = None,
) -> None:
    """Assert that exactly the given member change was made."""
    controller._handle_set_members.assert_awaited_once_with(  # type: ignore[attr-defined]
        controller._players[parent_id], add, remove
    )


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(_set_members("a", add=["b"]), id="set_members-add-other-to-own"),
        pytest.param(_set_members("b", add=["a"]), id="set_members-add-own-to-other"),
        pytest.param(_set_members("a", add=["c", "b"]), id="set_members-one-other-of-many"),
        pytest.param(_set_members("b", add=["a"], remove=["c"]), id="set_members-mixed"),
        pytest.param(_group("b", "a"), id="group-other-to-own"),
        pytest.param(_group("a", "b"), id="group-own-to-other"),
        pytest.param(_group_many("a", ["b"]), id="group_many-other-to-own"),
        pytest.param(_group_many("b", ["a"]), id="group_many-own-to-other"),
    ],
)
async def test_grouping_another_player_is_refused(
    controller: PlayerController, command: Command
) -> None:
    """A user limited to some players can not group in a player outside them, either way."""
    with as_user(["a", "c"]), pytest.raises(InsufficientPermissions, match="Player B"):
        await command(controller)

    _assert_unchanged(controller)
    assert all(not p.state.group_members for p in controller._players.values())


async def test_removing_a_player_from_another_players_group_is_refused(
    controller: PlayerController,
) -> None:
    """Taking a player out of a group needs access to the group's leader or that player."""
    _sync(controller, "b", "c")

    with as_user(["a"]), pytest.raises(InsufficientPermissions, match="Player C"):
        await _set_members("b", remove=["c"])(controller)

    _assert_unchanged(controller)


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(_set_members("b"), id="set_members-nothing"),
        pytest.param(_group_many("b", []), id="group_many-nothing"),
        pytest.param(_set_members("b", remove=["a"]), id="set_members-remove-non-member"),
    ],
)
async def test_a_synced_player_the_user_can_not_use_stays_synced(
    controller: PlayerController, command: Command
) -> None:
    """A request on a synced player outside the user's players does not detach it."""
    _sync(controller, "c", "b")

    with as_user(["a"]), pytest.raises(InsufficientPermissions, match="Player B"):
        await command(controller)

    _assert_unchanged(controller)
    assert controller._players["b"].synced_to == "c"


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(_set_members("a", add=["c"]), id="set_members"),
        pytest.param(_group("c", "a"), id="group"),
        pytest.param(_group_many("a", ["c"]), id="group_many"),
    ],
)
async def test_own_player_joins_a_group_the_user_can_not_use_through_a_member(
    controller: PlayerController, command: Command
) -> None:
    """Adding a player of one's own through a member of a group player is passed on to it."""
    _capture(controller, "a", "b")
    assert controller._players["a"].state.active_group == "g"

    with as_user(["a", "c"]):
        await command(controller)

    _assert_changed(controller, "g", add=["c"])


@pytest.mark.parametrize(
    "add",
    [
        pytest.param(None, id="remove"),
        pytest.param(["c"], id="mixed"),
    ],
)
async def test_removing_another_member_of_a_group_the_user_can_not_use_is_refused(
    controller: PlayerController, add: list[str] | None
) -> None:
    """A member of one's own does not let a user drop another member of its group player."""
    _capture(controller, "a", "b")

    with as_user(["a", "c"]), pytest.raises(InsufficientPermissions, match="Player B"):
        await _set_members("a", add=add, remove=["b"])(controller)

    _assert_unchanged(controller)
    assert set(controller._players["g"].state.group_members) == {"a", "b"}


@pytest.mark.parametrize(
    "target",
    [
        pytest.param("a", id="through-member"),
        pytest.param("g", id="group-player"),
    ],
)
async def test_own_player_leaves_a_group_the_user_can_not_use(
    controller: PlayerController, target: str
) -> None:
    """A user may take their own player out of a group player."""
    _capture(controller, "a", "b")

    with as_user(["b"]):
        await _set_members(target, remove=["b"])(controller)

    _assert_changed(controller, "g", remove=["b"])


async def test_own_synced_player_leaves_a_group_the_user_can_not_use(
    controller: PlayerController,
) -> None:
    """A user may take their own player out of a group player while it is synced in it."""
    _sync(controller, "c", "a")
    _capture(controller, "c", "a")
    assert controller._players["a"].synced_to == "c"

    with as_user(["a"]):
        await _set_members("a", remove=["a"])(controller)

    _assert_changed(controller, "g", remove=["a"])


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(_set_members("a", add=["c"]), id="set_members"),
        pytest.param(_group("c", "a"), id="group"),
        pytest.param(_group_many("a", ["c"]), id="group_many"),
    ],
)
async def test_grouping_own_players_is_permitted(
    controller: PlayerController, command: Command
) -> None:
    """A user may group the players they have access to."""
    with as_user(["a", "c"]):
        await command(controller)

    _assert_changed(controller, "a", add=["c"])


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(_set_members("b", remove=["a"]), id="set_members"),
        pytest.param(lambda c: _api(c, "players/cmd/ungroup")("a"), id="ungroup"),
    ],
)
async def test_own_player_leaves_a_leader_the_user_can_not_use(
    controller: PlayerController, command: Command
) -> None:
    """A user may always take a player of their own out of a group."""
    _sync(controller, "b", "a")

    with as_user(["a"]):
        await command(controller)

    _assert_changed(controller, "b", remove=["a"])


async def test_a_member_the_user_can_not_use_is_removed_from_their_leader(
    controller: PlayerController,
) -> None:
    """Whoever may use the leader may drop a member from its group."""
    _sync(controller, "a", "b")

    with as_user(["a"]):
        await _set_members("a", remove=["b"])(controller)

    _assert_changed(controller, "a", remove=["b"])


@pytest.mark.parametrize(
    "user",
    [
        pytest.param(lambda: as_user([]), id="empty-filter"),
        pytest.param(lambda: as_user(["a"], role=UserRole.ADMIN), id="admin"),
        pytest.param(nullcontext, id="no-user"),
    ],
)
async def test_an_unrestricted_caller_may_group_any_player(
    controller: PlayerController, user: Callable[[], AbstractContextManager[object]]
) -> None:
    """A user without a player filter, an admin and a call without a user are not limited."""
    with user():
        await _set_members("a", add=["b"])(controller)

    _assert_changed(controller, "a", add=["b"])
