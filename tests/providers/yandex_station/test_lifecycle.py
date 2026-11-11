"""Tests for Yandex Stations attached on a running server."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import IdentifierType, ProtocolRole

from music_assistant.controllers.players.protocol_linking import PLAYER_ID_PREFIX
from music_assistant.providers.yandex_station.constants import CONF_VOICE_CONTROL
from music_assistant.providers.yandex_station.player import YandexStationPlayer
from music_assistant.providers.yandex_station.provider import YandexStationProvider
from tests.scenarios.harness import Scenario

if TYPE_CHECKING:
    import pathlib
    from collections.abc import AsyncGenerator

DEVICE_ID = "XK0000000000000001"
PROTOCOL_ID = f"ys_{DEVICE_ID}"
MODULE = "music_assistant.providers.yandex_station.provider"


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
def glagol() -> MagicMock:
    """Return the local connection to the Station, without a network."""
    glagol = MagicMock()
    glagol.start = AsyncMock()
    glagol.stop = AsyncMock()
    glagol.connected = False
    return glagol


def _speaker() -> dict[str, Any]:
    """Return a Station as the cloud discovery lists it."""
    return {
        "name": "Station A",
        "host": "192.168.1.99",
        "port": 1961,
        "quasar_info": {"device_id": DEVICE_ID, "platform": "yandexstation_2"},
    }


async def _provider(scenario: Scenario) -> YandexStationProvider:
    """Load the provider with its real implementation and a signed-in session."""
    fake: Any = await scenario.add_provider("yandex_station")
    provider = YandexStationProvider(scenario.mass, fake.manifest, fake.config, set())
    provider._session = MagicMock()
    provider._passport_client = MagicMock()
    provider.available = True
    scenario.mass._providers[provider.instance_id] = provider
    return provider


async def _attach(scenario: Scenario, glagol: MagicMock) -> YandexStationPlayer:
    """Attach a Station the way the cloud discovery sets it up."""
    provider = scenario.mass.get_provider("yandex_station")
    if not isinstance(provider, YandexStationProvider):
        provider = await _provider(scenario)
    with patch(f"{MODULE}.YandexGlagol", return_value=glagol):
        await provider._create_player(PROTOCOL_ID, _speaker())
        await scenario.settle()
    protocol = provider.get_protocol(PROTOCOL_ID)
    assert isinstance(protocol, YandexStationPlayer)
    return protocol


async def test_a_new_station_is_the_native_protocol_of_a_minted_player(
    scenario: Scenario, glagol: MagicMock
) -> None:
    """A Station first seen now gets a player of its own and is connected to."""
    protocol = await _attach(scenario, glagol)

    assert protocol.player is not None
    assert protocol.player.player_id.startswith(PLAYER_ID_PREFIX)
    assert protocol.player.get_role(PROTOCOL_ID) == ProtocolRole.NATIVE
    assert (IdentifierType.UUID, DEVICE_ID) in protocol.identifiers
    glagol.start.assert_awaited_once()


async def test_a_detached_station_is_disconnected(scenario: Scenario, glagol: MagicMock) -> None:
    """Detaching the protocol closes the local connection to the Station."""
    await _attach(scenario, glagol)

    await scenario.mass.players.detach(PROTOCOL_ID)
    await scenario.settle()

    glagol.stop.assert_awaited_once()


async def test_a_station_of_a_disabled_player_is_not_connected(
    scenario: Scenario, glagol: MagicMock
) -> None:
    """A Station whose player is disabled gets no protocol and no connection."""
    protocol = await _attach(scenario, glagol)
    assert protocol.player is not None
    await scenario.disable(protocol.player.player_id)
    provider: Any = protocol.provider
    replacement = MagicMock(start=AsyncMock(), stop=AsyncMock())

    with patch(f"{MODULE}.YandexGlagol", return_value=replacement) as glagol_cls:
        await provider._create_player(PROTOCOL_ID, _speaker())
        await scenario.settle()

    glagol_cls.assert_not_called()
    assert provider.get_protocol(PROTOCOL_ID) is None


async def test_a_removed_station_comes_back_from_the_cloud_discovery(
    scenario: Scenario, glagol: MagicMock
) -> None:
    """After a removal the Station the cloud listed is offered again, as a new player."""
    provider = await _provider(scenario)
    provider._discovery_done = True
    provider._known_speakers = {PROTOCOL_ID: _speaker()}
    protocol = await _attach(scenario, glagol)
    assert protocol.player is not None
    player_id = protocol.player.player_id

    with patch(f"{MODULE}.YandexGlagol", return_value=glagol):
        await scenario.remove(player_id)
        await scenario.advance(5)

    returned = provider.get_protocol(PROTOCOL_ID)
    assert returned is not None
    assert returned is not protocol
    assert returned.player is not None
    assert returned.player.player_id != player_id


async def test_the_say_command_reaches_the_station_of_a_minted_player(
    scenario: Scenario, glagol: MagicMock
) -> None:
    """The say API command addresses a Station by the id of its player."""
    protocol = await _attach(scenario, glagol)
    assert protocol.player is not None
    provider: Any = protocol.provider

    with patch.object(YandexStationPlayer, "say", AsyncMock()) as say:
        await provider.say(protocol.player.player_id, "Привет")

    say.assert_awaited_once_with("Привет")


async def test_a_new_station_keeps_its_settings_on_its_own_record(
    scenario: Scenario, glagol: MagicMock
) -> None:
    """A setting of a Station with a minted player id is stored on its protocol and survives."""
    protocol = await _attach(scenario, glagol)
    assert protocol.player is not None
    player_id = protocol.player.player_id
    voice_control_before = protocol._voice_control_enabled

    await scenario.mass.config.save_protocol_config(PROTOCOL_ID, {CONF_VOICE_CONTROL: True})
    await scenario.mass.players.detach(PROTOCOL_ID)
    await scenario.mass.players.attach(protocol)
    await scenario.settle()

    assert (voice_control_before, protocol._voice_control_enabled) == (False, True)
    assert scenario.mass.config.get_player_value(player_id, CONF_VOICE_CONTROL) is None
