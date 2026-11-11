"""Tests that the group_players MCP tool follows the grouping rules of the players controller."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import PlayerFeature
from music_assistant_models.errors import InsufficientPermissions

from music_assistant.controllers.players import PlayerController
from music_assistant.providers.mcp_server.registry import TOOLS, ToolError
from tests.common import MockProvider, as_user, attach_mock


@pytest.fixture
def controller() -> PlayerController:
    """
    Build a players controller over three ungrouped players the tool can see.

    The players "pl_a", "pl_b" and "pl_c" are named "Player A" and so on, and their
    protocols "a", "b" and "c" can group with each other.
    """
    mass = MagicMock()
    mass.closing = False
    mass.config.get_player_value = MagicMock(
        side_effect=lambda _player_id, _key, default=None: default
    )
    mass.config.get_raw_core_config_value = MagicMock(return_value="GLOBAL")
    controller = PlayerController(mass)
    mass.players = controller
    provider = MockProvider("test", instance_id="test", mass=mass)
    for protocol_id in ("a", "b", "c"):
        _, protocol = attach_mock(provider, protocol_id, f"Player {protocol_id.upper()}")
        protocol._attr_supported_features.add(PlayerFeature.SET_MEMBERS)
        protocol._attr_can_group_with = {"a", "b", "c"} - {protocol_id}
    for player in controller._players.values():
        player.refresh_state(signal_event=False)
    controller._handle_set_members = AsyncMock()  # type: ignore[method-assign]
    return controller


async def _group_players(controller: PlayerController, player: str, members: list[str]) -> Any:
    """Call the group_players tool."""
    return await TOOLS["group_players"].handler(controller.mass, player=player, members=members)


async def test_grouping_another_player_is_refused(controller: PlayerController) -> None:
    """The tool does not group in a player outside the user's players."""
    with as_user(["pl_a", "pl_c"]), pytest.raises(ToolError, match="Unknown player 'pl_b'"):
        await _group_players(controller, "pl_a", ["pl_b"])

    controller._handle_set_members.assert_not_awaited()  # type: ignore[attr-defined]


async def test_the_grouping_check_runs_before_the_change(controller: PlayerController) -> None:
    """The tool asks the players controller whether the user may make the change."""
    check = MagicMock(side_effect=InsufficientPermissions("refused"))
    controller.check_grouping_access = check  # type: ignore[method-assign]

    with as_user(["pl_a", "pl_c"]), pytest.raises(InsufficientPermissions):
        await _group_players(controller, "Player A", ["pl_c"])

    check.assert_called_once_with("pl_a", player_ids_to_add=["pl_c"])
    controller._handle_set_members.assert_not_awaited()  # type: ignore[attr-defined]


async def test_grouping_own_players_is_permitted(controller: PlayerController) -> None:
    """The tool groups the players the user has access to."""
    with as_user(["pl_a", "pl_c"]):
        await _group_players(controller, "pl_a", ["Player C"])

    cast("MagicMock", controller._handle_set_members).assert_awaited_once_with(
        controller._players["pl_a"], ["pl_c"], None
    )
