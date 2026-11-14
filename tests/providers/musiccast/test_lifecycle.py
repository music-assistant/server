"""Tests for MusicCast zones on a running server: one player per zone, config per zone."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

from music_assistant.controllers.players.protocol_linking import PLAYER_ID_PREFIX
from music_assistant.providers.musiccast.constants import (
    CONF_PLAYER_TURN_OFF_ON_LEAVE,
    PLAYER_ZONE_SPLITTER,
)
from music_assistant.providers.musiccast.musiccast import (
    MusicCastController,
    MusicCastPhysicalDevice,
    MusicCastZoneDevice,
)
from music_assistant.providers.musiccast.player import MusicCastPlayer

from .conftest import FakeMusicCastDevice

if TYPE_CHECKING:
    from music_assistant.models.player import Player
    from tests.scenarios.harness import Scenario

DEVICE_ID = "AC44F2851234"
DEVICE_IP = "192.168.1.40"


def _receiver() -> MusicCastPhysicalDevice:
    """Return a receiver with a main zone, a zone2 and a zone3."""
    device = FakeMusicCastDevice(DEVICE_IP, DEVICE_ID)
    # the network status the main zone reads for its identifiers
    device.request_json = AsyncMock(return_value={})  # type: ignore[attr-defined]
    physical_device = MusicCastPhysicalDevice(
        device=device,
        controller=MusicCastController(logger=None),  # type: ignore[arg-type]
    )
    for zone_name in device.data.zones:
        physical_device.zone_devices[zone_name] = MusicCastZoneDevice(zone_name, physical_device)
    return physical_device


async def _register_zone(
    scenario: Scenario, physical_device: MusicCastPhysicalDevice, zone_name: str
) -> MusicCastPlayer:
    """Register the protocol of a zone of a receiver, as the provider does."""
    protocol = MusicCastPlayer(
        provider=scenario.provider("musiccast"),  # type: ignore[arg-type]
        protocol_id=f"{DEVICE_ID}{PLAYER_ZONE_SPLITTER}{zone_name}",
        physical_device=physical_device,
        zone_device=physical_device.zone_devices[zone_name],
    )
    await protocol.set_static_attributes()
    # the poll reads the UPnP state of the receiver, which is not there
    protocol._attr_needs_poll = False
    await scenario.mass.players.register(protocol)
    await scenario.settle()
    return protocol


def _player(protocol: MusicCastPlayer) -> Player:
    """Return the player a protocol is attached to."""
    assert protocol.player is not None
    return protocol.player


async def test_each_zone_of_a_receiver_is_a_player_of_its_own(scenario: Scenario) -> None:
    """A zone never lands on the player of another zone, also while that zone is away."""
    await scenario.add_provider("musiccast")
    receiver = _receiver()
    main = await _register_zone(scenario, receiver, "main")
    zone2 = await _register_zone(scenario, receiver, "zone2")
    assert _player(main) is not _player(zone2)
    main_player = _player(main).player_id
    await scenario.mass.players.unregister(main.protocol_id)
    await scenario.settle()

    zone3 = await _register_zone(scenario, receiver, "zone3")

    assert _player(zone3).player_id not in (main_player, _player(zone2).player_id)
    assert _player(zone3).player_id.startswith(PLAYER_ID_PREFIX)
    main = await _register_zone(scenario, receiver, "main")
    assert _player(main).player_id == main_player
    assert sorted(player.name for player in scenario.visible_players()) == [
        "Garage",
        "Living",
        "Patio",
    ]


async def test_each_zone_finds_its_own_player_after_a_restart(scenario: Scenario) -> None:
    """After a restart the zones come back in any order, each on its own player."""
    await scenario.add_provider("musiccast")
    receiver = _receiver()
    main = await _register_zone(scenario, receiver, "main")
    zone2 = await _register_zone(scenario, receiver, "zone2")
    main_player, zone2_player = _player(main).player_id, _player(zone2).player_id
    await scenario.configure_player(zone2_player, name="Patio amp")

    await scenario.restart()
    receiver = _receiver()
    zone2 = await _register_zone(scenario, receiver, "zone2")
    main = await _register_zone(scenario, receiver, "main")

    assert _player(main).player_id == main_player
    assert _player(zone2).player_id == zone2_player
    assert _player(zone2).name == "Patio amp"


async def test_a_new_zone_does_not_take_the_record_of_the_main_zone_after_a_restart(
    scenario: Scenario,
) -> None:
    """A zone first seen after a restart, before the main zone is back, gets a player of its own."""
    await scenario.add_provider("musiccast")
    main = await _register_zone(scenario, _receiver(), "main")
    main_player = _player(main).player_id
    await scenario.restart()

    receiver = _receiver()
    zone2 = await _register_zone(scenario, receiver, "zone2")

    assert _player(zone2).player_id != main_player
    assert scenario.mass.config.get_protocol_ids_of_player(main_player) == [main.protocol_id]
    main = await _register_zone(scenario, receiver, "main")
    assert _player(main).player_id == main_player


async def test_a_setting_of_a_new_zone_is_read_from_its_own_record(scenario: Scenario) -> None:
    """A zone setting saved on a player with a minted id is the zone's, also after a return."""
    await scenario.add_provider("musiccast")
    receiver = _receiver()
    zone2 = await _register_zone(scenario, receiver, "zone2")
    player_id = _player(zone2).player_id
    assert player_id != zone2.protocol_id

    await scenario.configure_player(player_id, **{CONF_PLAYER_TURN_OFF_ON_LEAVE: True})

    assert zone2.get_config_value(CONF_PLAYER_TURN_OFF_ON_LEAVE) is True
    assert scenario.protocol_config(zone2.protocol_id)[CONF_PLAYER_TURN_OFF_ON_LEAVE] is True  # type: ignore[index]
    player_values = scenario.mass.config.get(f"players/{player_id}/values") or {}
    assert CONF_PLAYER_TURN_OFF_ON_LEAVE not in player_values

    await scenario.mass.players.unregister(zone2.protocol_id)
    await scenario.settle()
    returned = await _register_zone(scenario, receiver, "zone2")

    assert _player(returned).player_id == player_id
    assert returned.get_config_value(CONF_PLAYER_TURN_OFF_ON_LEAVE) is True
