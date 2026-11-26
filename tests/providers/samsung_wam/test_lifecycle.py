"""Tests for a Samsung WAM speaker attached on a running server, and for its discovery."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import IdentifierType, ProtocolRole

from music_assistant.constants import (
    CONF_ENTRY_MANUAL_DISCOVERY_IPS,
    CONF_REQUIRES_FIXED_CONTENT_LENGTH,
)
from music_assistant.controllers.players.protocol_linking import PLAYER_ID_PREFIX
from music_assistant.providers.samsung_wam.features.discovery.handler import DiscoveryHandler
from music_assistant.providers.samsung_wam.features.grouping.coordinator import (
    GroupingCoordinator,
)
from music_assistant.providers.samsung_wam.player import WamPlayer
from tests.scenarios.harness import Scenario

if TYPE_CHECKING:
    import pathlib
    from collections.abc import AsyncGenerator

MAC_ADDRESS = "AA:BB:CC:00:00:02"
IP_ADDRESS = "192.168.1.97"
UDN = "1b2c3d4e-0000-1000-8000-aabbcc000002"
MODULE = "music_assistant.providers.samsung_wam.features.discovery.handler"


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


def _speaker() -> MagicMock:
    """Return a connected pywam speaker, without a network."""
    speaker = MagicMock()
    speaker.is_connected = True
    speaker.disconnect = AsyncMock()
    return speaker


async def _register(scenario: Scenario, speaker: MagicMock) -> WamPlayer:
    """Register the protocol of a speaker the way discovery sets it up."""
    provider: Any = scenario.mass.get_provider("samsung_wam") or await scenario.add_provider(
        "samsung_wam"
    )
    provider.groups = GroupingCoordinator(provider)
    protocol = WamPlayer(provider, IP_ADDRESS, UDN, MAC_ADDRESS, speaker)
    protocol._attr_name = "Speaker A"
    protocol._attr_needs_poll = False
    await scenario.mass.players.register(protocol)
    await scenario.settle()
    return protocol


async def test_a_new_speaker_is_the_native_protocol_of_a_minted_player(
    scenario: Scenario,
) -> None:
    """A speaker first seen now gets a player of its own, identified by its MAC and UDN."""
    protocol = await _register(scenario, _speaker())

    assert protocol.player is not None
    assert protocol.player.player_id.startswith(PLAYER_ID_PREFIX)
    assert protocol.player.get_role(MAC_ADDRESS) == ProtocolRole.NATIVE
    assert (IdentifierType.MAC_ADDRESS, MAC_ADDRESS) in protocol.identifiers
    assert (IdentifierType.UUID, UDN) in protocol.identifiers


async def test_the_speaker_is_followed_while_attached(scenario: Scenario) -> None:
    """Its events and groups are followed while attached; a detach disconnects it."""
    speaker = _speaker()
    protocol = await _register(scenario, speaker)
    speaker.events.register_subscriber.assert_called_once()
    assert protocol.player is not None
    player_id = protocol.player.player_id

    await scenario.mass.players.unregister(MAC_ADDRESS)
    await scenario.settle()

    speaker.disconnect.assert_awaited()
    assert scenario.mass.players.get_protocol(MAC_ADDRESS) is None
    assert scenario.mass.players.get_player(player_id) is not None
    await scenario.mass.players.register(protocol)
    await scenario.settle()
    assert speaker.events.register_subscriber.call_count == 2


async def test_a_new_speaker_takes_the_name_of_its_player(scenario: Scenario) -> None:
    """A name the user gives the player of a speaker with a minted id is set on the speaker."""
    protocol = await _register(scenario, _speaker())
    assert protocol.player is not None
    protocol.device_config.set_name = AsyncMock()  # type: ignore[method-assign]

    await scenario.configure_player(protocol.player.player_id, name="Speaker B")

    protocol.device_config.set_name.assert_awaited_once_with("Speaker B")


async def test_a_new_speaker_keeps_its_settings_on_its_own_record(scenario: Scenario) -> None:
    """A setting of a speaker with a minted player id is stored on its protocol and survives."""
    protocol = await _register(scenario, _speaker())
    assert protocol.player is not None
    player_id = protocol.player.player_id

    # the speaker's entry defaults to a fixed content length
    await scenario.mass.config.save_protocol_config(
        MAC_ADDRESS, {CONF_REQUIRES_FIXED_CONTENT_LENGTH: False}
    )
    await scenario.mass.players.unregister(MAC_ADDRESS)
    await scenario.mass.players.register(protocol)
    await scenario.settle()

    assert protocol.get_config_value(CONF_REQUIRES_FIXED_CONTENT_LENGTH) is False
    assert (
        scenario.mass.config.get_player_value(player_id, CONF_REQUIRES_FIXED_CONTENT_LENGTH) is None
    )


async def test_a_speaker_the_server_does_not_want_is_disconnected() -> None:
    """A disabled speaker found at an address is not attached and its connection is closed."""
    provider = MagicMock()
    provider.mass.players.wants_protocol.return_value = False
    handler = DiscoveryHandler(provider)
    speaker = _speaker()
    speaker.connect = AsyncMock()
    speaker.update = AsyncMock()
    attrs = MagicMock(mac=MAC_ADDRESS)

    with (
        patch(f"{MODULE}.Speaker", return_value=speaker),
        patch(f"{MODULE}.StateSyncMapper.create_speaker_attributes", return_value=attrs),
        patch(f"{MODULE}.WamPlayer") as player_cls,
    ):
        await handler._setup_player(UDN, IP_ADDRESS)

    provider.mass.players.wants_protocol.assert_called_once_with(MAC_ADDRESS)
    player_cls.assert_not_called()
    speaker.disconnect.assert_awaited_once()


async def test_discovery_probes_the_manual_addresses_without_an_attached_speaker() -> None:
    """Only a manually added address that no attached speaker has is probed."""
    provider = MagicMock()
    provider.config.get_value.return_value = [IP_ADDRESS, "192.168.1.98"]
    provider.get_players.return_value = [MagicMock(ip_address=IP_ADDRESS)]
    handler = DiscoveryHandler(provider)
    handler.probe_ip = AsyncMock(return_value=None)  # type: ignore[method-assign]

    await handler.probe_manual_ips()

    provider.config.get_value.assert_called_once_with(CONF_ENTRY_MANUAL_DISCOVERY_IPS.key)
    handler.probe_ip.assert_awaited_once_with("192.168.1.98")
