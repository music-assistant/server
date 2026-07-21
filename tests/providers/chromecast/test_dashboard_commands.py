"""Tests for the chromecast provider's dashboard casting transport."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from music_assistant_models.dashboard import DashboardDevice
from music_assistant_models.errors import PlayerUnavailableError

from music_assistant.providers.chromecast.provider import ChromecastProvider


def _make_provider() -> ChromecastProvider:
    """Build a ChromecastProvider instance without running its (network-touching) __init__."""
    provider = ChromecastProvider.__new__(ChromecastProvider)
    provider.mass = MagicMock()
    # run_in_executor just calls the function inline, synchronously
    provider.mass.loop.run_in_executor = AsyncMock(
        side_effect=lambda _executor, func, *args: func(*args)
    )
    provider.mass.players.get_player.return_value = None
    provider.manifest = MagicMock(domain="chromecast")
    provider.config = MagicMock(instance_id="chromecast--test")
    provider._dashboard_connections = {}
    return provider


def _cast_info(cast_type: str, name: str) -> MagicMock:
    info = MagicMock()
    info.cast_type = cast_type
    info.friendly_name = name
    return info


async def test_get_dashboard_devices_filters_video_capable() -> None:
    """Only the video-capable (chromecast) device is returned, audio/group devices are not."""
    provider = _make_provider()
    video_uuid, audio_uuid, group_uuid = uuid4(), uuid4(), uuid4()
    provider.browser = MagicMock(
        devices={
            video_uuid: _cast_info("cast", "Living Room TV"),
            audio_uuid: _cast_info("audio", "Kitchen Speaker"),
            group_uuid: _cast_info("group", "Whole House"),
        }
    )

    devices = await provider.get_dashboard_devices()

    assert devices == [
        DashboardDevice(
            device_id=str(video_uuid),
            provider_instance=provider.instance_id,
            name="Living Room TV",
            player_id=None,
        )
    ]


async def test_get_dashboard_devices_links_registered_player() -> None:
    """A display device that is also a registered MA player carries its player_id."""
    provider = _make_provider()
    video_uuid = uuid4()
    provider.browser = MagicMock(devices={video_uuid: _cast_info("cast", "Living Room TV")})
    provider.mass.players.get_player.side_effect = (  # type: ignore[attr-defined]
        lambda player_id: MagicMock() if player_id == str(video_uuid) else None
    )

    devices = await provider.get_dashboard_devices()

    assert devices[0].player_id == str(video_uuid)


async def test_show_dashboard_raises_for_unknown_device() -> None:
    """An unknown device_id (not in the browser's discovered devices) is rejected."""
    provider = _make_provider()
    provider.browser = MagicMock(devices={})

    with pytest.raises(PlayerUnavailableError):
        await provider.show_dashboard(str(uuid4()), "https://mass.example.com?path=%2Fparty")


async def test_show_dashboard_delegates_to_helper() -> None:
    """The resolved chromecast and url are forwarded to the helper transport."""
    provider = _make_provider()

    device_uuid = uuid4()
    connected_chromecast = MagicMock()
    connected_chromecast.socket_client.is_connected = True
    provider._dashboard_connections = {str(device_uuid): connected_chromecast}
    url = "https://mass.example.com?path=%2Fparty"

    with patch(
        "music_assistant.providers.chromecast.provider.send_show_dashboard"
    ) as mock_send_show_dashboard:
        await provider.show_dashboard(str(device_uuid), url)

    mock_send_show_dashboard.assert_called_once_with(connected_chromecast, url)


async def test_show_dashboard_wraps_timeout_error() -> None:
    """A TimeoutError from the helper surfaces as a PlayerUnavailableError."""
    provider = _make_provider()

    device_uuid = uuid4()
    connected_chromecast = MagicMock()
    connected_chromecast.socket_client.is_connected = True
    connected_chromecast.name = "Living Room TV"
    provider._dashboard_connections = {str(device_uuid): connected_chromecast}

    with (
        patch(
            "music_assistant.providers.chromecast.provider.send_show_dashboard",
            side_effect=TimeoutError,
        ),
        pytest.raises(PlayerUnavailableError),
    ):
        await provider.show_dashboard(str(device_uuid), "https://mass.example.com?path=%2Fparty")


async def test_hide_dashboard_delegates_to_helper() -> None:
    """The resolved chromecast is passed to the hide helper via the executor."""
    provider = _make_provider()

    device_uuid = uuid4()
    connected_chromecast = MagicMock()
    connected_chromecast.socket_client.is_connected = True
    provider._dashboard_connections = {str(device_uuid): connected_chromecast}

    with patch(
        "music_assistant.providers.chromecast.provider.send_hide_dashboard"
    ) as mock_send_hide_dashboard:
        await provider.hide_dashboard(str(device_uuid))

    mock_send_hide_dashboard.assert_called_once_with(connected_chromecast)


async def test_hide_dashboard_no_op_for_device_without_existing_connection() -> None:
    """Hiding on a device with no registered player and no cached connection is a no-op."""
    provider = _make_provider()
    provider.browser = MagicMock(devices={})

    with patch(
        "music_assistant.providers.chromecast.provider.send_hide_dashboard"
    ) as mock_send_hide_dashboard:
        await provider.hide_dashboard(str(uuid4()))

    mock_send_hide_dashboard.assert_not_called()


async def test_hide_dashboard_evicts_on_demand_connection() -> None:
    """Hiding disconnects and drops an on-demand connection cached for the device."""
    provider = _make_provider()

    device_uuid = uuid4()
    connected_chromecast = MagicMock()
    connected_chromecast.socket_client.is_connected = True
    provider._dashboard_connections = {str(device_uuid): connected_chromecast}

    with patch("music_assistant.providers.chromecast.provider.send_hide_dashboard"):
        await provider.hide_dashboard(str(device_uuid))

    connected_chromecast.disconnect.assert_called_once_with(10)
    assert str(device_uuid) not in provider._dashboard_connections


async def test_hide_dashboard_logs_debug_when_nothing_was_showing() -> None:
    """A False result from send_hide_dashboard is logged at debug, not raised."""
    provider = _make_provider()

    device_uuid = uuid4()
    connected_chromecast = MagicMock()
    connected_chromecast.socket_client.is_connected = True
    provider._dashboard_connections = {str(device_uuid): connected_chromecast}
    provider.logger = MagicMock()

    with patch(
        "music_assistant.providers.chromecast.provider.send_hide_dashboard",
        return_value=False,
    ):
        await provider.hide_dashboard(str(device_uuid))

    provider.logger.debug.assert_called_once()
