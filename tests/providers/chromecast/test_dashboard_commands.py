"""Tests for the chromecast provider's dashboard casting API commands."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from music_assistant_models.errors import ActionUnavailable, PlayerUnavailableError

from music_assistant.providers.chromecast.constants import MASS_APP_ID
from music_assistant.providers.chromecast.dashboard_controller import MusicAssistantCastController
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
    provider._dashboard_connections = {}
    provider._dashboard_controllers = {}
    provider.get_config_value = MagicMock(return_value=MASS_APP_ID)  # type: ignore[method-assign]
    return provider


def _cast_info(cast_type: str, name: str) -> MagicMock:
    info = MagicMock()
    info.cast_type = cast_type
    info.friendly_name = name
    return info


async def test_get_display_devices_filters_video_capable() -> None:
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

    devices = await provider.get_display_devices()

    assert devices == [{"device_id": str(video_uuid), "name": "Living Room TV"}]


async def test_show_dashboard_raises_when_remote_access_disabled() -> None:
    """Casting a dashboard requires remote access to be enabled."""
    provider = _make_provider()
    provider.mass.webserver.remote_access.is_enabled = False  # type: ignore[misc]
    provider.mass.webserver.remote_access.remote_id = ""  # type: ignore[misc]

    with pytest.raises(ActionUnavailable):
        await provider.show_dashboard(str(uuid4()), "/party")


async def test_show_dashboard_raises_for_unknown_device() -> None:
    """An unknown device_id (not in the browser's discovered devices) is rejected."""
    provider = _make_provider()
    provider.mass.webserver.remote_access.is_enabled = True  # type: ignore[misc]
    provider.mass.webserver.remote_access.remote_id = "remote123"  # type: ignore[misc]
    provider.browser = MagicMock(devices={})

    with pytest.raises(PlayerUnavailableError):
        await provider.show_dashboard(str(uuid4()), "/party")


async def test_show_dashboard_happy_path() -> None:
    """A cast code is generated and the receiver app is launched on the resolved device."""
    provider = _make_provider()
    provider.mass.webserver.remote_access.is_enabled = True  # type: ignore[misc]
    provider.mass.webserver.remote_access.remote_id = "remote123"  # type: ignore[misc]
    provider.mass.version = "2.17.150"

    device_uuid = uuid4()
    connected_chromecast = MagicMock()
    connected_chromecast.socket_client.is_connected = True
    provider._dashboard_connections = {str(device_uuid): connected_chromecast}

    with (
        patch(
            "music_assistant.providers.chromecast.provider.get_cast_code",
            new=AsyncMock(return_value="code456"),
        ),
        patch.object(MusicAssistantCastController, "show_dashboard") as mock_show_dashboard,
    ):
        await provider.show_dashboard(str(device_uuid), "/party")

    mock_show_dashboard.assert_called_once_with("remote123", "code456", "/party", "2.17.150")
    connected_chromecast.register_handler.assert_called_once()
    controller = connected_chromecast.register_handler.call_args[0][0]
    assert isinstance(controller, MusicAssistantCastController)
    assert controller.supporting_app_id == MASS_APP_ID


async def test_show_dashboard_reuses_controller_per_device() -> None:
    """Repeat casts to the same device reuse the same controller instance."""
    provider = _make_provider()
    provider.mass.webserver.remote_access.is_enabled = True  # type: ignore[misc]
    provider.mass.webserver.remote_access.remote_id = "remote123"  # type: ignore[misc]
    provider.mass.version = "2.17.150"

    device_uuid = uuid4()
    connected_chromecast = MagicMock()
    connected_chromecast.socket_client.is_connected = True
    provider._dashboard_connections = {str(device_uuid): connected_chromecast}

    with (
        patch(
            "music_assistant.providers.chromecast.provider.get_cast_code",
            new=AsyncMock(return_value="code456"),
        ),
        patch.object(MusicAssistantCastController, "show_dashboard"),
    ):
        await provider.show_dashboard(str(device_uuid), "/party")
        await provider.show_dashboard(str(device_uuid), "/quiz")

    registered = [call.args[0] for call in connected_chromecast.register_handler.call_args_list]
    assert len(set(map(id, registered))) == 1
    assert provider._dashboard_controllers == {str(device_uuid): registered[0]}
    connected_chromecast.unregister_handler.assert_not_called()


async def test_show_dashboard_swaps_controller_on_app_id_change() -> None:
    """Changing the configured app id unregisters the old controller and registers a new one."""
    provider = _make_provider()
    provider.mass.webserver.remote_access.is_enabled = True  # type: ignore[misc]
    provider.mass.webserver.remote_access.remote_id = "remote123"  # type: ignore[misc]
    provider.mass.version = "2.17.150"

    device_uuid = uuid4()
    connected_chromecast = MagicMock()
    connected_chromecast.socket_client.is_connected = True
    provider._dashboard_connections = {str(device_uuid): connected_chromecast}

    with (
        patch(
            "music_assistant.providers.chromecast.provider.get_cast_code",
            new=AsyncMock(return_value="code456"),
        ),
        patch.object(MusicAssistantCastController, "show_dashboard"),
    ):
        await provider.show_dashboard(str(device_uuid), "/party")
        old_controller = provider._dashboard_controllers[str(device_uuid)]
        provider.get_config_value.return_value = "920426A9"  # type: ignore[attr-defined]
        await provider.show_dashboard(str(device_uuid), "/party")

    connected_chromecast.unregister_handler.assert_called_once_with(old_controller)
    new_controller = provider._dashboard_controllers[str(device_uuid)]
    assert new_controller is not old_controller
    assert new_controller.supporting_app_id == "920426A9"
