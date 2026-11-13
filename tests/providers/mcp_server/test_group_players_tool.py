"""Tests that the group_players MCP tool follows the grouping rules of the players controller."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from music_assistant_models.errors import InsufficientPermissions

from music_assistant.constants import CONF_HIDE_IN_UI
from music_assistant.controllers.players import PlayerController
from music_assistant.providers.mcp_server.registry import TOOLS, ToolError
from tests.common import as_user, grouping_player_controller


@pytest.fixture
def controller() -> PlayerController:
    """Build a players controller over three ungrouped players the tool can see."""
    controller = grouping_player_controller("a", "b", "c")
    for player in controller._players.values():
        cast("MagicMock", player._config.get_value).side_effect = lambda key, _default=None: (
            False if key == CONF_HIDE_IN_UI else "none"
        )
        player._cache.clear()
        player.update_state(force_update=True, signal_event=False)
    return controller


async def _group_players(controller: PlayerController, player: str, members: list[str]) -> Any:
    """Call the group_players tool."""
    return await TOOLS["group_players"].handler(controller.mass, player=player, members=members)


async def test_grouping_another_player_is_refused(controller: PlayerController) -> None:
    """The tool does not group in a player outside the user's players."""
    with as_user(["a", "c"]), pytest.raises(ToolError, match="Unknown player 'b'"):
        await _group_players(controller, "a", ["b"])

    controller._handle_set_members.assert_not_awaited()  # type: ignore[attr-defined]


async def test_the_grouping_check_runs_before_the_change(controller: PlayerController) -> None:
    """The tool asks the players controller whether the user may make the change."""
    check = MagicMock(side_effect=InsufficientPermissions("refused"))
    controller.check_grouping_access = check  # type: ignore[method-assign]

    with as_user(["a", "c"]), pytest.raises(InsufficientPermissions):
        await _group_players(controller, "Player A", ["c"])

    check.assert_called_once_with("a", player_ids_to_add=["c"])
    controller._handle_set_members.assert_not_awaited()  # type: ignore[attr-defined]


async def test_grouping_own_players_is_permitted(controller: PlayerController) -> None:
    """The tool groups the players the user has access to."""
    with as_user(["a", "c"]):
        await _group_players(controller, "a", ["c"])

    cast("MagicMock", controller._handle_set_members).assert_awaited_once_with(
        controller._players["a"], ["c"], None
    )
