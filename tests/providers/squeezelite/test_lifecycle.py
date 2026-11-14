"""Tests for squeezelite players on a running server: identity, connect and disconnect, config."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

from aioslimproto.models import PlayerState as SlimPlayerState
from aioslimproto.models import VisualisationType as SlimVisualisationType
from music_assistant_models.enums import ContentType, ProtocolRole
from music_assistant_models.media_items import AudioFormat

from music_assistant.controllers.players.protocol_linking import PLAYER_ID_PREFIX
from music_assistant.providers.squeezelite.constants import CONF_ENTRY_DISPLAY
from music_assistant.providers.squeezelite.multi_client_stream import MultiClientStream
from music_assistant.providers.squeezelite.player import SqueezelitePlayer

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from music_assistant.models.player import Player
    from tests.scenarios.harness import Scenario

HOST_IP = "192.168.1.30"
# the first instance on the host uses the MAC address of the host, the second one a MAC
# address of its own (squeezelite -m)
AMP_ONE = "b8:27:eb:11:22:33"
AMP_TWO = "02:42:ac:11:00:02"


def _client(player_id: str, name: str, *, device_model: str = "SqueezeLite") -> MagicMock:
    """Return a connected slimproto client of a squeezelite instance on the host."""
    client = MagicMock(
        player_id=player_id,
        device_model=device_model,
        device_type="squeezelite",
        device_address=HOST_IP,
        max_sample_rate=192000,
        connected=True,
        powered=True,
        state=SlimPlayerState.STOPPED,
        volume_level=20,
        muted=False,
        current_media=None,
        elapsed_seconds=0,
    )
    client.name = name
    for command in ("stop", "mute", "volume_set", "power", "configure_display"):
        setattr(client, command, AsyncMock())
    return client


async def _connect(scenario: Scenario, client: MagicMock) -> SqueezelitePlayer:
    """Let a client connect: the provider sets up its protocol, which registers it."""
    protocol = SqueezelitePlayer(
        scenario.provider("squeezelite"),  # type: ignore[arg-type]
        client.player_id,
        client,
    )
    await protocol.setup()
    await scenario.settle()
    return protocol


async def _disconnect(scenario: Scenario, protocol: SqueezelitePlayer) -> None:
    """Let a client disconnect: the provider unregisters its protocol."""
    await scenario.mass.players.unregister(protocol.protocol_id)
    await scenario.settle()


def _player(protocol: SqueezelitePlayer) -> Player:
    """Return the player a protocol is attached to."""
    assert protocol.player is not None
    return protocol.player


async def test_two_instances_on_one_host_stay_two_players(scenario: Scenario) -> None:
    """A second instance first seen while the first is away gets a player of its own."""
    await scenario.add_provider("squeezelite")
    amp_one = await _connect(scenario, _client(AMP_ONE, "Pi amp 1"))
    player_one = _player(amp_one).player_id
    await scenario.configure_player(player_one, name="Amp one")
    await _disconnect(scenario, amp_one)

    amp_two = await _connect(scenario, _client(AMP_TWO, "Pi amp 2"))

    player_two = _player(amp_two).player_id
    assert player_two != player_one
    assert player_two.startswith(PLAYER_ID_PREFIX)
    assert _player(amp_two).name == "Pi amp 2"
    assert scenario.mass.config.get_protocol_record(AMP_ONE)["player_id"] == player_one  # type: ignore[index]

    amp_one = await _connect(scenario, _client(AMP_ONE, "Pi amp 1"))

    assert _player(amp_one).player_id == player_one
    assert _player(amp_one).name == "Amp one"
    assert [player.name for player in scenario.visible_players()] == ["Amp one", "Pi amp 2"]


async def test_each_instance_finds_its_own_player_after_a_restart(scenario: Scenario) -> None:
    """After a restart the instances come back in any order, each on its own player."""
    await scenario.add_provider("squeezelite")
    amp_one = await _connect(scenario, _client(AMP_ONE, "Pi amp 1"))
    amp_two = await _connect(scenario, _client(AMP_TWO, "Pi amp 2"))
    player_one, player_two = _player(amp_one).player_id, _player(amp_two).player_id
    assert player_one != player_two
    await scenario.configure_player(player_one, name="Amp one")

    await scenario.restart()
    amp_two = await _connect(scenario, _client(AMP_TWO, "Pi amp 2"))
    amp_one = await _connect(scenario, _client(AMP_ONE, "Pi amp 1"))

    assert _player(amp_one).player_id == player_one
    assert _player(amp_two).player_id == player_two
    assert _player(amp_one).name == "Amp one"


async def test_a_disconnected_client_leaves_its_player_unavailable(scenario: Scenario) -> None:
    """A client that disconnects keeps its player; its next connection attaches to it again."""
    await scenario.add_provider("squeezelite")
    protocol = await _connect(scenario, _client(AMP_ONE, "Pi amp 1"))
    player_id = _player(protocol).player_id

    await _disconnect(scenario, protocol)

    state = scenario.player(player_id)
    assert state is not None
    assert state.available is False

    returned = await _connect(scenario, _client(AMP_ONE, "Pi amp 1"))

    assert _player(returned).player_id == player_id
    assert scenario.player(player_id).available is True  # type: ignore[union-attr]


async def test_a_detached_player_ends_the_stream_of_its_sync_group(scenario: Scenario) -> None:
    """A detach ends the stream the player serves to its sync group, and closes its audio source."""
    await scenario.add_provider("squeezelite")
    protocol = await _connect(scenario, _client(AMP_ONE, "Pi amp 1"))
    started = asyncio.Event()
    closed = asyncio.Event()

    async def _source() -> AsyncGenerator[bytes]:
        try:
            started.set()
            await asyncio.Event().wait()
            yield b""
        finally:
            closed.set()

    stream = protocol.multi_client_stream = MultiClientStream(
        audio_source=_source(),
        audio_format=AudioFormat(content_type=ContentType.PCM_S16LE),
        queue_id=None,
        session_id=None,
    )
    # a member connected, so the stream reads its source
    stream.subscribers.append(asyncio.Queue())
    await scenario.advance(1)
    assert started.is_set()

    await _disconnect(scenario, protocol)
    # the controller detaches again when a detach overtook an attach
    await protocol.on_detach()

    assert stream.done
    assert closed.is_set()
    assert protocol.multi_client_stream is None


async def test_a_bridged_device_is_an_audio_protocol(scenario: Scenario) -> None:
    """A squeezelite client on a device of its own (a WiiM) attaches as an audio protocol."""
    await scenario.add_provider("squeezelite")
    protocol = await _connect(scenario, _client(AMP_ONE, "WiiM", device_model="WiiM Player"))

    assert _player(protocol).get_role(protocol.protocol_id) == ProtocolRole.AUDIO


async def test_a_setting_of_a_new_player_reaches_its_client(scenario: Scenario) -> None:
    """The display setting of a player with a minted id is the client's, also after a return."""
    await scenario.add_provider("squeezelite")
    client = _client(AMP_ONE, "Pi amp 1")
    protocol = await _connect(scenario, client)
    player_id = _player(protocol).player_id
    assert player_id != protocol.protocol_id

    # the display is off by default
    await scenario.configure_player(player_id, **{CONF_ENTRY_DISPLAY.key: True})

    assert scenario.protocol_config(AMP_ONE)[CONF_ENTRY_DISPLAY.key] is True  # type: ignore[index]
    player_values = scenario.mass.config.get(f"players/{player_id}/values") or {}
    assert CONF_ENTRY_DISPLAY.key not in player_values
    client.configure_display.assert_awaited_with(
        visualisation=SlimVisualisationType.NONE, disabled=False
    )

    await _disconnect(scenario, protocol)
    returned_client = _client(AMP_ONE, "Pi amp 1")
    await _connect(scenario, returned_client)

    returned_client.configure_display.assert_awaited_with(
        visualisation=SlimVisualisationType.NONE, disabled=False
    )
