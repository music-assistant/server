"""Tests for the config of a player and its protocol being created when a protocol attaches."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.enums import IdentifierType, PlayerType, ProtocolRole

from music_assistant.constants import CONF_PLAYER_PROTOCOLS, CONF_PLAYERS

if TYPE_CHECKING:
    from tests.scenarios.harness import Scenario

KITCHEN = {IdentifierType.MAC_ADDRESS: "54:78:C9:E6:0D:B1"}


async def test_constructing_a_protocol_does_not_create_its_config(scenario: Scenario) -> None:
    """A protocol object that is never attached leaves no config behind."""
    sonos = await scenario.add_provider("sonos")

    speaker = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)

    assert scenario.mass.config.get(f"{CONF_PLAYER_PROTOCOLS}/RINCON_KITCHEN") is None
    assert scenario.mass.config.get(CONF_PLAYERS, {}) == {}
    assert speaker.config.player_id == "RINCON_KITCHEN"


async def test_attaching_a_new_device_creates_the_config_of_its_player_and_protocol(
    scenario: Scenario,
) -> None:
    """Attach stores both configs with the values the protocol reports by then."""
    sonos = await scenario.add_provider("sonos")
    # not the default type, so a stored type can only come from what the protocol reports
    speaker = sonos.protocol(
        "RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN, type=PlayerType.STEREO_PAIR
    )

    await scenario.register(speaker)

    player_id = scenario.single_player(speaker).player_id
    player_record = scenario.mass.config.get(f"{CONF_PLAYERS}/{player_id}")
    assert player_record["provider"] == sonos.instance_id
    assert player_record["enabled"] is True
    assert player_record["default_name"] == "Kitchen"
    assert player_record["name"] is None
    assert player_record["player_type"] == PlayerType.STEREO_PAIR
    protocol_record = scenario.mass.config.get_protocol_record("RINCON_KITCHEN")
    assert protocol_record is not None
    assert protocol_record["player_id"] == player_id
    assert protocol_record["provider"] == sonos.instance_id
    assert protocol_record["role"] == ProtocolRole.NATIVE
    assert protocol_record["enabled"] is True
    assert protocol_record["default_name"] == "Kitchen"
    state = scenario.player(player_id)
    assert state is not None
    assert state.enabled is True
