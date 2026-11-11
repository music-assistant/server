"""Tests for the Auracast broadcast player on a running server."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import ProtocolRole

from music_assistant.controllers.players.protocol_linking import PLAYER_ID_PREFIX
from music_assistant.providers.auracast import AuracastProvider
from music_assistant.providers.auracast.constants import (
    CONF_BAUDRATE,
    CONF_BROADCAST_NAME,
    CONF_MANUFACTURER_DATA,
    CONF_SERIAL_PORT,
)
from music_assistant.providers.auracast.player import CONF_STORED_BROADCAST_ID, AuracastPlayer
from tests.scenarios.harness import Scenario

if TYPE_CHECKING:
    import pathlib
    from collections.abc import AsyncGenerator, Iterator


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


@pytest.fixture(autouse=True)
def no_controller() -> Iterator[MagicMock]:
    """Keep the broadcast away from a Bluetooth controller; return its start mock."""
    with (
        patch.object(AuracastPlayer, "start_broadcast", AsyncMock()) as start,
        patch.object(AuracastPlayer, "_release_broadcast", AsyncMock()),
        patch.object(AuracastPlayer, "poll", AsyncMock()),
    ):
        yield start


async def _provider(scenario: Scenario) -> AuracastProvider:
    """Load the provider with its real implementation on a configured controller."""
    fake: Any = scenario.mass.get_provider("auracast") or await scenario.add_provider("auracast")
    provider = AuracastProvider(scenario.mass, fake.manifest, fake.config, set())
    values = {
        CONF_SERIAL_PORT: "/dev/ttyAURACAST",
        CONF_BAUDRATE: 1_000_000,
        CONF_BROADCAST_NAME: "Broadcast A",
        CONF_MANUFACTURER_DATA: "",
    }
    provider.config.get_value = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda key, default=None: values.get(key, default)
    )
    provider.available = True
    scenario.mass._providers[provider.instance_id] = provider
    return provider


async def _discover(scenario: Scenario, provider: AuracastProvider) -> AuracastPlayer:
    """Run the discovery of the provider and return the attached broadcast player."""
    await provider.discover_players()
    await scenario.settle()
    protocol = provider.get_protocol(f"auracast_{provider.instance_id}")
    assert isinstance(protocol, AuracastPlayer)
    return protocol


async def test_the_broadcast_is_the_virtual_protocol_of_a_minted_player(
    scenario: Scenario, no_controller: AsyncMock
) -> None:
    """The broadcast gets a player of its own, as a virtual protocol, and starts."""
    provider = await _provider(scenario)

    protocol = await _discover(scenario, provider)

    assert protocol.player is not None
    assert protocol.player.player_id.startswith(PLAYER_ID_PREFIX)
    assert protocol.player.get_role(protocol.protocol_id) == ProtocolRole.VIRTUAL
    no_controller.assert_awaited_once()


async def test_discovery_leaves_the_attached_broadcast_alone(
    scenario: Scenario, no_controller: AsyncMock
) -> None:
    """A discovery while the broadcast is attached neither replaces nor restarts it."""
    provider = await _provider(scenario)
    protocol = await _discover(scenario, provider)

    assert await _discover(scenario, provider) is protocol
    no_controller.assert_awaited_once()


async def test_the_broadcast_keeps_its_identity_on_its_own_record(scenario: Scenario) -> None:
    """The broadcast id is stored on the protocol, and the same player returns after a restart."""
    protocol = await _discover(scenario, await _provider(scenario))
    assert protocol.player is not None
    player_id = protocol.player.player_id
    broadcast_id = protocol._broadcaster.config.broadcast_id
    protocol_id = protocol.protocol_id

    await scenario.restart()
    protocol = await _discover(scenario, await _provider(scenario))

    assert protocol.player is not None
    assert protocol.player.player_id == player_id
    assert protocol._broadcaster.config.broadcast_id == broadcast_id
    assert scenario.mass.config.get_protocol_value(protocol_id, CONF_STORED_BROADCAST_ID) == (
        broadcast_id
    )
    assert scenario.mass.config.get_player_value(player_id, CONF_STORED_BROADCAST_ID) is None


async def test_a_disabled_broadcast_is_not_built_and_returns_when_enabled(
    scenario: Scenario,
) -> None:
    """Disabling detaches the broadcast; enabling attaches it to the same player again."""
    provider = await _provider(scenario)
    protocol = await _discover(scenario, provider)
    assert protocol.player is not None
    player_id = protocol.player.player_id

    await scenario.disable(player_id)
    with patch("music_assistant.providers.auracast.AuracastPlayer") as player_cls:
        await provider.discover_players()
    player_cls.assert_not_called()
    await scenario.enable(player_id)
    await scenario.advance(5)

    returned = await _discover(scenario, provider)
    assert returned.player is not None
    assert returned.player.player_id == player_id
