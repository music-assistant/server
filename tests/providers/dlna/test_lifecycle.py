"""Tests for a DLNA renderer attached to its player on a running server."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from music_assistant_models.enums import IdentifierType, PlayerType, ProtocolRole
from music_assistant_models.player import DeviceInfo

from music_assistant.controllers.players.protocol_linking import PLAYER_ID_PREFIX
from music_assistant.providers.dlna.player import DLNAPlayer
from tests.scenarios.harness import Scenario

if TYPE_CHECKING:
    import pathlib
    from collections.abc import AsyncGenerator

UDN = "uuid:dlna-renderer"
IP_ADDRESS = "192.168.1.41"
MAC_ADDRESS = "00:11:22:33:44:55"


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


async def _attach_renderer(scenario: Scenario, udn: str = UDN) -> DLNAPlayer:
    """
    Attach a DLNA renderer the way discovery sets it up.

    :param udn: The UDN of the renderer; every renderer of these tests is on one host.
    """
    provider: Any = scenario.mass.get_provider("dlna") or await scenario.add_provider("dlna")
    scenario.arp[IP_ADDRESS] = MAC_ADDRESS
    device = MagicMock()
    device.name = "Renderer A"
    device.manufacturer = "Acme"
    device.has_play_media = True
    device.can_stop = True
    device.can_pause = True
    for command in ("async_stop", "async_pause", "async_update", "async_unsubscribe_services"):
        setattr(device, command, AsyncMock())
    protocol = DLNAPlayer(provider, udn, f"http://{IP_ADDRESS}/description.xml", device=device)
    protocol._attr_device_info = DeviceInfo(model="Renderer", manufacturer="Acme")
    protocol._attr_device_info.add_identifier(IdentifierType.UUID, udn.removeprefix("uuid:"))
    protocol._attr_device_info.add_identifier(IdentifierType.IP_ADDRESS, IP_ADDRESS)
    protocol._device_connect = AsyncMock()  # type: ignore[method-assign]
    assert await protocol.setup() is True
    await scenario.settle()
    return protocol


async def test_a_new_renderer_is_the_audio_protocol_of_a_minted_player(
    scenario: Scenario,
) -> None:
    """A renderer of a device nothing else knows becomes a player of its own, as its output."""
    protocol = await _attach_renderer(scenario)

    player = protocol.player
    assert player is not None
    assert player.player_id.startswith(PLAYER_ID_PREFIX)
    assert player.get_role(UDN) == ProtocolRole.AUDIO
    assert player.state.type == PlayerType.PLAYER


async def test_a_new_renderer_reads_its_settings_from_its_own_record(
    scenario: Scenario,
) -> None:
    """A setting of the renderer is read from its protocol record, not the player's."""
    protocol = await _attach_renderer(scenario)
    assert protocol.player is not None
    assert protocol.player.player_id != UDN

    await scenario.mass.config.save_protocol_config(UDN, {"replace_pause_with_stop": True})
    await scenario.settle()
    device = protocol.device
    assert isinstance(device, MagicMock)
    await protocol.pause()

    device.async_stop.assert_awaited_once()
    device.async_pause.assert_not_awaited()


async def test_two_renderers_on_one_host_are_two_players(scenario: Scenario) -> None:
    """The UDN of a renderer vetoes a match with another renderer of the same host."""
    renderer_a = await _attach_renderer(scenario)
    assert renderer_a.player is not None
    player_a = renderer_a.player.player_id
    # while it is offline, its player has no DLNA protocol attached
    await scenario.mass.players.detach(UDN)
    await scenario.settle()

    renderer_b = await _attach_renderer(scenario, "uuid:dlna-renderer-b")

    assert renderer_b.player is not None
    assert renderer_b.player.player_id != player_a


async def test_the_renderer_of_a_cast_device_joins_its_player(scenario: Scenario) -> None:
    """A renderer joins the player of a Cast device on the same hardware address."""
    cast = await scenario.add_provider("chromecast")
    tv = cast.protocol(
        "cast-tv",
        "TV",
        identifiers={IdentifierType.MAC_ADDRESS: MAC_ADDRESS, IdentifierType.CAST_UUID: "c1"},
    )
    await scenario.attach(tv)

    renderer = await _attach_renderer(scenario)

    assert renderer.player is not None
    assert renderer.player.player_id == scenario.player_of(tv)
    assert renderer.player.get_role(UDN) == ProtocolRole.AUDIO
