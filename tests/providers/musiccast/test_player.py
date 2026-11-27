"""Tests for the MusicCast player state updates and polling."""

import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import ClientError
from music_assistant_models.enums import PlaybackState

from music_assistant.controllers.players.power import _CONTINUES_PLAYBACK
from music_assistant.helpers.upnp import AVTransport
from music_assistant.providers.musiccast.constants import CONF_PLAYER_AUTO_ADVANCE
from music_assistant.providers.musiccast.player import MusicCastPlayer, UpnpUpdateHelper
from tests.common import bare_player_controller

from .conftest import MusicCastNetwork


@pytest.mark.parametrize(
    ("min_volume", "max_volume", "current_volume", "expected_volume"),
    [
        pytest.param(0, 100, 40, 40, id="plain"),
        pytest.param(0, 161, 80, 50, id="rounded"),
        pytest.param(10, 110, 60, 50, id="min-volume-offset"),
    ],
)
async def test_update_without_upnp_media_info(
    network: MusicCastNetwork,
    monkeypatch: pytest.MonkeyPatch,
    min_volume: int,
    max_volume: int,
    current_volume: int,
    expected_volume: int,
) -> None:
    """A failing UPnP media info request still publishes the volume from the device data."""
    monkeypatch.setattr(
        AVTransport, "get_media_info", AsyncMock(side_effect=ClientError("timeout"))
    )
    player = network.player("A___main")
    zone_data = player.zone_device.zone_data
    zone_data.min_volume = min_volume
    zone_data.max_volume = max_volume
    zone_data.current_volume = current_volume
    player.upnp_update_helper = UpnpUpdateHelper(
        last_poll=time.time() - 10, controlled_by_mass=False, current_uri=None
    )
    update_state = MagicMock()
    monkeypatch.setattr(player, "update_state", update_state)

    await player.set_dynamic_attributes()

    assert player._attr_volume_level == expected_volume
    update_state.assert_called_once()


@pytest.mark.parametrize(
    ("registered_player_ids", "polling_player_id"),
    [
        pytest.param(["A___main", "A___zone2"], "A___main", id="main-polls"),
        pytest.param(["A___zone2"], "A___zone2", id="zone-polls-while-main-disabled"),
    ],
)
async def test_poll_by_first_registered_zone(
    network: MusicCastNetwork,
    monkeypatch: pytest.MonkeyPatch,
    registered_player_ids: list[str],
    polling_player_id: str,
) -> None:
    """Exactly one registered player of a device fetches the device on poll."""
    monkeypatch.setattr(MusicCastPlayer, "set_dynamic_attributes", AsyncMock())
    for player_id in ["A___main", "A___zone2"]:
        if player_id not in registered_player_ids:
            del network.registry[player_id]
    device = network.player("A___main").physical_device.device

    polled_by = []
    for player_id in registered_player_ids:
        fetch_count = device.fetch_count
        await network.player(player_id).poll()
        if device.fetch_count > fetch_count:
            polled_by.append(player_id)

    assert polled_by == [polling_player_id]


async def test_poll_restores_udp_for_reenabled_player(
    network: MusicCastNetwork, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A player re-enabled while UDP was stopped gets UDP back with the next successful poll."""
    monkeypatch.setattr(MusicCastPlayer, "set_dynamic_attributes", AsyncMock())
    player = network.player("A___main")
    device = player.physical_device.device
    # UDP stopped by a connection loss, the re-enabled player starts out available
    device.transport = None
    player._attr_available = True

    await player.poll()

    assert device.transport is not None


def _attach_to_new_player(player: MusicCastPlayer) -> None:
    """Attach a zone to a player with an id of its own, as a zone first seen now gets."""
    player.set_player(MagicMock(player_id="pl_living"))


async def test_playback_of_music_assistant_names_the_queue_of_the_player(
    network: MusicCastNetwork, monkeypatch: pytest.MonkeyPatch
) -> None:
    """While Music Assistant plays on the zone, its source is the queue of the zone's player."""
    player = network.player("A___main")
    _attach_to_new_player(player)
    player.upnp_update_helper = UpnpUpdateHelper(
        last_poll=time.time(),
        controlled_by_mass=True,
        current_uri=f"http://ma:8097/single/session/pl_living/item/{player.protocol_id}.flac",
    )
    monkeypatch.setattr(player, "update_state", MagicMock())

    await player.set_dynamic_attributes()

    assert player._attr_active_source == "pl_living"


async def test_input_without_metadata_drops_the_previous_media(
    network: MusicCastNetwork, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An input that reports no metadata shows none, not what the net source played before."""
    player = network.player("A___main")
    device = player.physical_device.device
    device.data.zones["main"].input = "server"
    device.data.netusb_track = "Queue Song"
    device.data.netusb_artist = "Queue Artist"
    device.data.netusb_album = "Queue Album"
    device.data.netusb_play_time_updated = None
    device.media_image_url = "http://ma:8097/image.jpg"
    player.upnp_update_helper = UpnpUpdateHelper(
        last_poll=time.time(), controlled_by_mass=False, current_uri=None
    )
    monkeypatch.setattr(player, "update_state", MagicMock())
    await player.set_dynamic_attributes()

    device.data.zones["main"].input = "audio1"
    await player.set_dynamic_attributes()

    assert player.current_media is not None
    assert player.current_media.uri == "A___main_audio1"
    assert player.current_media.title is None
    assert player.current_media.artist is None
    assert player.current_media.album is None
    assert player.current_media.image_url is None


async def test_end_of_track_advances_the_queue_of_the_player(network: MusicCastNetwork) -> None:
    """A receiver that went idle at the end of a track advances the queue of its player."""
    player = network.player("A___main")
    _attach_to_new_player(player)
    queue = MagicMock(queue_id="pl_living", active=True)
    queue.current_item.duration = 200
    queue.current_item.queue_item_id = "item_1"
    network.mass.player_queues.get.side_effect = {"pl_living": queue}.get
    network.mass.player_queues.next = AsyncMock()
    network.mass.config.get_protocol_value.return_value = True
    player.upnp_update_helper = UpnpUpdateHelper(
        last_poll=time.time(), controlled_by_mass=True, current_uri="http://ma:8097/x.flac"
    )
    player._last_playback_state = PlaybackState.PLAYING
    player._last_playing_elapsed_time = 198
    player._attr_playback_state = PlaybackState.IDLE

    player._maybe_advance_on_track_end()
    await player._advance_queue_after_idle("item_1")

    network.mass.config.get_protocol_value.assert_called_once_with(
        player.protocol_id, CONF_PLAYER_AUTO_ADVANCE, default=True
    )
    network.mass.call_later.assert_called_once()
    network.mass.player_queues.next.assert_awaited_once_with("pl_living")


async def test_end_of_track_advance_switches_no_member_of_a_group_on(
    network: MusicCastNetwork,
) -> None:
    """The advance after an end of track is no start of a group, so it switches nothing on."""
    player = network.player("A___main")
    _attach_to_new_player(player)
    queue = MagicMock(queue_id="pl_living", active=True)
    queue.current_item.queue_item_id = "item_1"
    network.mass.player_queues.get.side_effect = {"pl_living": queue}.get
    network.mass.players = bare_player_controller()
    seen: list[bool] = []

    async def _next(*_args: object) -> None:
        seen.append(_CONTINUES_PLAYBACK.get())

    network.mass.player_queues.next = _next
    player._attr_playback_state = PlaybackState.IDLE

    await player._advance_queue_after_idle("item_1")

    assert seen == [True]


@pytest.mark.parametrize("controlled_by_mass", [True, False], ids=["plays from MA", "plays itself"])
def test_a_zone_that_plays_from_music_assistant_stops_on_a_pause(
    network: MusicCastNetwork, controlled_by_mass: bool
) -> None:
    """
    A zone that plays from Music Assistant stops on a pause, a zone that plays itself pauses.

    :param controlled_by_mass: Whether Music Assistant plays on the zone.
    """
    player = network.player("A___main")
    assert not player.stops_on_pause
    player.upnp_update_helper = UpnpUpdateHelper(
        last_poll=time.time(), controlled_by_mass=controlled_by_mass, current_uri=None
    )

    assert player.stops_on_pause is controlled_by_mass


def test_a_musiccast_player_gets_the_gapless_burst(network: MusicCastNetwork) -> None:
    """A MusicCast device only plays a track gapless once it holds a whole opening chunk of it."""
    assert network.player("A___main").needs_gapless_burst


async def test_inputs_of_the_receiver_are_marked_as_inputs(network: MusicCastNetwork) -> None:
    """The inputs of a zone are inputs; what the receiver plays itself or links in is not."""
    player = network.player("A___zone2")
    player.zone_device.zone_data.input_list = [
        "hdmi1",
        "optical",
        "usb_dac",
        "audio_cd",
        "line_cd",
        "usb",
        "bluetooth",
        "spotify",
        "tuner",
        "cd",
        "mc_link",
        "main_sync",
    ]

    await player.set_static_attributes()

    assert {x.id: x.is_input for x in player._attr_source_list} == {
        "hdmi1": True,
        "optical": True,
        "usb_dac": True,
        "audio_cd": True,
        "line_cd": True,
        "usb": False,
        "bluetooth": False,
        "spotify": False,
        "tuner": False,
        "cd": False,
        "mc_link": False,
        "main_sync": False,
    }
