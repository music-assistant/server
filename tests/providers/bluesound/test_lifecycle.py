"""Tests for BluOS players and the zones of one chassis attached on a running server."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import IdentifierType, ProtocolRole
from zeroconf import ServiceStateChange

from music_assistant.constants import CONF_ENABLE_ICY_METADATA, MAIN_ZONE
from music_assistant.controllers.players.protocol_linking import PLAYER_ID_PREFIX
from music_assistant.providers.bluesound.player import BluesoundPlayer
from music_assistant.providers.bluesound.provider import BluesoundPlayerProvider
from tests.scenarios.harness import Scenario

if TYPE_CHECKING:
    import pathlib
    from collections.abc import AsyncGenerator

    from music_assistant.providers.bluesound.provider import BluesoundDiscoveryInfo

MAC_ADDRESS = "00:11:22:aa:bb:cc"
IP_ADDRESS = "192.168.1.61"
MAIN_ZONE_ID = MAC_ADDRESS
SECOND_ZONE_ID = f"{MAC_ADDRESS}:11010"
MODULE = "music_assistant.providers.bluesound.provider"


@pytest.fixture
async def scenario(tmp_path: pathlib.Path) -> AsyncGenerator[Scenario]:
    """
    Boot a real server with fake player providers.

    :param tmp_path: Temporary directory for the server's storage.
    """
    scenario = Scenario(tmp_path)
    try:
        await scenario.start()
        yield scenario
    finally:
        await scenario.close()


async def _register_zone(scenario: Scenario, protocol_id: str, name: str) -> BluesoundPlayer:
    """Register a zone of the chassis the way discovery sets it up, without contacting it."""
    provider: Any = scenario.mass.get_provider("bluesound") or await scenario.add_provider(
        "bluesound"
    )
    # a secondary zone has the port of its zone in its id, the main zone the default port
    port = 11000 if protocol_id == MAIN_ZONE_ID else int(protocol_id.rsplit(":", 1)[1])
    discovery_info: BluesoundDiscoveryInfo = {
        "_objectType": "",
        "ip_address": IP_ADDRESS,
        "port": str(port),
        "mac": MAC_ADDRESS,
        "model": "NODE",
    }
    protocol = BluesoundPlayer(provider, protocol_id, discovery_info, name, IP_ADDRESS, port)
    protocol.sync_status = MagicMock(leader=None, followers=[])
    protocol._attr_needs_poll = False
    await scenario.mass.players.register(protocol)
    await scenario.settle()
    return protocol


def _player_id(protocol: BluesoundPlayer) -> str:
    """Return the id of the player a zone is attached to."""
    assert protocol.player is not None
    return protocol.player.player_id


async def test_a_new_device_is_the_native_protocol_of_a_minted_player(
    scenario: Scenario,
) -> None:
    """A device first seen now gets a player of its own, identified by its MAC, as main zone."""
    protocol = await _register_zone(scenario, MAIN_ZONE_ID, "Speaker A")

    assert _player_id(protocol).startswith(PLAYER_ID_PREFIX)
    assert protocol.player is not None
    assert protocol.player.get_role(MAIN_ZONE_ID) == ProtocolRole.NATIVE
    assert (IdentifierType.MAC_ADDRESS, MAC_ADDRESS.upper()) in protocol.identifiers
    assert (IdentifierType.ZONE, MAIN_ZONE) in protocol.identifiers


async def test_two_zones_of_one_chassis_are_two_players(scenario: Scenario) -> None:
    """The zones share the MAC and IP of their chassis and still each get a player."""
    main_zone = await _register_zone(scenario, MAIN_ZONE_ID, "Speaker A")
    second_zone = await _register_zone(scenario, SECOND_ZONE_ID, "Speaker B")

    assert _player_id(second_zone) != _player_id(main_zone)
    assert second_zone.player is not None
    assert set(second_zone.player.protocols) == {SECOND_ZONE_ID}
    assert (IdentifierType.ZONE, SECOND_ZONE_ID) in second_zone.identifiers


async def test_the_airplay_receiver_of_the_chassis_lands_on_the_main_zone(
    scenario: Scenario,
) -> None:
    """An AirPlay receiver sharing the chassis MAC and IP is an output of the main zone only."""
    second_zone = await _register_zone(scenario, SECOND_ZONE_ID, "Speaker B")
    airplay = (await scenario.add_provider("airplay")).protocol(
        "airplay_chassis",
        "Speaker A",
        role="audio",
        identifiers={
            IdentifierType.MAC_ADDRESS: MAC_ADDRESS,
            IdentifierType.IP_ADDRESS: IP_ADDRESS,
        },
    )
    await scenario.register(airplay)
    main_zone = await _register_zone(scenario, MAIN_ZONE_ID, "Speaker A")
    await scenario.idle()

    assert scenario.player_of(airplay) == _player_id(main_zone)
    assert second_zone.player is not None
    assert set(second_zone.player.protocols) == {SECOND_ZONE_ID}


async def test_a_new_zone_does_not_take_the_record_of_a_zone_that_is_away(
    scenario: Scenario,
) -> None:
    """A zone first seen while another zone of its chassis is not back yet gets its own player."""
    main_zone = await _register_zone(scenario, MAIN_ZONE_ID, "Speaker A")
    main_player_id = _player_id(main_zone)
    await scenario.restart()

    second_zone = await _register_zone(scenario, SECOND_ZONE_ID, "Speaker B")

    assert _player_id(second_zone) != main_player_id
    assert scenario.mass.config.get_protocol_ids_of_player(main_player_id) == [MAIN_ZONE_ID]
    main_zone = await _register_zone(scenario, MAIN_ZONE_ID, "Speaker A")
    assert _player_id(main_zone) == main_player_id


async def test_each_zone_finds_its_own_player_after_a_restart(scenario: Scenario) -> None:
    """After a restart a zone is the same player again, whichever zone is found first."""
    main_zone = await _register_zone(scenario, MAIN_ZONE_ID, "Speaker A")
    second_zone = await _register_zone(scenario, SECOND_ZONE_ID, "Speaker B")
    player_ids = {MAIN_ZONE_ID: _player_id(main_zone), SECOND_ZONE_ID: _player_id(second_zone)}
    await scenario.restart()

    second_zone = await _register_zone(scenario, SECOND_ZONE_ID, "Speaker B")
    main_zone = await _register_zone(scenario, MAIN_ZONE_ID, "Speaker A")

    assert {
        MAIN_ZONE_ID: _player_id(main_zone),
        SECOND_ZONE_ID: _player_id(second_zone),
    } == player_ids


async def test_a_new_zone_keeps_its_settings_on_its_own_record(scenario: Scenario) -> None:
    """A setting of a zone with a minted player id is stored on its protocol and survives."""
    protocol = await _register_zone(scenario, SECOND_ZONE_ID, "Speaker B")
    player_id = _player_id(protocol)
    assert player_id != SECOND_ZONE_ID

    await scenario.mass.config.save_protocol_config(
        SECOND_ZONE_ID, {CONF_ENABLE_ICY_METADATA: "basic"}
    )
    await scenario.settle()

    assert protocol.get_config_value(CONF_ENABLE_ICY_METADATA) == "basic"
    assert scenario.mass.config.get_player_value(player_id, CONF_ENABLE_ICY_METADATA) is None
    await scenario.restart()
    protocol = await _register_zone(scenario, SECOND_ZONE_ID, "Speaker B")
    assert _player_id(protocol) == player_id
    assert protocol.get_config_value(CONF_ENABLE_ICY_METADATA) == "basic"


async def test_discovery_builds_nothing_for_a_device_the_server_does_not_want() -> None:
    """A disabled device is neither built nor contacted when discovery finds it."""
    provider = BluesoundPlayerProvider.__new__(BluesoundPlayerProvider)
    provider.mass = MagicMock()
    provider.mass.players.get_protocol.return_value = None
    provider.mass.players.wants_protocol.return_value = False
    provider.logger = MagicMock()
    info = MagicMock(type="_musc._tcp.local.", decoded_properties={"mac": MAC_ADDRESS})

    with (
        patch(f"{MODULE}.get_primary_ip_address_from_zeroconf", return_value=IP_ADDRESS),
        patch(f"{MODULE}.get_port_from_zeroconf", return_value=11000),
        patch(f"{MODULE}.BluesoundPlayer") as player_cls,
    ):
        player_cls.return_value.setup = AsyncMock()
        await provider.on_mdns_service_state_change(
            "Speaker A._musc._tcp.local.", ServiceStateChange.Added, info
        )

    provider.mass.players.wants_protocol.assert_called_once_with(MAC_ADDRESS)
    player_cls.assert_not_called()
