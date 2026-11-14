"""Tests for the TVs of the MSX Bridge provider on a running server."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from music_assistant_models.enums import EventType, IdentifierType, PlaybackState

from music_assistant.constants import CONF_OUTPUT_CODEC
from music_assistant.controllers.players.protocol_linking import PLAYER_ID_PREFIX
from music_assistant.providers.msx_bridge.provider import MSXBridgeProvider
from tests.scenarios.harness import Scenario

if TYPE_CHECKING:
    import pathlib
    from collections.abc import AsyncGenerator

    from music_assistant.models.player import Player
    from music_assistant.providers.msx_bridge.player import MSXPlayer

TV = "msx_192_168_1_40"


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
async def provider(scenario: Scenario) -> MSXBridgeProvider:
    """
    Return a real provider loaded in the server, without its HTTP server.

    :param scenario: The running server.
    """
    fake = await scenario.add_provider("msx_bridge")
    provider = MSXBridgeProvider(scenario.mass, fake.manifest, fake.config)
    provider.available = True
    provider.initialized.set()
    scenario.mass._providers[fake.instance_id] = provider
    return provider


async def _request(scenario: Scenario, provider: MSXBridgeProvider) -> MSXPlayer:
    """Let the TV send a request, as it does when MSX opens a page; return its protocol."""
    protocol = await provider.get_or_register_player(TV, display_name="MSX TV")
    await scenario.settle()
    return protocol


def _player(protocol: MSXPlayer) -> Player:
    """Return the player a TV is attached to."""
    assert protocol.player is not None
    return protocol.player


async def test_a_new_tv_gets_a_minted_player_that_its_commands_address(
    scenario: Scenario, provider: MSXBridgeProvider
) -> None:
    """A TV first seen now has a player of its own, which the queue and player commands use."""
    protocol = await _request(scenario, provider)

    player_id = _player(protocol).player_id
    assert player_id.startswith(PLAYER_ID_PREFIX)
    assert provider.get_player_id(TV) == player_id
    assert scenario.mass.player_queues.get_active_queue(provider.get_player_id(TV)) is not None


async def test_a_disabled_tv_stays_off_until_its_player_is_enabled(
    scenario: Scenario, provider: MSXBridgeProvider
) -> None:
    """The requests of a disabled TV do not bring it back; enabling its player does."""
    protocol = await _request(scenario, provider)
    player_id = _player(protocol).player_id

    await scenario.disable(player_id)
    returned = await _request(scenario, provider)

    assert returned is protocol
    assert protocol.player is None
    assert scenario.player(player_id) is None

    await scenario.enable(player_id)
    await scenario.idle()

    assert _player(protocol).player_id == player_id
    assert scenario.player(player_id).available is True  # type: ignore[union-attr]


async def test_an_idle_tv_stays_listed_and_returns_on_its_player(
    scenario: Scenario, provider: MSXBridgeProvider
) -> None:
    """The idle timeout leaves the player unavailable; the TV's next request makes it available."""
    protocol = await _request(scenario, provider)
    player_id = _player(protocol).player_id

    await provider._handle_player_unregister(TV)
    await scenario.settle()

    assert scenario.player(player_id).available is False  # type: ignore[union-attr]

    await _request(scenario, provider)

    assert _player(protocol).player_id == player_id
    assert scenario.player(player_id).available is True  # type: ignore[union-attr]


async def test_a_tv_that_was_playing_returns_idle_after_its_idle_timeout(
    scenario: Scenario, provider: MSXBridgeProvider
) -> None:
    """A TV switched off mid-playback comes back available and idle, and so does its queue."""
    protocol = await _request(scenario, provider)
    player_id = _player(protocol).player_id
    await scenario.play(player_id)

    protocol.on_ws_disconnected()
    await provider._handle_player_unregister(TV)
    await scenario.settle()
    await _request(scenario, provider)

    assert scenario.player(player_id).available is True  # type: ignore[union-attr]

    protocol.on_ws_connected()
    await scenario.idle()

    assert scenario.player(player_id).playback_state == PlaybackState.IDLE  # type: ignore[union-attr]
    assert protocol.current_media is None
    assert scenario.queue(player_id).state == PlaybackState.IDLE  # type: ignore[union-attr]


async def test_a_removed_tv_comes_back_as_a_new_player(
    scenario: Scenario, provider: MSXBridgeProvider
) -> None:
    """A removed player is gone; the TV gets a new player when it asks again."""
    player_id = _player(await _request(scenario, provider)).player_id

    await scenario.remove(player_id)
    await scenario.idle()

    assert scenario.player(player_id) is None
    returned = await _request(scenario, provider)
    assert _player(returned).player_id != player_id


async def test_a_setting_of_a_new_player_is_its_tv_s(
    scenario: Scenario, provider: MSXBridgeProvider
) -> None:
    """The codec of a player with a minted id is stored for its TV, also after it was idle."""
    protocol = await _request(scenario, provider)
    player_id = _player(protocol).player_id

    await scenario.configure_player(player_id, **{CONF_OUTPUT_CODEC: "aac"})
    await provider._handle_player_unregister(TV)
    await _request(scenario, provider)

    assert protocol.config.get_value(CONF_OUTPUT_CODEC) == "aac"
    assert CONF_OUTPUT_CODEC not in (scenario.mass.config.get(f"players/{player_id}/values") or {})


@pytest.mark.parametrize("away", [True, False], ids=["after-idle-timeout", "while-attached"])
async def test_a_tv_under_a_new_address_reports_it(
    scenario: Scenario, provider: MSXBridgeProvider, away: bool
) -> None:
    """
    A TV that returns under another address reports that one.

    The device that has its old address now has an audio output of its own player.
    """
    scenario.arp["10.0.0.20"] = "00:11:22:33:44:55"
    protocol = await provider.get_or_register_player(TV, ip_address="10.0.0.20")
    await scenario.settle()
    player_id = _player(protocol).player_id
    if away:
        await provider._handle_player_unregister(TV)
        await scenario.settle()
    scenario.arp["10.0.0.20"] = "00:11:22:33:44:66"
    scenario.arp["10.0.0.21"] = "00:11:22:33:44:55"
    scenario.clear_events()

    returned = await provider.get_or_register_player(TV, ip_address="10.0.0.21")
    await scenario.settle()
    assert scenario.events(EventType.PLAYER_UPDATED, object_id=player_id)
    other_device = (await scenario.add_provider("dlna")).protocol(
        "uuid:device-b",
        "Device B",
        role="audio",
        identifiers={
            IdentifierType.IP_ADDRESS: "10.0.0.20",
            IdentifierType.MAC_ADDRESS: "00:11:22:33:44:66",
        },
    )
    await scenario.register(other_device)

    assert _player(returned).player_id == player_id
    assert returned.device_info.ip_address == "10.0.0.21"
    assert scenario.player_of(other_device) != player_id


async def test_two_clients_on_one_host_stay_two_players(
    scenario: Scenario, provider: MSXBridgeProvider
) -> None:
    """A client first seen while another client of its host is away gets a player of its own."""
    scenario.arp["10.0.0.20"] = "00:11:22:33:44:55"
    first = await provider.get_or_register_player(
        "msx_client_a", ip_address="10.0.0.20", device_id="client-a"
    )
    await scenario.settle()
    first_player = _player(first).player_id
    await provider._handle_player_unregister(first.protocol_id)
    await scenario.settle()

    second = await provider.get_or_register_player(
        "msx_client_b", ip_address="10.0.0.20", device_id="client-b"
    )
    await scenario.settle()

    assert _player(second).player_id != first_player
    record = scenario.mass.config.get_protocol_record(first.protocol_id)
    assert record is not None
    assert record["player_id"] == first_player
