"""Tests for the legacy ids a player keeps answering to."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast
from unittest.mock import MagicMock

import pytest
from music_assistant_models.enums import IdentifierType, PlayerFeature

from music_assistant.constants import CONF_LEGACY_IDS, CONF_PLAYERS
from music_assistant.controllers.players import PlayerController
from music_assistant.controllers.players.constants import PlayerLockPurpose
from tests.common import MockProvider, attach_mock
from tests.scenarios.harness import DEFAULT_FEATURES

if TYPE_CHECKING:
    from music_assistant.models.player import Player
    from tests.scenarios.harness import FakeProtocol, Scenario

KITCHEN = "kitchen"
BEDROOM = "bedroom"
# the id of a player that was merged into the living room player
OLD_LIVING = "old_living"
GROUPING = frozenset({*DEFAULT_FEATURES, PlayerFeature.SET_MEMBERS, PlayerFeature.POWER})


def _stored(*legacy_ids: Any) -> dict[str, Any]:
    """Return the stored config of a player that holds the given legacy ids."""
    return {"values": {CONF_LEGACY_IDS: list(legacy_ids)}}


def _controller(stored_configs: Any) -> PlayerController:
    """
    Return a players controller on a server that stores the given player configs.

    :param stored_configs: The stored config of every player, by player id.
    """
    mass = MagicMock()
    mass.closing = False
    mass.config.get_raw_core_config_value.return_value = "GLOBAL"

    def _get(key: str, default: Any = None) -> Any:
        if key == CONF_PLAYERS:
            return stored_configs
        if key.startswith(f"{CONF_PLAYERS}/"):
            return stored_configs.get(key.removeprefix(f"{CONF_PLAYERS}/"), default)
        return default

    mass.config.get = MagicMock(side_effect=_get)
    mass.config.get_player_value = MagicMock(
        side_effect=lambda player_id, key, default=None: (
            stored_configs.get(player_id, {}).get("values", {}).get(key, default)
        )
    )
    mass.config.remove = MagicMock(
        side_effect=lambda key: stored_configs.pop(key.removeprefix(f"{CONF_PLAYERS}/"), None)
    )
    mass.config.get_protocol_ids_of_player = MagicMock(return_value=[])
    mass.config.get_protocol_record = MagicMock(return_value=None)
    controller = PlayerController(mass)
    mass.players = controller
    controller._build_legacy_id_index()
    return controller


def _register(controller: PlayerController, player_id: str) -> Player:
    """Put a live player with the given id, attached to a protocol of its own, in the registry."""
    mass = cast("MagicMock", controller.mass)
    provider = MockProvider("test_provider", instance_id="test", mass=mass)
    player, _ = attach_mock(
        provider, f"{player_id}_speaker", player_id.title(), player_id=player_id
    )
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


def test_legacy_id_of_a_player_that_is_not_live_still_resolves() -> None:
    """The legacy id of a stored player without a live object names it, without finding it."""
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


def test_live_player_keeps_its_own_id() -> None:
    """A live player whose id is the legacy id of another player is found by that id."""
    controller = _controller({KITCHEN: _stored("old_kitchen")})
    _register(controller, KITCHEN)
    newcomer = _register(controller, "old_kitchen")

    assert controller.resolve_player_id("old_kitchen") == "old_kitchen"
    assert controller.get_player("old_kitchen") is newcomer


async def test_legacy_ids_of_a_removed_player_no_longer_resolve() -> None:
    """Removing a player ends the legacy ids it held."""
    controller = _controller({KITCHEN: _stored("old_kitchen"), BEDROOM: _stored("old_bedroom")})

    await controller.remove(KITCHEN)

    assert controller.resolve_player_id("old_kitchen") == "old_kitchen"
    assert controller.resolve_player_id("old_bedroom") == BEDROOM


async def test_id_of_a_removed_player_becomes_a_legacy_id_of_the_player_that_lists_it() -> None:
    """A player that lists the id of another one answers to it once that player is removed."""
    controller = _controller({KITCHEN: _stored(BEDROOM), BEDROOM: _stored()})

    _register(controller, KITCHEN)

    await controller.remove(BEDROOM)

    assert controller.resolve_player_id(BEDROOM) == KITCHEN
    # the deleted player is gone with its saved queue, whoever answers to its id from now on
    purge = cast("MagicMock", controller.mass).player_queues.purge_saved_queue
    purge.assert_called_once_with(BEDROOM)


def test_state_of_a_player_lists_its_legacy_ids() -> None:
    """Clients read the legacy ids of a player from the state it publishes."""
    controller = _controller({KITCHEN: _stored("old_kitchen", "older_kitchen"), BEDROOM: _stored()})
    kitchen = _register(controller, KITCHEN)
    bedroom = _register(controller, BEDROOM)

    kitchen.update_state(signal_event=False)
    bedroom.update_state(signal_event=False)

    assert kitchen.legacy_ids == ["old_kitchen", "older_kitchen"]
    assert kitchen.to_dict()["legacy_ids"] == ["old_kitchen", "older_kitchen"]
    assert bedroom.legacy_ids == []


async def test_absorbed_frozen_id_names_the_keeper_and_still_its_protocol(
    scenario: Scenario,
) -> None:
    """
    An absorbed id that its protocol keeps resolves to the keeper as a player and queue id.

    A frozen id is shared by a player and its protocol. Once that player is merged into
    another, its id is a legacy id of the keeper, while the protocol of that id, now
    attached to the keeper, is still found by it.
    """
    sonos = await scenario.add_provider("sonos")
    airplay = await scenario.add_provider("airplay")
    mac = {IdentifierType.MAC_ADDRESS: "54:78:C9:E6:0D:D4"}
    native = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=mac)
    audio = airplay.protocol("ap_kitchen", "Kitchen", role="audio", identifiers=mac)
    await scenario.register(native)
    await scenario.register(audio)
    keeper_id = scenario.single_player(native, audio).player_id
    scenario.mass.config.set_player_value(keeper_id, CONF_LEGACY_IDS, ["ap_kitchen"])
    players = scenario.mass.players
    players._build_legacy_id_index()

    assert scenario.mass.config.get_protocol_record("ap_kitchen") is not None
    assert players.resolve_player_id("ap_kitchen") == keeper_id
    assert players.get_player("ap_kitchen") is players.get_player(keeper_id)
    assert scenario.mass.player_queues.get("ap_kitchen") is scenario.queue(keeper_id)
    assert players.get_protocol("ap_kitchen") is audio
    assert players.get_player_by_protocol("ap_kitchen") is players.get_player(keeper_id)


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


async def test_volume_command_by_a_legacy_id_keeps_the_limit_and_lock_of_the_player(
    scenario: Scenario,
) -> None:
    """A volume command by a legacy id is held to the player's volume limit and waits for its lock."""
    living_id, living, _hall_id = await _living_room_with_a_legacy_id(scenario)
    await scenario.configure_player(living_id, max_volume=40)
    await scenario.idle()
    players = scenario.mass.players
    living.commands.clear()

    await players.cmd_volume_set(OLD_LIVING, 80)
    await scenario.idle()

    assert [args for command, *args in living.commands if command == "volume_set"] == [[32]]
    async with players.get_player_lock(living_id, PlayerLockPurpose.VOLUME):
        task = await scenario.command(players.cmd_volume_set(OLD_LIVING, 10))
        assert not task.done()
    await scenario.settle()
    await task
    assert not [key for key in players._player_command_locks if OLD_LIVING in key]


async def test_power_command_by_a_legacy_id_waits_for_the_playback_lock_of_the_player(
    scenario: Scenario,
) -> None:
    """A power on by a legacy id is serialized with the playback of the player."""
    living_id, living, _hall_id = await _living_room_with_a_legacy_id(scenario)
    players = scenario.mass.players

    async with players.get_player_lock(living_id, PlayerLockPurpose.PLAYBACK):
        task = await scenario.command(players.cmd_power(OLD_LIVING, True))
        assert not task.done()
    await scenario.settle()
    await task
    assert ("power", True) in living.commands


@pytest.mark.parametrize("command", ["set_members", "group"])
async def test_group_change_by_a_legacy_id_admits_the_player(
    scenario: Scenario, command: str
) -> None:
    """A member named by a legacy id joins the group as the player that answers to it."""
    living_id, _living, hall_id = await _living_room_with_a_legacy_id(scenario)
    players = scenario.mass.players

    if command == "group":
        await players.cmd_group(OLD_LIVING, hall_id)
    else:
        await players.cmd_set_members(hall_id, player_ids_to_add=[OLD_LIVING])
    await scenario.idle()

    hall, living = scenario.player(hall_id), scenario.player(living_id)
    assert hall is not None
    assert living is not None
    assert (hall.group_members, living.synced_to) == ([hall_id, living_id], hall_id)


async def _living_room_with_a_legacy_id(scenario: Scenario) -> tuple[str, FakeProtocol, str]:
    """
    Return the living room player, which answers to a legacy id, its protocol and the hall player.

    The two can be grouped with each other.

    :param scenario: The running scenario.
    """
    airplay = await scenario.add_provider("airplay")
    living = airplay.protocol(
        "ap_living",
        "Living Room",
        role="native",
        identifiers={IdentifierType.MAC_ADDRESS: "54:78:C9:E6:0D:E1"},
        supported_features=set(GROUPING),
        can_group_with={"ap_hall"},
        powered=False,
    )
    hall = airplay.protocol(
        "ap_hall",
        "Hall",
        role="native",
        identifiers={IdentifierType.MAC_ADDRESS: "54:78:C9:E6:0D:E2"},
        supported_features=set(GROUPING),
        can_group_with={"ap_living"},
    )
    await scenario.register(living)
    await scenario.register(hall)
    await scenario.idle()
    living_id = scenario.single_player(living).player_id
    scenario.mass.config.set_player_value(living_id, CONF_LEGACY_IDS, [OLD_LIVING])
    scenario.mass.players._build_legacy_id_index()
    return living_id, living, scenario.single_player(hall).player_id
