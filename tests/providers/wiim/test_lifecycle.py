"""Tests for an official WiiM device attached to its player on a running server."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import IdentifierType
from wiim.exceptions import WiimRequestException

from music_assistant.providers.wiim.constants import PLAYER_ID_PREFIX
from music_assistant.providers.wiim.grouping import NativeGroupCoordinator, NativeGroupRole
from music_assistant.providers.wiim.player import WiimPlayer
from music_assistant.providers.wiim.provider import WiimProvider

if TYPE_CHECKING:
    from collections.abc import Coroutine

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
        await scenario.register(audio)

    create = AsyncMock(side_effect=_sdk_device)
    with patch("music_assistant.providers.wiim.player.async_create_wiim_device", create):
        protocol = _speaker(provider)
        await scenario.mass.players.register(protocol)
        await scenario.settle()
        if move == "unlink":
            await scenario.mass.players.unlink_protocol(PROTOCOL_ID)
        else:
            await scenario.mass.players.unregister(PROTOCOL_ID)
            await scenario.settle()
            await scenario.mass.players.register(protocol)
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

    delays: list[float] = []

    async def _wait_for(awaitable: Coroutine[object, object, object], timeout: float) -> None:
        awaitable.close()
        delays.append(timeout)
        raise TimeoutError

    with (
        patch("music_assistant.providers.wiim.player.async_create_wiim_device", create),
        patch("music_assistant.providers.wiim.player.asyncio.wait_for", _wait_for),
    ):
        await protocol.on_attach(MagicMock())
        assert protocol._connect_task is not None
        await protocol._connect_task

    assert delays == [30]
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


async def test_a_detach_after_a_failed_setup_still_tears_down() -> None:
    """A setup that failed half way leaves a detach to release the group and the device."""
    provider = _detached_provider()
    provider.wiim_controller.add_device = AsyncMock(side_effect=RuntimeError("controller"))
    protocol = _speaker(provider)
    device = _sdk_device()
    with patch(
        "music_assistant.providers.wiim.player.async_create_wiim_device",
        AsyncMock(return_value=device),
    ):
        await protocol.on_attach(MagicMock())
        assert protocol._connect_task is not None
        await asyncio.wait([protocol._connect_task])

    await protocol.on_detach()

    provider.native_groups.unregister.assert_called_once_with(PROTOCOL_ID)
    device.disconnect.assert_awaited_once_with()
    assert device.rendering_control_event_callback is None


async def test_a_cancelled_detach_is_not_swallowed() -> None:
    """A detach that is itself cancelled while it ends the setup stops there."""
    protocol = _speaker(_detached_provider())
    creating = asyncio.Event()

    async def _create(*_args: object, **_kwargs: object) -> MagicMock:
        creating.set()
        await asyncio.Event().wait()
        return _sdk_device()

    with patch("music_assistant.providers.wiim.player.async_create_wiim_device", _create):
        await protocol.on_attach(MagicMock())
        await creating.wait()
        detach = asyncio.create_task(protocol.on_detach())
        await asyncio.sleep(0)
        detach.cancel()

        with pytest.raises(asyncio.CancelledError):
            await detach


async def test_a_speaker_moved_while_its_setup_backs_off_connects_at_its_new_location() -> None:
    """A speaker discovery finds at a new location is set up there at once, not after its delay."""
    provider = _detached_provider()
    protocol = _speaker(provider)
    device = _sdk_device()
    new_address = "192.168.1.81"
    new_location = f"http://{new_address}:49152/description.xml"
    attempts: list[tuple[str, object]] = []

    async def _create(location: str, *_args: object, **kwargs: object) -> MagicMock:
        attempts.append((location, kwargs["host"]))
        if location != new_location:
            raise WiimRequestException("unreachable")
        return device

    with patch("music_assistant.providers.wiim.player.async_create_wiim_device", _create):
        await protocol.on_attach(MagicMock())
        for _ in range(5):
            await asyncio.sleep(0)
        assert len(attempts) == 1
        await WiimProvider._reconcile_player_address(
            provider, protocol, new_address, MagicMock(udn=UDN), new_location
        )
        assert protocol._connect_task is not None
        await asyncio.wait_for(protocol._connect_task, 1)

    assert attempts == [
        (f"http://{IP_ADDRESS}:49152/description.xml", IP_ADDRESS),
        (new_location, new_address),
    ]
    assert protocol.device is device
    assert protocol.device_info.ip_address == new_address


async def test_a_follower_is_published_with_its_native_role_once_set_up() -> None:
    """The setup reads the native topology before the speaker shows as available."""
    provider = _detached_provider()
    provider.native_groups = NativeGroupCoordinator(provider)
    protocol = _speaker(provider)
    provider.get_protocol = MagicMock(return_value=protocol)
    provider.protocols = [protocol]
    client = MagicMock(capabilities={})
    client.get_device_group_info = AsyncMock(return_value=MagicMock(role="slave"))
    published: list[tuple[bool, NativeGroupRole]] = []

    def _update_state(*_args: object, **_kwargs: object) -> None:
        published.append((protocol.available, provider.native_groups.role_of(PROTOCOL_ID)))

    with (
        patch(
            "music_assistant.providers.wiim.player.async_create_wiim_device",
            AsyncMock(return_value=_sdk_device()),
        ),
        patch.object(protocol, "make_command_client", return_value=client),
        patch.object(protocol, "update_state", side_effect=_update_state),
    ):
        await protocol.on_attach(MagicMock())
        assert protocol._connect_task is not None
        await protocol._connect_task

    client.get_device_group_info.assert_awaited_once_with()
    first_available = next(state for state in published if state[0])
    assert first_available == (True, NativeGroupRole.FOLLOWER)


async def test_a_speaker_set_up_reports_the_model_of_its_sdk_device() -> None:
    """Model and manufacturer follow the SDK device once it is up, not the UPnP description."""
    provider = _detached_provider()
    upnp_device = MagicMock(
        friendly_name="Speaker A", model_name="WiiM", manufacturer=None, udn=UDN
    )
    protocol = WiimPlayer(
        provider, PROTOCOL_ID, upnp_device, f"http://{IP_ADDRESS}/description.xml", IP_ADDRESS
    )
    device = _sdk_device()
    device.model_name = "WiiM Pro Plus"
    device.manufacturer = "Linkplay"

    with patch(
        "music_assistant.providers.wiim.player.async_create_wiim_device",
        AsyncMock(return_value=device),
    ):
        await protocol.on_attach(MagicMock())
        assert protocol._connect_task is not None
        await protocol._connect_task

    assert protocol.device_info.model == "WiiM Pro Plus"
    assert protocol.device_info.manufacturer == "Linkplay"
    assert protocol.device_info.software_version == "4.8.1"
