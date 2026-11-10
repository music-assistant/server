"""Tests for the legacy ids a player keeps answering to."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import MagicMock

import pytest

from music_assistant.constants import CONF_LEGACY_IDS, CONF_PLAYERS
from music_assistant.controllers.players import PlayerController
from tests.common import MockPlayer, MockProvider

KITCHEN = "kitchen"
BEDROOM = "bedroom"


def _stored(*legacy_ids: Any) -> dict[str, Any]:
    """Return the stored config of a player that holds the given legacy ids."""
    return {"values": {CONF_LEGACY_IDS: list(legacy_ids)}}


def _controller(stored_configs: Any) -> PlayerController:
    """
    Return a players controller on a server that stores the given player configs.

    :param stored_configs: The stored config of every player, by player id.
    """
    mass = MagicMock()
    mass.config.get_raw_core_config_value.return_value = "GLOBAL"
    mass.config.get = MagicMock(
        side_effect=lambda key, default=None: stored_configs if key == CONF_PLAYERS else default
    )
    mass.config.remove = MagicMock(
        side_effect=lambda key: stored_configs.pop(key.removeprefix(f"{CONF_PLAYERS}/"), None)
    )
    controller = PlayerController(mass)
    mass.players = controller
    controller._build_legacy_id_index()
    return controller


def _register(controller: PlayerController, player_id: str) -> MockPlayer:
    """Put a player with the given id in the registry of the controller."""
    mass = cast("MagicMock", controller.mass)
    provider = MockProvider("test_provider", instance_id="test", mass=mass)
    player = MockPlayer(provider, player_id, player_id.title())
    controller._players[player_id] = player
    return player


def test_legacy_id_resolves_to_the_player_that_holds_it() -> None:
    """A player answers to its own id and to every legacy id it holds."""
    controller = _controller({KITCHEN: _stored("old_kitchen", "older_kitchen")})
    kitchen = _register(controller, KITCHEN)

    assert controller.resolve_player_id("old_kitchen") == KITCHEN
    assert controller.resolve_player_id("older_kitchen") == KITCHEN
    assert controller.resolve_player_id(KITCHEN) == KITCHEN
    assert controller.get_player("old_kitchen") is kitchen
    assert controller.get_player(KITCHEN) is kitchen


def test_unknown_id_is_returned_as_it_is() -> None:
    """An id no player answers to resolves to itself and finds no player."""
    controller = _controller({KITCHEN: _stored("old_kitchen")})

    assert controller.resolve_player_id("garage") == "garage"
    assert controller.get_player("garage") is None


def test_legacy_id_of_a_player_that_is_not_registered_still_resolves() -> None:
    """The legacy id of a stored player that is offline names that player, without finding it."""
    controller = _controller({KITCHEN: _stored("old_kitchen")})

    assert controller.resolve_player_id("old_kitchen") == KITCHEN
    assert controller.get_player("old_kitchen") is None


def test_every_player_answers_to_its_own_legacy_ids() -> None:
    """Legacy ids of several players each resolve to the player that holds them."""
    controller = _controller({KITCHEN: _stored("old_kitchen"), BEDROOM: _stored("old_bedroom")})

    assert controller.resolve_player_id("old_kitchen") == KITCHEN
    assert controller.resolve_player_id("old_bedroom") == BEDROOM


def test_legacy_id_held_by_two_players_stays_with_the_first() -> None:
    """A legacy id that two stored players hold keeps resolving to the same one of them."""
    controller = _controller({KITCHEN: _stored("shared"), BEDROOM: _stored("shared")})

    assert controller.resolve_player_id("shared") == KITCHEN


def test_id_of_a_stored_player_is_never_a_legacy_id() -> None:
    """A stored player keeps its own id, also when another player lists it as a legacy id."""
    controller = _controller({KITCHEN: _stored(BEDROOM), BEDROOM: _stored()})

    assert controller.resolve_player_id(BEDROOM) == BEDROOM


def test_player_whose_config_is_stored_later_keeps_its_own_id() -> None:
    """An id stops being a legacy id as soon as a player config is stored under it."""
    stored_configs = {KITCHEN: _stored("old_kitchen")}
    controller = _controller(stored_configs)
    _register(controller, KITCHEN)

    # a device that comes back under its former id while its player is disabled or offline
    stored_configs["old_kitchen"] = _stored()

    assert controller.resolve_player_id("old_kitchen") == "old_kitchen"
    assert controller.get_player("old_kitchen") is None


def test_registered_player_keeps_its_own_id() -> None:
    """A player that registers under the legacy id of another player is found by that id."""
    controller = _controller({KITCHEN: _stored("old_kitchen")})
    _register(controller, KITCHEN)
    newcomer = _register(controller, "old_kitchen")

    assert controller.resolve_player_id("old_kitchen") == "old_kitchen"
    assert controller.get_player("old_kitchen") is newcomer


def test_legacy_ids_of_a_deleted_player_no_longer_resolve() -> None:
    """Deleting the config of a player ends the legacy ids it held."""
    controller = _controller({KITCHEN: _stored("old_kitchen"), BEDROOM: _stored("old_bedroom")})

    controller.delete_player_config(KITCHEN)

    assert controller.resolve_player_id("old_kitchen") == "old_kitchen"
    assert controller.resolve_player_id("old_bedroom") == BEDROOM


def test_id_of_a_deleted_player_becomes_a_legacy_id_of_the_player_that_lists_it() -> None:
    """A player that lists the id of another one answers to it once that player is deleted."""
    controller = _controller({KITCHEN: _stored(BEDROOM), BEDROOM: _stored()})

    _register(controller, KITCHEN)

    controller.delete_player_config(BEDROOM)

    assert controller.resolve_player_id(BEDROOM) == KITCHEN
    # the deleted player is gone with its saved queue, whoever answers to its id from now on
    purge = cast("MagicMock", controller.mass).player_queues.purge_saved_queue
    purge.assert_called_once_with(BEDROOM)


@pytest.mark.parametrize(
    "stored_configs",
    [
        pytest.param([], id="players_not_a_mapping"),
        pytest.param({KITCHEN: "broken"}, id="config_not_a_mapping"),
        pytest.param({KITCHEN: {"values": None}}, id="values_missing"),
        pytest.param({KITCHEN: {"values": {CONF_LEGACY_IDS: "old_kitchen"}}}, id="ids_not_a_list"),
        pytest.param({KITCHEN: _stored(None, 7, ["old_kitchen"])}, id="ids_not_strings"),
    ],
)
def test_malformed_stored_legacy_ids_are_ignored(stored_configs: Any) -> None:
    """Stored data of an unexpected shape yields no legacy id and does not fail."""
    controller = _controller(stored_configs)

    assert controller._legacy_ids == {}
    assert controller.resolve_player_id("old_kitchen") == "old_kitchen"
