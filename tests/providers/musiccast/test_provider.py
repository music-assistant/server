"""Tests for MusicCast device discovery and player registration."""

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest
from aiohttp import ClientError
from aiomusiccast.exceptions import MusicCastConnectionException

from music_assistant.helpers.upnp import AVTransport
from music_assistant.providers.musiccast import provider as provider_module
from music_assistant.providers.musiccast.player import MusicCastPlayer

from .conftest import FakeMusicCastDevice, MusicCastNetwork


class _DiscoveredDevice(FakeMusicCastDevice):
    """Device receiver B announces under a new IP."""

    def __init__(self, client: Any, ip: str, upnp_description: str) -> None:
        super().__init__(ip, "B", upnp_description)

    @classmethod
    async def check_yamaha_ssdp(cls, *_: Any) -> bool:
        return True


@pytest.mark.parametrize(
    ("fail_fetch", "expected_ips"),
    [
        pytest.param(False, ["192.168.1.10", "192.168.1.21"], id="replaced"),
        pytest.param(True, ["192.168.1.10", "192.168.1.20"], id="setup-failed"),
    ],
)
async def test_rediscovery_keeps_one_device_per_receiver(
    network: MusicCastNetwork,
    monkeypatch: pytest.MonkeyPatch,
    fail_fetch: bool,
    expected_ips: list[str],
) -> None:
    """A rediscovered receiver replaces its stale device, a failed setup keeps it."""
    monkeypatch.setattr(provider_module, "MusicCastDevice", _DiscoveredDevice)
    monkeypatch.setattr(_DiscoveredDevice, "fail_fetch", fail_fetch)
    monkeypatch.setattr(MusicCastPlayer, "setup", AsyncMock())
    network.player("B___main")._attr_available = False

    await network.provider._device_discovered(
        device_id="B",
        device_ip="192.168.1.21",
        description_url="http://192.168.1.21:49154/MediaRenderer/desc.xml",
    )

    assert [
        physical_device.device.ip
        for physical_device in network.provider.mc_controller.physical_devices
    ] == expected_ips


@pytest.mark.parametrize(
    ("player_id", "expected_registered"),
    [
        pytest.param("B___zone2", True, id="known-device"),
        pytest.param("C___main", False, id="unknown-device"),
    ],
)
async def test_player_enabled_again(
    network: MusicCastNetwork,
    monkeypatch: pytest.MonkeyPatch,
    player_id: str,
    expected_registered: bool,
) -> None:
    """A re-enabled player of a known device gets registered without a rediscovery."""
    monkeypatch.setattr(MusicCastPlayer, "setup", AsyncMock())
    network.registry.pop("B___zone2")

    network.provider.on_player_enabled(player_id)
    await asyncio.gather(*network.tasks)

    assert (player_id in network.registry) is expected_registered
    if expected_registered:
        physical_device = network.provider.musiccast_player_helpers["B"].physical_device
        assert network.registry[player_id].physical_device is physical_device


async def test_player_enabled_while_receiver_unreachable(
    network: MusicCastNetwork, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A main re-enabled while its receiver is unreachable still gets registered."""
    monkeypatch.setattr(
        AVTransport, "get_media_info", AsyncMock(side_effect=ClientError("unreachable"))
    )
    device = network.player("B___main").physical_device.device
    monkeypatch.setattr(
        device,
        "request_json",
        AsyncMock(side_effect=MusicCastConnectionException),
        raising=False,
    )
    network.registry.pop("B___main")

    network.provider.on_player_enabled("B___main")
    await asyncio.gather(*network.tasks)

    assert network.registry["B___main"].physical_device.device is device


async def test_rediscovery_with_failing_zone_keeps_registrations(
    network: MusicCastNetwork, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A zone failing to set up on rediscovery leaves all players on the previous device."""
    monkeypatch.setattr(provider_module, "MusicCastDevice", _DiscoveredDevice)

    async def setup(player: MusicCastPlayer) -> None:
        if player.zone_device.zone_name == "zone2":
            raise RuntimeError("zone2 setup failed")

    monkeypatch.setattr(MusicCastPlayer, "setup", setup)
    previous_device = network.player("B___main").physical_device
    network.player("B___main")._attr_available = False

    with pytest.raises(RuntimeError):
        await network.provider._device_discovered(
            device_id="B",
            device_ip="192.168.1.21",
            description_url="http://192.168.1.21:49154/MediaRenderer/desc.xml",
        )

    assert network.registry["B___main"].physical_device is previous_device
    assert [
        physical_device.device.ip
        for physical_device in network.provider.mc_controller.physical_devices
    ] == ["192.168.1.10", "192.168.1.20"]


async def test_player_enabled_during_rediscovery(
    network: MusicCastNetwork, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rediscovery while a re-enabled player sets up leaves the player on the new device."""
    monkeypatch.setattr(provider_module, "MusicCastDevice", _DiscoveredDevice)
    release_first_setup = asyncio.Event()
    setups: list[MusicCastPlayer] = []

    async def setup(player: MusicCastPlayer) -> None:
        setups.append(player)
        if len(setups) == 1:
            await release_first_setup.wait()

    monkeypatch.setattr(MusicCastPlayer, "setup", setup)
    network.registry.pop("B___zone2")
    network.player("B___main")._attr_available = False

    network.provider.on_player_enabled("B___zone2")
    await asyncio.sleep(0)
    rediscovery = asyncio.ensure_future(
        network.provider._device_discovered(
            device_id="B",
            device_ip="192.168.1.21",
            description_url="http://192.168.1.21:49154/MediaRenderer/desc.xml",
        )
    )
    await asyncio.sleep(0.01)
    release_first_setup.set()
    await asyncio.gather(rediscovery, *network.tasks)

    physical_device = network.provider.musiccast_player_helpers["B"].physical_device
    assert physical_device.device.ip == "192.168.1.21"
    assert network.registry["B___zone2"].physical_device is physical_device


DESCRIPTION_URL_B = "http://192.168.1.20:49154/MediaRenderer/desc.xml"


@pytest.mark.parametrize(
    "removed_zones",
    [
        pytest.param(["main"], id="main"),
        pytest.param(["zone2"], id="zone2"),
        pytest.param(["main", "zone2", "zone3"], id="all-zones"),
    ],
)
async def test_removed_player_found_again_as_new_player(
    network: MusicCastNetwork, monkeypatch: pytest.MonkeyPatch, removed_zones: list[str]
) -> None:
    """A removed zone of a receiver still on the network is registered anew on its next discovery."""
    monkeypatch.setattr(provider_module, "MusicCastDevice", _DiscoveredDevice)
    monkeypatch.setattr(MusicCastPlayer, "setup", AsyncMock())
    previous_device = network.player("B___main").physical_device
    previous_device.device.upnp_description = DESCRIPTION_URL_B
    removed = {zone: network.registry.pop(f"B___{zone}") for zone in removed_zones}
    for player in removed.values():
        await player.on_remove()
    # the other zones keep the device until the receiver is set up again, a receiver without
    # a zone in use lets go of it right away
    in_use = len(removed) < 3
    assert (previous_device in network.provider.mc_controller.physical_devices) is in_use
    assert ("B" in network.provider.musiccast_player_helpers) is in_use
    assert all(
        player.physical_device is previous_device
        for player_id, player in network.registry.items()
        if player_id.startswith("B___")
    )

    await network.provider._device_discovered(
        device_id="B", device_ip="192.168.1.20", description_url=DESCRIPTION_URL_B
    )

    physical_device = network.provider.musiccast_player_helpers["B"].physical_device
    assert physical_device is not previous_device
    for zone in ("main", "zone2", "zone3"):
        assert network.registry[f"B___{zone}"].physical_device is physical_device
    for zone, player in removed.items():
        assert network.registry[f"B___{zone}"] is not player
    assert network.provider.mc_controller.physical_devices == [
        network.provider.musiccast_player_helpers["A"].physical_device,
        physical_device,
    ]
