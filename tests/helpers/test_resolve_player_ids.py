"""Tests for resolving the legacy player ids in the arguments of an API command."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from music_assistant import MusicAssistant
from music_assistant.controllers.config import ConfigController
from music_assistant.controllers.discovery import DiscoveryController
from music_assistant.helpers.api import (
    PLAYER_ID_ARGUMENTS,
    APICommandHandler,
    resolve_player_ids,
    run_api_command,
)

LEGACY_ID = "old_kitchen"
PLAYER_ID = "kitchen"
OTHER_PLAYER_ID = "bedroom"

# the arguments that hold a player id, listed apart from the set the resolver reads
EXPECTED_PLAYER_ID_ARGUMENTS = (
    "child_player_ids",
    "members",
    "player_filter",
    "player_id",
    "player_ids",
    "player_ids_to_add",
    "player_ids_to_remove",
    "queue_id",
    "source_queue_id",
    "target_player",
    "target_queue_id",
    "venue_player_id",
    "web_player_id",
)
# the arguments named after a player or queue that hold something else
NO_PLAYER_ID_ARGUMENTS = {"queue_item_id"}


def _mass() -> MusicAssistant:
    """Return a stand-in server whose players controller knows one legacy id."""
    legacy_ids = {LEGACY_ID: PLAYER_ID}
    players = SimpleNamespace(
        resolve_player_id=lambda player_id: legacy_ids.get(player_id, player_id)
    )
    return cast("MusicAssistant", SimpleNamespace(players=players))


@pytest.mark.parametrize("name", EXPECTED_PLAYER_ID_ARGUMENTS)
def test_legacy_id_in_a_player_id_argument_is_resolved(name: str) -> None:
    """Every argument that holds a player id takes a legacy id, alone or in a list."""
    assert resolve_player_ids(_mass(), {name: LEGACY_ID}) == {name: PLAYER_ID}
    assert resolve_player_ids(_mass(), {name: [OTHER_PLAYER_ID, LEGACY_ID]}) == {
        name: [OTHER_PLAYER_ID, PLAYER_ID]
    }


def test_every_player_id_argument_is_tested() -> None:
    """A name added to the player id arguments is added to the tested names as well."""
    assert set(EXPECTED_PLAYER_ID_ARGUMENTS) >= PLAYER_ID_ARGUMENTS


def test_other_arguments_are_left_alone() -> None:
    """Only the arguments that hold a player id are resolved, whatever the others contain."""
    args = {
        "player_id": PLAYER_ID,
        "queue_id": None,
        "queue_item_id": LEGACY_ID,
        "source": LEGACY_ID,
        "values": {"player_id": LEGACY_ID},
        "volume_level": 20,
    }

    assert resolve_player_ids(_mass(), args) == args


def test_arguments_without_a_player_id_need_no_players_controller() -> None:
    """A command that names no player runs on a server that has no players controller yet."""
    mass = cast("MusicAssistant", SimpleNamespace())

    assert resolve_player_ids(mass, {"username": "someone"}) == {"username": "someone"}


def test_the_given_arguments_are_not_changed() -> None:
    """The caller keeps the arguments it passed in."""
    args = {"player_ids": [LEGACY_ID]}

    resolve_player_ids(_mass(), args)

    assert args == {"player_ids": [LEGACY_ID]}


async def test_command_reaches_its_handler_with_the_player_ids_resolved() -> None:
    """A coroutine handler is called with the resolved ids and its result is handed back."""

    async def group(player_id: str, target_player: str) -> tuple[str, str]:
        return player_id, target_player

    handler = APICommandHandler.parse("test/group", group)

    result = await run_api_command(
        _mass(), handler, {"player_id": OTHER_PLAYER_ID, "target_player": LEGACY_ID}
    )

    assert result == (OTHER_PLAYER_ID, PLAYER_ID)


async def test_command_hands_back_the_async_generator_of_its_handler() -> None:
    """A listing handler keeps streaming its items, for the player with the resolved id."""

    async def items(queue_id: str) -> AsyncGenerator[str]:
        yield queue_id

    handler = APICommandHandler.parse("test/items", items)

    result = run_api_command(_mass(), handler, {"queue_id": LEGACY_ID})

    assert [item async for item in result] == [PLAYER_ID]


async def test_every_player_id_argument_of_the_core_commands_is_resolved(tmp_path: Path) -> None:
    """An argument of a core command that is named after a player or queue is accounted for."""
    mass = MusicAssistant(str(tmp_path), str(tmp_path))
    mass.config = ConfigController(mass)
    mass.config.initialized = True
    mass.discovery = DiscoveryController(mass)
    await mass._load_core_controllers()
    mass._register_api_commands()

    unaccounted: dict[str, Any] = {
        command: names
        for command, handler in mass.command_handlers.items()
        if (
            names := sorted(
                name
                for name in handler.signature.parameters
                if ("player" in name or "queue" in name)
                and name not in PLAYER_ID_ARGUMENTS | NO_PLAYER_ID_ARGUMENTS
            )
        )
    }

    assert unaccounted == {}
