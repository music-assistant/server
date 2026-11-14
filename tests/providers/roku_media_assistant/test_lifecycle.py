"""Tests for Rokus found by the Media Assistant (Roku) provider on a running server."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import IdentifierType, ProtocolRole
from rokuecp.models import Info

from music_assistant.constants import (
    CONF_ENTRY_MANUAL_DISCOVERY_IPS,
    CONF_FLOW_MODE,
    CONF_PLAYERS,
)
from music_assistant.controllers.players.protocol_linking import PLAYER_ID_PREFIX
from music_assistant.providers.roku_media_assistant.constants import (
    CONF_AUTO_DISCOVER,
    CONF_ROKU_APP_ID,
)
from music_assistant.providers.roku_media_assistant.provider import MediaAssistantprovider
from tests.scenarios.harness import Scenario

if TYPE_CHECKING:
    import pathlib
    from collections.abc import AsyncGenerator

    from music_assistant.providers.roku_media_assistant.player import MediaAssistantPlayer

IP_ADDRESS = "192.168.1.95"
PROTOCOL_ID = "ROKU_X00000000001"
MODULE = "music_assistant.providers.roku_media_assistant.provider"


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


@pytest.fixture
def roku() -> MagicMock:
    """Return the Roku at the manual address, as rokuecp reports it, without a network."""
    device = MagicMock()
    # the real model: what the provider copies from it ends up in the stored config
    device.info = Info(
        name="Roku A",
        brand="Roku",
        device_type="box",
        device_location=None,
        model_name="Roku Ultra",
        model_number="4800X",
        serial_number=PROTOCOL_ID.removeprefix("ROKU_"),
        version="11.5.0",
        ethernet_mac="AA:BB:CC:00:00:01",
    )
    device.app = None
    client = MagicMock()
    client.update = AsyncMock(return_value=device)
    client.close_session = AsyncMock()
    return client


async def _roku_provider(scenario: Scenario) -> MediaAssistantprovider:
    """Load the provider with its real implementation and one Roku at a manual address."""
    fake: Any = await scenario.add_provider("roku_media_assistant")
    provider = MediaAssistantprovider(scenario.mass, fake.manifest, fake.config, set())
    values = {
        CONF_ENTRY_MANUAL_DISCOVERY_IPS.key: [IP_ADDRESS],
        CONF_ROKU_APP_ID: "782875",
        CONF_AUTO_DISCOVER: True,
    }
    provider.config.get_value = MagicMock(side_effect=values.get)  # type: ignore[method-assign]
    # the class keeps its Rokus in a class variable: give this instance its own
    provider.roku_players = {}  # type: ignore[misc]
    await provider.handle_async_init()
    provider.available = True
    scenario.mass._providers[provider.instance_id] = provider
    return provider


def _attached(provider: MediaAssistantprovider) -> MediaAssistantPlayer | None:
    """Return the attached protocol of the Roku."""
    protocol = provider.get_protocol(PROTOCOL_ID)
    return protocol if protocol is provider.roku_players.get(PROTOCOL_ID) else None


async def test_a_roku_at_a_manual_address_is_the_native_protocol_of_a_minted_player(
    scenario: Scenario, roku: MagicMock
) -> None:
    """A Roku first seen now gets a player of its own, identified by its serial and MAC."""
    provider = await _roku_provider(scenario)

    with patch(f"{MODULE}.Roku", return_value=roku):
        await provider.discover_players()
        await scenario.settle()

    protocol = _attached(provider)
    assert protocol is not None
    assert protocol.player is not None
    assert protocol.player.player_id.startswith(PLAYER_ID_PREFIX)
    assert protocol.player.get_role(PROTOCOL_ID) == ProtocolRole.NATIVE
    assert (IdentifierType.SERIAL_NUMBER, "X00000000001") in protocol.identifiers
    assert (IdentifierType.MAC_ADDRESS, "AA:BB:CC:00:00:01") in protocol.identifiers
    stored = scenario.mass.config.get(
        f"{CONF_PLAYERS}/{protocol.player.player_id}/values/device_info"
    )
    assert stored == {"model": "Roku Ultra", "manufacturer": "Roku", "model_id": "4800X"}


async def test_discovery_leaves_an_attached_roku_alone(scenario: Scenario, roku: MagicMock) -> None:
    """A discovery while the Roku is attached does not contact it."""
    provider = await _roku_provider(scenario)
    with patch(f"{MODULE}.Roku", return_value=roku):
        await provider.discover_players()
        await scenario.settle()
    protocol = _attached(provider)
    assert protocol is not None
    protocol._attr_available = True
    roku.update.reset_mock()

    with patch(f"{MODULE}.Roku", return_value=roku) as roku_cls:
        await provider.discover_players()
        await scenario.settle()

    roku_cls.assert_not_called()
    roku.update.assert_not_awaited()
    assert _attached(provider) is protocol


async def test_a_removed_roku_at_a_manual_address_comes_back_as_a_new_player(
    scenario: Scenario, roku: MagicMock
) -> None:
    """The removal forgets the Roku, and the discovery after it finds it again."""
    provider = await _roku_provider(scenario)
    with patch(f"{MODULE}.Roku", return_value=roku):
        await provider.discover_players()
        await scenario.settle()
        protocol = _attached(provider)
        assert protocol is not None
        assert protocol.player is not None
        player_id = protocol.player.player_id

        await scenario.remove(player_id)
        assert PROTOCOL_ID not in provider.roku_players
        await scenario.advance(5)

    returned = _attached(provider)
    assert returned is not None
    assert returned is not protocol
    assert returned.player is not None
    assert returned.player.player_id != player_id


async def test_a_re_enabled_roku_comes_back(scenario: Scenario, roku: MagicMock) -> None:
    """Enabling a disabled Roku attaches the protocol the provider kept for it again."""
    provider = await _roku_provider(scenario)
    with patch(f"{MODULE}.Roku", return_value=roku):
        await provider.discover_players()
        await scenario.settle()
        protocol = _attached(provider)
        assert protocol is not None
        assert protocol.player is not None
        player_id = protocol.player.player_id
        # the provider polled it before the user disabled it
        protocol._attr_available = True

        await scenario.disable(player_id)
        assert _attached(provider) is None
        await scenario.enable(player_id)
        await scenario.advance(5)

    assert _attached(provider) is protocol
    assert protocol.player is not None
    assert protocol.player.player_id == player_id


async def test_a_disabled_roku_is_not_set_up(scenario: Scenario, roku: MagicMock) -> None:
    """A discovery that finds a Roku whose player is disabled builds no protocol for it."""
    provider = await _roku_provider(scenario)
    with patch(f"{MODULE}.Roku", return_value=roku):
        await provider.discover_players()
        await scenario.settle()
        protocol = _attached(provider)
        assert protocol is not None
        assert protocol.player is not None
        await provider.forget_player(protocol)
        await scenario.disable(protocol.player.player_id)

        await provider.discover_players()
        await scenario.settle()

    assert PROTOCOL_ID not in provider.roku_players


async def test_a_new_roku_keeps_its_settings_on_its_own_record(
    scenario: Scenario, roku: MagicMock
) -> None:
    """A setting of a Roku with a minted player id is stored on its protocol and survives."""
    provider = await _roku_provider(scenario)
    with patch(f"{MODULE}.Roku", return_value=roku):
        await provider.discover_players()
        await scenario.settle()
        protocol = _attached(provider)
        assert protocol is not None
        assert protocol.player is not None
        player_id = protocol.player.player_id
        flow_mode_before = protocol.flow_mode

        await scenario.mass.config.save_protocol_config(PROTOCOL_ID, {CONF_FLOW_MODE: True})
        await scenario.mass.players.unregister(PROTOCOL_ID)
        await scenario.mass.players.register(protocol)
        await scenario.settle()

    assert (flow_mode_before, protocol.flow_mode) == (False, True)
    assert scenario.mass.config.get_player_value(player_id, CONF_FLOW_MODE) is None
