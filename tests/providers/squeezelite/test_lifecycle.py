"""Tests for squeezelite players on a running server: identity, lifecycle, config."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
from aioslimproto.models import PlayerState as SlimPlayerState
from aioslimproto.models import VisualisationType as SlimVisualisationType
from music_assistant_models.enums import ProtocolRole, RepeatMode

from music_assistant.constants import CONF_PROTOCOL_KEY_SPLITTER, CONF_SYNC_ADJUST
from music_assistant.controllers.players.protocol_linking import PLAYER_ID_PREFIX
from music_assistant.providers.squeezelite.constants import CONF_ENTRY_DISPLAY
from music_assistant.providers.squeezelite.player import SqueezelitePlayer

if TYPE_CHECKING:
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


@pytest.mark.parametrize(
    ("device_model", "setting"),
    [
        ("SqueezeLite", CONF_SYNC_ADJUST),
        # the settings of an audio protocol are listed under the id of that output
        ("WiiM Player", f"{AMP_ONE}{CONF_PROTOCOL_KEY_SPLITTER}{CONF_SYNC_ADJUST}"),
    ],
    ids=["own protocol", "audio protocol"],
)
async def test_the_sync_delay_of_a_new_player_is_that_of_its_squeezelite_output(
    scenario: Scenario, device_model: str, setting: str
) -> None:
    """The sync delay of a player with a minted id is its output's, also after a return."""
    await scenario.add_provider("squeezelite")
    client = _client(AMP_ONE, "Pi amp 1", device_model=device_model)
    protocol = await _connect(scenario, client)
    player_id = _player(protocol).player_id
    assert player_id != protocol.protocol_id
    assert protocol.config.get_value(CONF_SYNC_ADJUST) == 0
    entries = await scenario.mass.config.get_player_config_entries(player_id)
    assert setting in [entry.key for entry in entries]

    await scenario.configure_player(player_id, **{setting: 30})

    assert scenario.protocol_config(AMP_ONE)[CONF_SYNC_ADJUST] == 30  # type: ignore[index]
    player_values = scenario.mass.config.get(f"players/{player_id}/values") or {}
    assert CONF_SYNC_ADJUST not in player_values
    assert protocol.config.get_value(CONF_SYNC_ADJUST) == 30

    await _disconnect(scenario, protocol)
    returned = await _connect(scenario, _client(AMP_ONE, "Pi amp 1", device_model=device_model))

    assert returned.config.get_value(CONF_SYNC_ADJUST) == 30


async def test_a_sync_delay_set_before_the_split_is_that_of_its_squeezelite_output(
    scenario: Scenario,
) -> None:
    """A sync delay stored with the player before the split is that of its output."""
    provider = await scenario.add_provider("squeezelite")
    await scenario.seed_pre_split_config(
        {
            AMP_ONE: {
                "player_id": AMP_ONE,
                "provider": provider.instance_id,
                "player_type": "player",
                "enabled": True,
                "name": "Amp one",
                "default_name": "Pi amp 1",
                "values": {CONF_SYNC_ADJUST: 30},
            }
        }
    )

    protocol = await _connect(scenario, _client(AMP_ONE, "Pi amp 1"))

    assert _player(protocol).player_id == AMP_ONE
    assert _player(protocol).name == "Amp one"
    assert protocol.config.get_value(CONF_SYNC_ADJUST) == 30


async def test_the_client_shows_repeat_and_shuffle_of_its_queue(scenario: Scenario) -> None:
    """The status a client polls has repeat and shuffle of the queue its player plays."""
    await scenario.add_provider("squeezelite")
    client = _client(AMP_ONE, "Pi amp 1")
    client.extra_data = {"playlist repeat": 0, "playlist shuffle": 0}
    amp = await _connect(scenario, client)
    queue_id = _player(amp).player_id
    client.signal_update.reset_mock()

    await scenario.mass.player_queues.set_repeat(queue_id, RepeatMode.ALL)
    await scenario.mass.player_queues.set_shuffle(queue_id, True)
    amp.on_queue_updated(queue_id)

    assert client.extra_data == {"playlist repeat": 2, "playlist shuffle": 1}
    client.signal_update.assert_called_once_with()
    # the same values again, or a change of another queue, leave the client alone
    amp.on_queue_updated(queue_id)
    amp.on_queue_updated("another_queue")
    client.signal_update.assert_called_once_with()
