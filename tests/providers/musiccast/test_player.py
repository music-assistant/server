"""Tests for the MusicCast player state updates and polling."""

import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import ClientError

from music_assistant.providers.musiccast import player as player_module
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
        player_module, "avt_get_media_info", AsyncMock(side_effect=ClientError("timeout"))
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
