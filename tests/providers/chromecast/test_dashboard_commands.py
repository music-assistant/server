"""Tests for the chromecast provider's dashboard registration and casting transport."""

from __future__ import annotations

import threading
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from music_assistant_models.dashboard import DashboardDevice
from music_assistant_models.enums import DashboardType
from music_assistant_models.errors import PlayerUnavailableError
from pychromecast.const import CAST_TYPE_CHROMECAST

from music_assistant.providers.chromecast.dashboard import ChromecastDashboards
from music_assistant.providers.chromecast.player import ChromecastPlayer
from music_assistant.providers.chromecast.provider import ChromecastProvider


def _make_dashboards() -> ChromecastDashboards:
    """Build a ChromecastDashboards instance with a fully mocked owning provider."""
    provider = MagicMock()
    provider.translation_owner = "provider.chromecast"
    provider.domain = "chromecast"
    provider.mass.closing = False
    # run_in_executor just calls the function inline, synchronously
    provider.mass.loop.run_in_executor = AsyncMock(
        side_effect=lambda _executor, func, *args: func(*args)
    )
    provider.mass.players.get_player.return_value = None
    return ChromecastDashboards(provider)


def _cast_info(cast_type: str = CAST_TYPE_CHROMECAST, name: str = "Living Room TV") -> MagicMock:
    info = MagicMock()
    info.cast_type = cast_type
    info.friendly_name = name
    return info


def _make_provider() -> ChromecastProvider:
    """Build a ChromecastProvider without running its (network-touching) __init__."""
    provider = ChromecastProvider.__new__(ChromecastProvider)
    provider.mass = MagicMock()
    provider.mass.closing = False
    provider.mass.loop.call_soon_threadsafe = MagicMock(side_effect=lambda func, *args: func(*args))
    provider.mass.players.get_player.return_value = None
    provider.mass.config.get_raw_player_config_value.return_value = True
    provider.logger = MagicMock()
    provider._discover_lock = threading.Lock()
    provider._pending_discoveries = set()
    provider.dashboards = MagicMock()
    return provider


### Registration on discovery


def test_register_ignores_non_video_capable_device() -> None:
    """Audio speakers/groups are not registered as dashboard endpoints."""
    dashboards = _make_dashboards()

    dashboards.register(uuid4(), _cast_info(cast_type="audio"))

    dashboards.mass.dashboard.register_dashboard_handler.assert_not_called()  # type: ignore[attr-defined]


def test_register_video_capable_device() -> None:
    """A video-capable Cast device registers as a dashboard endpoint."""
    dashboards = _make_dashboards()
    uuid = uuid4()

    dashboards.register(uuid, _cast_info(name="Living Room TV"))

    dashboards.mass.dashboard.register_dashboard_handler.assert_called_once()  # type: ignore[attr-defined]
    device = dashboards.mass.dashboard.register_dashboard_handler.call_args[0][0]  # type: ignore[attr-defined]
    assert device == DashboardDevice(
        dashboard_id=f"chromecast_{uuid}",
        name="Living Room TV",
        supported_types={DashboardType.PARTY, DashboardType.NOW_PLAYING},
        provider_domain_hint="chromecast",
    )


### Unregistration on removal


def test_unregister_calls_stored_unregister_callback() -> None:
    """Removing a discovered device unregisters it from the dashboard controller."""
    dashboards = _make_dashboards()
    uuid = uuid4()
    unregister_callback = MagicMock()
    dashboards.mass.dashboard.register_dashboard_handler.return_value = unregister_callback  # type: ignore[attr-defined]
    dashboards.register(uuid, _cast_info())

    dashboards.unregister(uuid)

    unregister_callback.assert_called_once()


def test_unregister_disconnects_cached_connection() -> None:
    """Removing a discovered device also drops its cached on-demand connection."""
    dashboards = _make_dashboards()
    uuid = uuid4()
    chromecast = MagicMock()
    dashboards._dashboard_connections[str(uuid)] = chromecast

    dashboards.unregister(uuid)

    assert not dashboards._dashboard_connections
    chromecast.disconnect.assert_called_once_with(0)


def test_unregister_unknown_device_is_noop() -> None:
    """Unregistering a device that was never registered does not raise."""
    dashboards = _make_dashboards()

    dashboards.unregister(uuid4())


### on_show: connection reuse and error wrapping


async def test_on_show_reuses_connected_player_cc() -> None:
    """A connected registered player's own Chromecast connection is reused."""
    dashboards = _make_dashboards()
    device_uuid = uuid4()
    connected_chromecast = MagicMock()
    connected_chromecast.socket_client.is_connected = True
    castplayer = MagicMock(spec=ChromecastPlayer)
    castplayer.cc = connected_chromecast
    dashboards.mass.players.get_player.return_value = castplayer  # type: ignore[attr-defined]
    url = "https://mass.example.com?path=%2Fparty"
    dashboards.mass.dashboard.resolve_dashboard_url = AsyncMock(return_value=url)  # type: ignore[method-assign]

    with patch(
        "music_assistant.providers.chromecast.dashboard.send_show_dashboard"
    ) as mock_send_show_dashboard:
        await dashboards._on_show(str(device_uuid), DashboardType.PARTY, None)

    mock_send_show_dashboard.assert_called_once_with(connected_chromecast, url)


async def test_on_show_reuses_cached_on_demand_connection() -> None:
    """A cached on-demand connection (no MA player) is reused instead of reconnecting."""
    dashboards = _make_dashboards()
    device_uuid = uuid4()
    connected_chromecast = MagicMock()
    connected_chromecast.socket_client.is_connected = True
    dashboards._dashboard_connections[str(device_uuid)] = connected_chromecast
    url = "https://mass.example.com?path=%2Fparty"
    dashboards.mass.dashboard.resolve_dashboard_url = AsyncMock(return_value=url)  # type: ignore[method-assign]

    with patch(
        "music_assistant.providers.chromecast.dashboard.send_show_dashboard"
    ) as mock_send_show_dashboard:
        await dashboards._on_show(str(device_uuid), DashboardType.PARTY, None)

    mock_send_show_dashboard.assert_called_once_with(connected_chromecast, url)


async def test_on_show_raises_for_unknown_device() -> None:
    """An unknown device_id (not in the browser's discovered devices) is rejected."""
    dashboards = _make_dashboards()
    dashboards.provider.browser = MagicMock(devices={})
    dashboards.mass.dashboard.resolve_dashboard_url = AsyncMock(  # type: ignore[method-assign]
        return_value="https://mass.example.com?path=%2Fparty"
    )

    with pytest.raises(PlayerUnavailableError):
        await dashboards._on_show(str(uuid4()), DashboardType.PARTY, None)


async def test_on_show_wraps_timeout_error() -> None:
    """A TimeoutError from the helper surfaces as PlayerUnavailableError, keeping its reason."""
    dashboards = _make_dashboards()
    device_uuid = uuid4()
    connected_chromecast = MagicMock()
    connected_chromecast.socket_client.is_connected = True
    connected_chromecast.name = "Living Room TV"
    dashboards._dashboard_connections[str(device_uuid)] = connected_chromecast
    dashboards.mass.dashboard.resolve_dashboard_url = AsyncMock(  # type: ignore[method-assign]
        return_value="https://mass.example.com?path=%2Fparty"
    )

    with (
        patch(
            "music_assistant.providers.chromecast.dashboard.send_show_dashboard",
            side_effect=TimeoutError("Launching app on Living Room TV failed"),
        ),
        pytest.raises(PlayerUnavailableError) as exc_info,
    ):
        await dashboards._on_show(str(device_uuid), DashboardType.PARTY, None)

    assert str(exc_info.value) == "Launching app on Living Room TV failed"
    assert exc_info.value.translation_key == "show_dashboard_failed"
    assert exc_info.value.translation_owner == "provider.chromecast"


### on_hide: no-op / eviction behavior


async def test_on_hide_noop_when_nothing_connected() -> None:
    """Hiding on a device with no registered player and no cached connection is a no-op."""
    dashboards = _make_dashboards()
    dashboards.provider.browser = MagicMock(devices={})

    with patch(
        "music_assistant.providers.chromecast.dashboard.send_hide_dashboard"
    ) as mock_send_hide_dashboard:
        await dashboards._on_hide(str(uuid4()))

    mock_send_hide_dashboard.assert_not_called()


async def test_on_hide_evicts_on_demand_connection() -> None:
    """Hiding disconnects and drops an on-demand connection cached for the device."""
    dashboards = _make_dashboards()
    device_uuid = uuid4()
    connected_chromecast = MagicMock()
    connected_chromecast.socket_client.is_connected = True
    dashboards._dashboard_connections[str(device_uuid)] = connected_chromecast

    with patch("music_assistant.providers.chromecast.dashboard.send_hide_dashboard"):
        await dashboards._on_hide(str(device_uuid))

    connected_chromecast.disconnect.assert_called_once_with(10)
    assert str(device_uuid) not in dashboards._dashboard_connections


async def test_on_hide_keeps_registered_player_connection() -> None:
    """Hiding on a live MA player's own connection must not disconnect it."""
    dashboards = _make_dashboards()
    device_uuid = uuid4()
    connected_chromecast = MagicMock()
    connected_chromecast.socket_client.is_connected = True
    castplayer = MagicMock(spec=ChromecastPlayer)
    castplayer.cc = connected_chromecast
    dashboards.mass.players.get_player.return_value = castplayer  # type: ignore[attr-defined]

    with patch("music_assistant.providers.chromecast.dashboard.send_hide_dashboard"):
        await dashboards._on_hide(str(device_uuid))

    connected_chromecast.disconnect.assert_not_called()


async def test_on_hide_with_stale_cache_entry_spares_player_connection() -> None:
    """A stale on-demand entry alongside a live player must not disconnect the player's cc."""
    dashboards = _make_dashboards()
    device_uuid = uuid4()
    player_cc = MagicMock()
    player_cc.socket_client.is_connected = True
    castplayer = MagicMock(spec=ChromecastPlayer)
    castplayer.cc = player_cc
    dashboards.mass.players.get_player.return_value = castplayer  # type: ignore[attr-defined]
    stale = MagicMock()
    dashboards._dashboard_connections[str(device_uuid)] = stale

    with patch("music_assistant.providers.chromecast.dashboard.send_hide_dashboard"):
        await dashboards._on_hide(str(device_uuid))

    player_cc.disconnect.assert_not_called()
    stale.disconnect.assert_called_once_with(10)
    assert str(device_uuid) not in dashboards._dashboard_connections


async def test_on_hide_logs_debug_when_nothing_was_showing() -> None:
    """A False result from send_hide_dashboard is logged at debug, not raised."""
    dashboards = _make_dashboards()
    device_uuid = uuid4()
    connected_chromecast = MagicMock()
    connected_chromecast.socket_client.is_connected = True
    dashboards._dashboard_connections[str(device_uuid)] = connected_chromecast

    with patch(
        "music_assistant.providers.chromecast.dashboard.send_hide_dashboard",
        return_value=False,
    ):
        await dashboards._on_hide(str(device_uuid))

    dashboards.logger.debug.assert_called_once()  # type: ignore[attr-defined]


### unload cleanup


async def test_unload_unregisters_all_and_disconnects_via_executor() -> None:
    """Unload unregisters every dashboard endpoint and disconnects cached connections."""
    dashboards = _make_dashboards()
    unregister_callback = MagicMock()
    dashboards.mass.dashboard.register_dashboard_handler.return_value = unregister_callback  # type: ignore[attr-defined]
    dashboards.register(uuid4(), _cast_info())
    chromecast = MagicMock()
    dashboards._dashboard_connections["on-demand-device"] = chromecast

    await dashboards.unload()

    unregister_callback.assert_called_once()
    chromecast.disconnect.assert_called_once_with(10)
    assert dashboards._dashboard_connections == {}


async def test_unload_disconnects_without_executor_when_closing() -> None:
    """During shutdown, disconnects happen synchronously instead of via the executor."""
    dashboards = _make_dashboards()
    dashboards.mass.closing = True  # type: ignore[misc]
    chromecast = MagicMock()
    dashboards._dashboard_connections["on-demand-device"] = chromecast

    await dashboards.unload()

    chromecast.disconnect.assert_called_once_with(0)


async def test_register_after_unload_is_noop() -> None:
    """A late discovery callback after unload must not resurrect a stale registration."""
    dashboards = _make_dashboards()

    await dashboards.unload()
    dashboards.register(uuid4(), _cast_info())

    dashboards.mass.dashboard.register_dashboard_handler.assert_not_called()  # type: ignore[attr-defined]


### Provider wiring: discovery lifecycle hooks


def test_discovery_registers_device_with_dashboards() -> None:
    """Discovering an already-registered player's Cast device still (re-)registers it."""
    provider = _make_provider()
    uuid = uuid4()
    disc_info = _cast_info()
    disc_info.uuid = uuid
    provider.browser = MagicMock(devices={uuid: disc_info})
    castplayer = MagicMock(spec=ChromecastPlayer)
    castplayer.available = True
    castplayer.cast_info = MagicMock(is_audio_group=False)
    castplayer.cc = MagicMock()  # discovery fast-path reads castplayer.cc.socket_client
    provider.mass.players.get_player.return_value = castplayer  # type: ignore[attr-defined]

    provider._on_chromecast_discovered(uuid, "service")

    provider.dashboards.register.assert_called_once_with(uuid, disc_info)  # type: ignore[attr-defined]


def test_discovery_registers_device_even_when_player_disabled() -> None:
    """A device disabled as a player is still registered as a dashboard endpoint."""
    provider = _make_provider()
    uuid = uuid4()
    disc_info = _cast_info()
    disc_info.uuid = uuid
    provider.browser = MagicMock(devices={uuid: disc_info})
    provider.mass.config.get_raw_player_config_value.return_value = False  # type: ignore[attr-defined]

    provider._on_chromecast_discovered(uuid, "service")

    provider.dashboards.register.assert_called_once_with(uuid, disc_info)  # type: ignore[attr-defined]


def test_removed_callback_unregisters_dashboard() -> None:
    """A Cast device removed from discovery is unregistered as a dashboard endpoint."""
    provider = _make_provider()
    uuid = uuid4()

    provider._on_chromecast_removed(uuid, "service", _cast_info())

    provider.dashboards.unregister.assert_called_once_with(uuid)  # type: ignore[attr-defined]
