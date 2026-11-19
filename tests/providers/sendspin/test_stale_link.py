"""Tests for a Sendspin client whose config names the player of a device it rode on before."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from music_assistant_models.enums import IdentifierType, ProtocolRole

from tests.providers.sendspin.test_bridge_reconnect import (
    BRIDGE_ID,
    _connect,
    _hello,
    _no_sendspin_entries,
    _sendspin,
)

if TYPE_CHECKING:
    from tests.scenarios.harness import Scenario

__all__ = ["_no_sendspin_entries"]

KITCHEN = {IdentifierType.MAC_ADDRESS: "54:78:C9:E6:0D:A0"}


async def _linked_client(scenario: Scenario) -> tuple[str, Any]:
    """
    Return the player of a device whose config lists the Sendspin client, and the provider.

    :param scenario: The running server.
    """
    sonos = await scenario.add_provider("sonos")
    await scenario.add_provider("sendspin")
    native = sonos.protocol("RINCON_KITCHEN", "Kitchen", identifiers=KITCHEN)
    await scenario.register(native)
    player_id = scenario.single_player(native).player_id
    sendspin = _sendspin(scenario)
    scenario.mass.config.create_protocol_record(
        BRIDGE_ID,
        provider=sendspin.instance_id,
        player_id=player_id,
        role=ProtocolRole.AUDIO,
        default_name="Kitchen",
    )
    player = scenario.mass.players.get_player(player_id)
    assert player is not None
    player.protocol_ids.add(BRIDGE_ID)
    return player_id, sendspin


def _player_of_client(scenario: Scenario) -> str | None:
    """Return the id of the player the client is attached to."""
    protocol = scenario.mass.players.get_protocol(BRIDGE_ID)
    return protocol.player.player_id if protocol and protocol.player else None


@pytest.mark.parametrize("stale", ["device_player_disabled", "listed_output_switched_off"])
async def test_web_client_is_a_player_of_its_own_whatever_the_player_of_the_device_says(
    scenario: Scenario, stale: str
) -> None:
    """A browser that connects is its own player, while the player its config names is not."""
    player_id, sendspin = await _linked_client(scenario)
    if stale == "device_player_disabled":
        await scenario.disable(player_id)
    else:
        scenario.mass.config.set_protocol_value(BRIDGE_ID, "enabled", False)

    await _connect(scenario, sendspin, _hello("Web Browser"))

    assert _player_of_client(scenario) == BRIDGE_ID
    protocol = scenario.mass.players.get_protocol(BRIDGE_ID)
    assert protocol is not None
    assert protocol.available_for_playback


@pytest.mark.parametrize("off", ["switched_off", "player_disabled"])
async def test_device_client_the_user_does_not_want_is_left_alone(
    scenario: Scenario, off: str
) -> None:
    """A client of the device itself stays off: nothing is set up for it, it gets no player."""
    player_id, sendspin = await _linked_client(scenario)
    if off == "switched_off":
        scenario.mass.config.set_protocol_value(BRIDGE_ID, "enabled", False)
    else:
        await scenario.disable(player_id)

    await _connect(scenario, sendspin, _hello("Speaker"))

    assert _player_of_client(scenario) is None
    sendspin._auto_trust_guest_access.assert_not_awaited()
    assert [player.player_id for player in scenario.visible_players()] == (
        [player_id] if off == "switched_off" else []
    )
