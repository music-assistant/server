"""Tests for the Echo devices of the Alexa provider on a running server."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import pytest
from music_assistant_models.enums import IdentifierType, MediaType, ProtocolRole

from music_assistant.constants import CONF_OUTPUT_CODEC
from music_assistant.controllers.players.protocol_linking import PLAYER_ID_PREFIX
from music_assistant.models.player import PlayerMedia
from music_assistant.providers.alexa import AlexaDevice, AlexaPlayer, AlexaProvider
from tests.scenarios.harness import Scenario

if TYPE_CHECKING:
    import pathlib
    from collections.abc import AsyncGenerator

PROTOCOL_ID = "Echo A"


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


async def _provider(scenario: Scenario) -> AlexaProvider:
    """Load the provider with its real implementation and one Echo on the account."""
    fake: Any = await scenario.add_provider("alexa")
    provider = AlexaProvider(scenario.mass, fake.manifest, fake.config, set())
    await provider.handle_async_init()
    device = AlexaDevice()
    device.device_serial_number = "G0000000000000A1"
    provider.devices = {PROTOCOL_ID: device}
    # the volume poll reads the account, which is not signed in here
    provider.get_volumes = AsyncMock(return_value={})  # type: ignore[method-assign]
    provider.available = True
    scenario.mass._providers[provider.instance_id] = provider
    return provider


def _attached(provider: AlexaProvider) -> AlexaPlayer | None:
    """Return the attached protocol of the Echo."""
    protocol = provider.get_protocol(PROTOCOL_ID)
    return protocol if isinstance(protocol, AlexaPlayer) else None


async def _discover(scenario: Scenario, provider: AlexaProvider) -> AlexaPlayer:
    """Run the discovery of the provider and return the protocol of the Echo."""
    await provider.discover_players()
    await scenario.settle()
    protocol = _attached(provider)
    assert protocol is not None
    return protocol


async def test_an_echo_is_the_native_protocol_of_a_minted_player(scenario: Scenario) -> None:
    """An Echo first seen now gets a player of its own, identified by its serial number."""
    provider = await _provider(scenario)

    protocol = await _discover(scenario, provider)

    assert protocol.player is not None
    assert protocol.player.player_id.startswith(PLAYER_ID_PREFIX)
    assert protocol.player.get_role(PROTOCOL_ID) == ProtocolRole.NATIVE
    assert (IdentifierType.SERIAL_NUMBER, "G0000000000000A1") in protocol.identifiers


async def test_discovery_leaves_an_attached_echo_alone(scenario: Scenario) -> None:
    """A discovery while the Echo is attached keeps its protocol."""
    provider = await _provider(scenario)
    protocol = await _discover(scenario, provider)

    assert await _discover(scenario, provider) is protocol


async def test_a_removed_echo_comes_back_as_a_new_player(scenario: Scenario) -> None:
    """The discovery after a removal offers the Echo of the account again."""
    provider = await _provider(scenario)
    protocol = await _discover(scenario, provider)
    assert protocol.player is not None
    player_id = protocol.player.player_id

    await scenario.remove(player_id)
    await scenario.advance(5)

    returned = _attached(provider)
    assert returned is not None
    assert returned is not protocol
    assert returned.player is not None
    assert returned.player.player_id != player_id


async def test_a_re_enabled_echo_comes_back(scenario: Scenario) -> None:
    """Enabling a disabled Echo attaches it to its player again."""
    provider = await _provider(scenario)
    protocol = await _discover(scenario, provider)
    assert protocol.player is not None
    player_id = protocol.player.player_id

    await scenario.disable(player_id)
    assert _attached(provider) is None
    with patch("music_assistant.providers.alexa.AlexaPlayer") as player_cls:
        await provider.discover_players()
    # nothing is built for a device the server does not want
    player_cls.assert_not_called()
    await scenario.enable(player_id)
    await scenario.advance(5)

    returned = _attached(provider)
    assert returned is not None
    assert returned.player is not None
    assert returned.player.player_id == player_id


async def test_a_new_echo_streams_in_the_codec_of_its_own_record(scenario: Scenario) -> None:
    """The output codec of an Echo with a minted player id is read from its protocol."""
    provider = await _provider(scenario)
    protocol = await _discover(scenario, provider)
    assert protocol.player is not None
    player_id = protocol.player.player_id
    await scenario.mass.config.save_protocol_config(PROTOCOL_ID, {CONF_OUTPUT_CODEC: "mp3"})
    await scenario.mass.players.unregister(PROTOCOL_ID)
    await scenario.mass.players.register(protocol)
    await scenario.settle()
    media = PlayerMedia(
        uri="source://line_in",
        media_type=MediaType.AUDIO_SOURCE,
        source_id="line_in",
        queue_session_id="session_a",
    )

    stream_url = await scenario.mass.streams.resolve_stream_url(PROTOCOL_ID, media)

    assert stream_url.endswith(".mp3")
    assert scenario.mass.config.get_player_value(player_id, CONF_OUTPUT_CODEC) is None
