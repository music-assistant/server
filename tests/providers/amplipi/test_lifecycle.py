"""Tests for the zones of an AmpliPi controller on a running server."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import pytest
from music_assistant_models.enums import ProtocolRole

from music_assistant.constants import CONF_OUTPUT_CODEC
from music_assistant.controllers.players.protocol_linking import PLAYER_ID_PREFIX
from music_assistant.providers.amplipi.constants import SOURCE_DISCONNECTED
from music_assistant.providers.amplipi.player import AmpliPiZonePlayer, zone_protocol_id
from music_assistant.providers.amplipi.provider import AmpliPiPlayerProvider
from tests.scenarios.harness import Scenario

if TYPE_CHECKING:
    import pathlib
    from collections.abc import AsyncGenerator


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


def _status(*zone_ids: int) -> SimpleNamespace:
    """Return the status the controller reports, with the given zones."""
    zones = [
        SimpleNamespace(
            id=zone_id,
            name=f"Zone {zone_id}",
            source_id=SOURCE_DISCONNECTED,
            disabled=False,
            mute=False,
            vol=-30,
            vol_f=0.5,
        )
        for zone_id in zone_ids
    ]
    return SimpleNamespace(zones=zones, sources=[])


async def _provider(scenario: Scenario, *zone_ids: int) -> AmpliPiPlayerProvider:
    """Load the provider with its real implementation on a controller with the given zones."""
    fake: Any = scenario.mass.get_provider("amplipi") or await scenario.add_provider("amplipi")
    provider = AmpliPiPlayerProvider(scenario.mass, fake.manifest, fake.config, set())
    provider.api = AsyncMock()
    provider._players = {}
    provider._ma_streams = {}
    provider._streams = []
    provider._stream_locks = {}
    provider._status = _status(*zone_ids)
    provider.available = True
    scenario.mass._providers[provider.instance_id] = provider
    return provider


def _zone(provider: AmpliPiPlayerProvider, zone_id: int) -> AmpliPiZonePlayer | None:
    """Return the attached protocol of a zone."""
    protocol = provider.get_protocol(zone_protocol_id(provider.instance_id, zone_id))
    return protocol if isinstance(protocol, AmpliPiZonePlayer) else None


def _player_id(provider: AmpliPiPlayerProvider, zone_id: int) -> str:
    """Return the id of the player of an attached zone."""
    protocol = _zone(provider, zone_id)
    assert protocol is not None
    assert protocol.player is not None
    return protocol.player.player_id


async def test_each_zone_is_the_native_protocol_of_a_player_of_its_own(
    scenario: Scenario,
) -> None:
    """The zones of one controller are separate players, each with a minted id."""
    provider = await _provider(scenario, 0, 1)

    await provider.discover_players()
    await scenario.settle()

    player_ids = {_player_id(provider, 0), _player_id(provider, 1)}
    assert len(player_ids) == 2
    assert all(player_id.startswith(PLAYER_ID_PREFIX) for player_id in player_ids)
    protocol = _zone(provider, 0)
    assert protocol is not None
    assert protocol.player is not None
    assert protocol.player.get_role(protocol.protocol_id) == ProtocolRole.NATIVE


async def test_a_new_zone_does_not_land_on_a_zone_that_is_away(scenario: Scenario) -> None:
    """A zone first seen while another zone of the controller is not back gets its own player."""
    provider = await _provider(scenario, 0)
    await provider.discover_players()
    await scenario.settle()
    first_player_id = _player_id(provider, 0)
    await scenario.restart()

    provider = await _provider(scenario, 1, 0)
    await provider.discover_players()
    await scenario.settle()

    assert _player_id(provider, 1) != first_player_id
    assert _player_id(provider, 0) == first_player_id


async def test_each_zone_finds_its_own_player_after_a_restart(scenario: Scenario) -> None:
    """After a restart a zone is the same player again, whichever zone is found first."""
    provider = await _provider(scenario, 0, 1)
    await provider.discover_players()
    await scenario.settle()
    player_ids = {zone_id: _player_id(provider, zone_id) for zone_id in (0, 1)}
    await scenario.restart()

    provider = await _provider(scenario, 1, 0)
    await provider.discover_players()
    await scenario.settle()

    assert {zone_id: _player_id(provider, zone_id) for zone_id in (0, 1)} == player_ids


async def test_a_re_enabled_zone_comes_back(scenario: Scenario) -> None:
    """Enabling a disabled zone attaches the protocol the provider kept for it again."""
    provider = await _provider(scenario, 0)
    await provider.discover_players()
    await scenario.settle()
    protocol = _zone(provider, 0)
    player_id = _player_id(provider, 0)

    await scenario.disable(player_id)
    await provider.discover_players()
    assert _zone(provider, 0) is None
    await scenario.enable(player_id)
    await scenario.advance(5)

    assert _zone(provider, 0) is protocol
    assert _player_id(provider, 0) == player_id


async def test_a_removed_disabled_zone_comes_back_as_a_new_player(scenario: Scenario) -> None:
    """Removing the player of a disabled zone reaches the provider, which offers the zone anew."""
    provider = await _provider(scenario, 0)
    await provider.discover_players()
    await scenario.settle()
    protocol = _zone(provider, 0)
    player_id = _player_id(provider, 0)
    await scenario.disable(player_id)

    await scenario.remove(player_id)
    await scenario.advance(5)

    returned = _zone(provider, 0)
    assert returned is not None
    assert returned is not protocol
    assert _player_id(provider, 0) != player_id


async def test_a_new_zone_keeps_its_settings_on_its_own_record(scenario: Scenario) -> None:
    """A setting of a zone with a minted player id is stored on its protocol and survives."""
    provider = await _provider(scenario, 0)
    await provider.discover_players()
    await scenario.settle()
    protocol = _zone(provider, 0)
    assert protocol is not None
    player_id = _player_id(provider, 0)

    await scenario.mass.config.save_protocol_config(
        protocol.protocol_id, {CONF_OUTPUT_CODEC: "mp3"}
    )
    await scenario.mass.players.unregister(protocol.protocol_id)
    await provider.discover_players()
    await scenario.settle()

    assert _zone(provider, 0) is protocol
    assert protocol.get_config_value(CONF_OUTPUT_CODEC) == "mp3"
    assert scenario.mass.config.get_player_value(player_id, CONF_OUTPUT_CODEC) is None
