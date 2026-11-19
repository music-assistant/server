"""Tests for a HEOS player attached on a running server, and for its discovery."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import IdentifierType, MediaType, ProtocolRole
from pyheos import PlayState as HeosPlayState
from pyheos import const as heos_const

from music_assistant.controllers.players.protocol_linking import PLAYER_ID_PREFIX
from music_assistant.models.player import PlayerMedia
from music_assistant.providers.heos.constants import CONF_PLAYBACK_TRANSITION_TIMEOUT
from music_assistant.providers.heos.player import HeosPlayer
from music_assistant.providers.heos.provider import HeosPlayerProvider
from tests.scenarios.harness import Scenario

if TYPE_CHECKING:
    import pathlib
    from collections.abc import AsyncGenerator

IP_ADDRESS = "192.168.1.81"
PROTOCOL_ID = "-1234567"


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


def _device(player_id: str = PROTOCOL_ID, name: str = "Speaker A") -> MagicMock:
    """Return a HEOS device as the controller connection reports it."""
    device = MagicMock()
    device.player_id = int(player_id)
    device.name = name
    device.model = "Denon Home 150"
    device.version = "3.34.410"
    device.ip_address = IP_ADDRESS
    device.available = True
    device.heos = MagicMock()
    device.state = HeosPlayState.STOP
    device.volume = 20
    device.is_muted = False
    device.now_playing_media.current_position = None
    device.now_playing_media.current_position_updated = None
    return device


async def _register(scenario: Scenario, device: MagicMock) -> HeosPlayer:
    """Register the protocol of a HEOS device the way discovery sets it up."""
    provider: Any = scenario.mass.get_provider("heos") or await scenario.add_provider("heos")
    provider._heos_queue = MagicMock()
    protocol = HeosPlayer(provider, device)
    protocol.set_device_info()
    await scenario.mass.players.register(protocol)
    await scenario.settle()
    return protocol


async def test_a_new_device_is_the_native_protocol_of_a_minted_player(
    scenario: Scenario,
) -> None:
    """A device first seen now gets a player of its own, identified by its HEOS id."""
    protocol = await _register(scenario, _device())

    assert protocol.player is not None
    assert protocol.player.player_id.startswith(PLAYER_ID_PREFIX)
    assert protocol.player.get_role(PROTOCOL_ID) == ProtocolRole.NATIVE
    assert (IdentifierType.UUID, PROTOCOL_ID) in protocol.identifiers
    assert (IdentifierType.IP_ADDRESS, IP_ADDRESS) in protocol.identifiers


async def test_two_heos_players_on_one_host_stay_apart(scenario: Scenario) -> None:
    """A new HEOS player on the host of one that is away does not take over its record."""
    first = await _register(scenario, _device())
    assert first.player is not None
    first_player_id = first.player.player_id
    await scenario.restart()

    second = await _register(scenario, _device("-7654321", "Speaker B"))

    assert second.player is not None
    assert second.player.player_id != first_player_id
    assert scenario.mass.config.get_protocol_ids_of_player(first_player_id) == [PROTOCOL_ID]


async def test_the_device_events_follow_every_attach(scenario: Scenario) -> None:
    """The protocol listens to its device while attached, again after a detach."""
    device = _device()
    protocol = await _register(scenario, device)
    unsubscribe = device.add_on_player_event.return_value
    assert device.add_on_player_event.call_count == 1

    await scenario.mass.players.unregister(PROTOCOL_ID)
    await scenario.settle()
    unsubscribe.assert_called_once()

    await scenario.mass.players.register(protocol)
    await scenario.settle()
    assert device.add_on_player_event.call_count == 2


async def test_a_new_device_reads_its_settings_from_its_own_record(scenario: Scenario) -> None:
    """The transition timeout of a device with a minted player id comes from its protocol."""
    device = _device()
    protocol = await _register(scenario, device)
    assert protocol.player is not None
    player_id = protocol.player.player_id
    await scenario.mass.config.save_protocol_config(
        PROTOCOL_ID, {CONF_PLAYBACK_TRANSITION_TIMEOUT: 12}
    )
    await scenario.settle()
    await scenario.mass.players.unregister(PROTOCOL_ID)
    await scenario.mass.players.register(protocol)
    await scenario.settle()
    device.play_url = AsyncMock()
    media = PlayerMedia(
        uri="source://line_in",
        media_type=MediaType.AUDIO_SOURCE,
        source_id="line_in",
        queue_session_id="session_a",
    )

    with patch.object(scenario.mass, "call_later") as call_later:
        await protocol.play_media(media)

    assert (
        scenario.mass.config.get_player_value(player_id, CONF_PLAYBACK_TRANSITION_TIMEOUT) is None
    )
    assert call_later.call_args.args[0] == 12


async def test_discovery_only_builds_the_wanted_devices_without_a_protocol() -> None:
    """An attached device is refreshed, a disabled one skipped, a new one set up."""
    attached = MagicMock()
    provider = HeosPlayerProvider.__new__(HeosPlayerProvider)
    provider.logger = MagicMock()
    provider._controller_discovery_running = False
    provider._player_discovery_running = False
    provider._heos = MagicMock()
    provider._heos.get_players = AsyncMock(
        return_value={1: _device("1"), 2: _device("2"), 3: _device("3")}
    )
    provider.mass = MagicMock()
    provider.mass.players.get_protocol.side_effect = lambda pid: attached if pid == "1" else None
    provider.mass.players.wants_protocol.side_effect = lambda pid: pid != "2"

    with patch("music_assistant.providers.heos.provider.HeosPlayer") as player_cls:
        player_cls.return_value.setup = AsyncMock()
        await provider.discover_players()

    attached.set_device_info.assert_called_once()
    assert [call.args[1].player_id for call in player_cls.call_args_list] == [3]
    player_cls.return_value.setup.assert_awaited_once()


async def test_a_detached_device_leaves_its_queue_alone(scenario: Scenario) -> None:
    """A detach ends the cleanup of the device's own queue that MA playback scheduled."""
    device = _device()
    device.play_url = AsyncMock()
    protocol = await _register(scenario, device)
    queue: Any = protocol._heos_queue
    queue.player_get_queue = AsyncMock(
        return_value=[SimpleNamespace(queue_id=1), SimpleNamespace(queue_id=2)]
    )
    queue.get_now_playing_media = AsyncMock(return_value=SimpleNamespace(queue_id=2))
    queue.player_remove_from_queue = AsyncMock()
    await protocol.play_media(
        PlayerMedia(
            uri="source://line_in",
            media_type=MediaType.AUDIO_SOURCE,
            source_id="line_in",
            queue_session_id="session_a",
        )
    )
    device.state = HeosPlayState.PLAY
    on_event = device.add_on_player_event.call_args.args[0]
    await on_event(heos_const.EVENT_PLAYER_QUEUE_CHANGED)

    await scenario.mass.players.unregister(PROTOCOL_ID)
    await scenario.advance(5)

    queue.player_remove_from_queue.assert_not_awaited()
