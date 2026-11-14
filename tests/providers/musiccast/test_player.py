"""Tests for the MusicCast player state updates and polling."""

import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import ClientError
from music_assistant_models.enums import PlaybackState

from music_assistant.helpers.upnp import AVTransport
from music_assistant.providers.musiccast.constants import CONF_PLAYER_AUTO_ADVANCE
from music_assistant.providers.musiccast.player import MusicCastPlayer, UpnpUpdateHelper

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


def test_a_musiccast_player_gets_the_gapless_burst(network: MusicCastNetwork) -> None:
    """A MusicCast device only plays a track gapless once it holds a whole opening chunk of it."""
    assert network.player("A___main").needs_gapless_burst
