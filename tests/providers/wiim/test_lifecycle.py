"""Tests for an official WiiM device attached to its player on a running server."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import IdentifierType
from wiim.exceptions import WiimRequestException

from music_assistant.providers.wiim.constants import PLAYER_ID_PREFIX
from music_assistant.providers.wiim.grouping import NativeGroupRole
from music_assistant.providers.wiim.player import WiimPlayer

if TYPE_CHECKING:
    from tests.scenarios.harness import Scenario

UDN = "uuid:wiim-pro-001"
PROTOCOL_ID = f"{PLAYER_ID_PREFIX}{UDN}"
IP_ADDRESS = "192.168.1.80"
MAC_ADDRESS = "00:22:6C:AA:BB:CC"


def _sdk_device(*_args: object, **_kwargs: object) -> MagicMock:
    """Return a fake SDK device of an idle, reachable WiiM speaker."""
    device = MagicMock()
    device.name = "Speaker A"
    device.udn = UDN
    device.ip_address = IP_ADDRESS
    device.model_name = "WiiM Pro"
    device.manufacturer = "Linkplay"
    device.firmware_version = "4.8.1"
    device.available = True
    device.supports_http_api = True
    device.supported_input_modes = ("Network", "Line In")
    device.volume = 50
    device.is_muted = False
    device.playing_status = None
    device.play_mode = None
    device.current_media = None
    device.async_set_volume = AsyncMock()
    device.sync_device_duration_and_position = AsyncMock()
    device.async_update_http_status = AsyncMock()
    device.disconnect = AsyncMock()
    device.general_event_callback = None
    device.rendering_control_event_callback = None
    device.av_transport_event_callback = None
    device.play_queue_event_callback = None
    return device


def _native_groups() -> MagicMock:
    """Return a topology coordinator that reports a standalone speaker."""
    groups = MagicMock()
    groups.role_of.return_value = NativeGroupRole.STANDALONE
    groups.members_of.return_value = []
    groups.can_group_with.return_value = set()
    groups.is_unknown_leader_follower.return_value = False
    groups.refresh_leader = AsyncMock()
    return groups


def _speaker(provider: Any) -> WiimPlayer:
    """
    Return the protocol of a speaker as discovery creates it.

    :param provider: The WiiM provider.
    """
    upnp_device = MagicMock(
        friendly_name="Speaker A", model_name="WiiM Pro", manufacturer="Linkplay", udn=UDN
    )
    return WiimPlayer(
        provider,
        PROTOCOL_ID,
        upnp_device,
        f"http://{IP_ADDRESS}:49152/description.xml",
        IP_ADDRESS,
        MAC_ADDRESS,
    )


def _detached_provider() -> MagicMock:
    """Return a provider for a protocol that is driven by hand, not by a server."""
    provider = MagicMock()
    provider.mass.closing = False
    provider.mass.create_task = MagicMock(side_effect=asyncio.ensure_future)
    provider.mass.streams.get_source_ip = AsyncMock(return_value=None)
    provider.wiim_controller = MagicMock(add_device=AsyncMock(), remove_device=AsyncMock())
    provider.native_groups = _native_groups()
    return provider


def _report_volume(device: MagicMock, volume: int) -> None:
    """
    Let the speaker report a new volume, as the SDK does through the callback set on it.

    :param device: The SDK device of the speaker.
    :param volume: The volume the speaker reports.
    """
    device.volume = volume
    if device.rendering_control_event_callback is not None:
        device.rendering_control_event_callback(MagicMock(), [])


@pytest.mark.parametrize("move", ["attach_again", "unlink"])
async def test_a_speaker_attached_again_follows_its_events_and_takes_commands(
    scenario: Scenario, move: str
) -> None:
    """A WiiM speaker attached again reports its state to its player and takes its commands."""
    provider: Any = await scenario.add_provider("wiim")
    provider.wiim_controller = MagicMock(add_device=AsyncMock(), remove_device=AsyncMock())
    provider.native_groups = _native_groups()
    if move == "unlink":
        audio = (await scenario.add_provider("airplay")).protocol(
            "ap00226caabbcc",
            "Speaker A",
            role="audio",
            identifiers={IdentifierType.MAC_ADDRESS: MAC_ADDRESS},
        )
        await scenario.attach(audio)

    create = AsyncMock(side_effect=_sdk_device)
    with patch("music_assistant.providers.wiim.player.async_create_wiim_device", create):
        protocol = _speaker(provider)
        await scenario.mass.players.attach(protocol)
        await scenario.settle()
        if move == "unlink":
            await scenario.mass.players.unlink_protocol(PROTOCOL_ID)
        else:
            await scenario.mass.players.detach(PROTOCOL_ID)
            await scenario.settle()
            await scenario.mass.players.attach(protocol)
        await scenario.settle()

    device = cast("MagicMock", protocol.device)
    _report_volume(device, 33)
    await scenario.settle()
    player = protocol.player
    assert player is not None
    assert player.state.available is True
    assert player.state.volume_level == 33
    await scenario.mass.players.cmd_volume_set(player.player_id, 20)
    device.async_set_volume.assert_awaited_once_with(20)
    device.disconnect.assert_not_awaited()
    # one SDK device per attach
    assert create.await_count == 2


async def test_a_detach_during_the_setup_leaves_no_connection() -> None:
    """A detach while the SDK device is still set up releases the device that setup creates."""
    provider = _detached_provider()
    protocol = _speaker(provider)
    device = _sdk_device()
    creating = asyncio.Event()
    finish = asyncio.Event()

    async def _create(*_args: object, **_kwargs: object) -> MagicMock:
        creating.set()
        await finish.wait()
        return device

    with patch("music_assistant.providers.wiim.player.async_create_wiim_device", _create):
        await protocol.on_attach(MagicMock())
        await creating.wait()
        await protocol.on_detach()
        finish.set()
        # the setup finishes, then the device it created is released
        for _ in range(5):
            await asyncio.sleep(0)
        # the controller detaches again once a late hook finished
        await protocol.on_detach()

    device.disconnect.assert_awaited_once_with()
    provider.wiim_controller.add_device.assert_not_awaited()
    assert protocol.device_info.software_version is None
    assert device.rendering_control_event_callback is None


async def test_a_speaker_not_reachable_at_its_attach_is_set_up_later() -> None:
    """A failed setup of the SDK device is retried after a delay, while still attached."""
    provider = _detached_provider()
    protocol = _speaker(provider)
    device = _sdk_device()
    create = AsyncMock(side_effect=[WiimRequestException("unreachable"), device])

    with (
        patch("music_assistant.providers.wiim.player.async_create_wiim_device", create),
        patch("music_assistant.providers.wiim.player.asyncio.sleep", AsyncMock()) as sleep,
    ):
        await protocol.on_attach(MagicMock())
        assert protocol._connect_task is not None
        await protocol._connect_task

    sleep.assert_awaited_once_with(30)
    assert protocol.device is device
    provider.wiim_controller.add_device.assert_awaited_once_with(device)
    assert protocol.available is True
    # a detach releases the device once, also when it runs twice
    await protocol.on_detach()
    await protocol.on_detach()
    device.disconnect.assert_awaited_once_with()
    assert protocol.available is False


async def test_a_speaker_without_its_sdk_device_is_unavailable() -> None:
    """Until its SDK device is set up a speaker is unavailable, and a poll reads nothing."""
    protocol = _speaker(_detached_provider())

    await protocol.poll()
    protocol.on_native_group_update()

    assert protocol.available is False
    assert protocol.native_available is False
