"""Tests for MusicCast device discovery and player registration."""

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest

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
