"""Tests for the Fully Kiosk provider's dashboard registration and display control."""

from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fullykiosk.exceptions import FullyKioskError
from music_assistant_models.enums import DashboardType
from music_assistant_models.errors import PlayerCommandFailed, PlayerUnavailableError

from music_assistant.constants import CONF_SSL_FINGERPRINT, CONF_USE_SSL, CONF_VERIFY_SSL
from music_assistant.providers.fully_kiosk.dashboard import FullyKioskDashboards
from music_assistant.providers.fully_kiosk.player import FullyKioskPlayer
from music_assistant.providers.fully_kiosk.provider import FullyKioskProvider
from tests.common import MockProvider, create_mock_config

PLAYER_ID = "fully_kiosk_10.0.0.5_2323"


def _make_dashboards(player: FullyKioskPlayer | None = None) -> FullyKioskDashboards:
    """Build a FullyKioskDashboards instance whose mocked players controller returns player."""
    provider = MagicMock()
    provider.domain = "fully_kiosk"
    provider.translation_owner = "provider.fully_kiosk"
    provider.mass.players.get_player.return_value = player
    provider.mass.dashboard.register_dashboard_handler.return_value = MagicMock()
    return FullyKioskDashboards(provider)


def _make_player(
    *,
    player_id: str = PLAYER_ID,
    display_name: str = "Living Room Kiosk",
    connected: bool = True,
) -> FullyKioskPlayer:
    """Build a real FullyKioskPlayer, optionally with a connected client mock."""
    provider = MockProvider("fully_kiosk", instance_id="fully_kiosk--1")
    provider.mass.config.get_base_player_config.return_value = create_mock_config(display_name)
    player = FullyKioskPlayer(provider=provider, player_id=player_id, host="10.0.0.5")  # type: ignore[arg-type]
    player._attr_name = display_name
    if connected:
        client = MagicMock()
        client.screenOn = AsyncMock()
        client.stopScreensaver = AsyncMock()
        client.toForeground = AsyncMock()
        client.loadUrl = AsyncMock()
        client.loadStartUrl = AsyncMock()
        player.fully_kiosk = client
    return player


def _make_connectable_player(*, player_id: str = PLAYER_ID) -> tuple[FullyKioskPlayer, MagicMock]:
    """Build a real player with a mocked provider.dashboards, ready to drive _connect()/poll()."""
    provider = MockProvider("fully_kiosk", instance_id="fully_kiosk--1")
    provider.dashboards = MagicMock()
    provider.mass.config.get_base_player_config.return_value = create_mock_config("Kiosk")
    player = FullyKioskPlayer(provider=provider, player_id=player_id, host="10.0.0.5")  # type: ignore[arg-type]
    player.get_setup_value = MagicMock(return_value="secret")  # type: ignore[method-assign]
    ssl_config = {CONF_USE_SSL: False, CONF_VERIFY_SSL: True, CONF_SSL_FINGERPRINT: ""}
    player.config.get_value = MagicMock(side_effect=ssl_config.get)  # type: ignore[method-assign]
    return player, provider.dashboards


def _make_client(*, fails: bool = False) -> MagicMock:
    """Build a FullyKiosk client stub for patching into player._connect()."""
    client = MagicMock()
    client.getDeviceInfo = AsyncMock(
        side_effect=FullyKioskError("Error", "boom") if fails else None
    )
    client.deviceInfo = {}
    return client


def _client_of(player: FullyKioskPlayer) -> MagicMock:
    """Return a connected player's client mock, narrowing away the FullyKiosk | None type."""
    assert player.fully_kiosk is not None
    return cast("MagicMock", player.fully_kiosk)


### Player connect/poll wiring


async def test_connect_registers_dashboard_endpoint() -> None:
    """A successful connect registers the player as a dashboard endpoint."""
    player, dashboards = _make_connectable_player()
    client = _make_client()
    with patch("music_assistant.providers.fully_kiosk.player.FullyKiosk", return_value=client):
        await player._connect()

    assert player.available is True
    dashboards.register.assert_called_once_with(player)


async def test_connect_registers_dashboard_endpoint_with_synced_name() -> None:
    """A successful connect registers under the device's synced name, not the placeholder."""
    player, dashboards = _make_connectable_player()
    client = _make_client()
    client.deviceInfo = {"deviceName": "Kitchen Tablet"}
    captured_names: list[str] = []
    dashboards.register.side_effect = lambda registered_player: captured_names.append(
        registered_player.display_name
    )
    # a real config change (e.g. the password being set) clears the cached display_name
    # via set_config() before on_config_updated()/_connect() runs; mirror that here
    player.set_config(player.config)
    with patch("music_assistant.providers.fully_kiosk.player.FullyKiosk", return_value=client):
        await player._connect()

    assert captured_names == ["Kitchen Tablet"]


async def test_connect_fingerprint_without_ssl_unregisters_dashboard_endpoint() -> None:
    """A config change to a fingerprint without SSL drops any existing dashboard endpoint."""
    player, dashboards = _make_connectable_player()
    client = _make_client()
    with patch("music_assistant.providers.fully_kiosk.player.FullyKiosk", return_value=client):
        await player._connect()
    dashboards.reset_mock()

    ssl_config = {CONF_USE_SSL: False, CONF_VERIFY_SSL: True, CONF_SSL_FINGERPRINT: "aa:bb"}
    player.config.get_value = MagicMock(side_effect=ssl_config.get)  # type: ignore[method-assign]

    await player._connect()

    assert player.available is False
    dashboards.unregister.assert_called_once_with(PLAYER_ID)


async def test_poll_failure_unregisters_then_reconnect_reregisters() -> None:
    """A poll failure drops the dashboard endpoint; a subsequent reconnect re-registers it."""
    player, dashboards = _make_connectable_player()
    client = _make_client()
    with patch("music_assistant.providers.fully_kiosk.player.FullyKiosk", return_value=client):
        await player._connect()
        dashboards.reset_mock()

        client.getDeviceInfo.side_effect = FullyKioskError("Error", "boom")
        await player.poll()
        unavailable = player.available
        assert unavailable is False
        dashboards.unregister.assert_called_once_with(PLAYER_ID)

        client.getDeviceInfo.side_effect = None
        await player.poll()  # fully_kiosk is None, so poll() reconnects

    reconnected = player.available
    assert reconnected is True
    dashboards.register.assert_called_once_with(player)


async def test_connect_failure_unregisters_dashboard_endpoint() -> None:
    """A failed (re)connect drops any existing dashboard endpoint registration."""
    player, dashboards = _make_connectable_player()
    client = _make_client(fails=True)
    with patch("music_assistant.providers.fully_kiosk.player.FullyKiosk", return_value=client):
        await player._connect()

    assert player.available is False
    dashboards.unregister.assert_called_once_with(PLAYER_ID)


async def test_password_cleared_unregisters_dashboard_endpoint() -> None:
    """Clearing the password drops the dashboard endpoint without a network call."""
    player, dashboards = _make_connectable_player()
    client = _make_client()
    with patch("music_assistant.providers.fully_kiosk.player.FullyKiosk", return_value=client):
        await player._connect()
    dashboards.reset_mock()

    player.get_setup_value = MagicMock(return_value=None)  # type: ignore[method-assign]
    await player.on_config_updated()

    assert player.available is False
    dashboards.unregister.assert_called_once_with(PLAYER_ID)


async def test_on_unload_unregisters_dashboard_endpoint() -> None:
    """Unloading the player drops its dashboard endpoint registration."""
    player, dashboards = _make_connectable_player()

    await player.on_unload()

    dashboards.unregister.assert_called_once_with(PLAYER_ID)


### Provider unload


async def test_provider_unload_awaits_dashboards_unload() -> None:
    """Provider unload tears down every dashboard endpoint it registered."""
    provider = FullyKioskProvider.__new__(FullyKioskProvider)
    provider.dashboards = AsyncMock()

    await provider.unload()

    provider.dashboards.unload.assert_awaited_once()


### Registration lifecycle


def test_register_creates_dashboard_endpoint_with_all_types() -> None:
    """A connected player registers a dashboard endpoint supporting all three types."""
    player = _make_player()
    dashboards = _make_dashboards(player)

    dashboards.register(player)

    handler = dashboards.mass.dashboard.register_dashboard_handler
    handler.assert_called_once()  # type: ignore[attr-defined]
    device = handler.call_args[0][0]  # type: ignore[attr-defined]
    assert device.dashboard_id == PLAYER_ID
    assert device.name == "Living Room Kiosk"
    assert device.supported_types == {
        DashboardType.NOW_PLAYING,
        DashboardType.PARTY,
        DashboardType.MUSIC_QUIZ,
    }
    assert device.provider_domain_hint == "fully_kiosk"
    assert PLAYER_ID in dashboards._unregister_callbacks


def test_register_is_noop_after_unload() -> None:
    """A late register call after unload must not resurrect an endpoint."""
    player = _make_player()
    dashboards = _make_dashboards(player)
    dashboards._unloaded = True

    dashboards.register(player)

    dashboards.mass.dashboard.register_dashboard_handler.assert_not_called()  # type: ignore[attr-defined]


@pytest.mark.parametrize("other_player", [False, True])
def test_register_is_noop_for_removed_player(other_player: bool) -> None:
    """A register call for a player the players controller no longer knows about is a no-op."""
    player = _make_player()
    current: FullyKioskPlayer | None = _make_player() if other_player else None
    dashboards = _make_dashboards(current)

    dashboards.register(player)

    dashboards.mass.dashboard.register_dashboard_handler.assert_not_called()  # type: ignore[attr-defined]


def test_unregister_calls_callback_and_reregister_creates_new_one() -> None:
    """Connection loss unregisters the endpoint; a subsequent reconnect re-registers it."""
    player = _make_player()
    dashboards = _make_dashboards(player)
    unregister = MagicMock()
    dashboards.mass.dashboard.register_dashboard_handler.return_value = unregister  # type: ignore[attr-defined]
    dashboards.register(player)
    assert PLAYER_ID in dashboards._unregister_callbacks

    dashboards.unregister(PLAYER_ID)

    unregister.assert_called_once()
    assert PLAYER_ID not in dashboards._unregister_callbacks

    # reconnect re-registers a fresh endpoint
    dashboards.register(player)
    assert PLAYER_ID in dashboards._unregister_callbacks
    assert dashboards.mass.dashboard.register_dashboard_handler.call_count == 2  # type: ignore[attr-defined]


def test_unregister_unknown_player_is_noop() -> None:
    """Unregistering a player id that was never registered does nothing."""
    dashboards = _make_dashboards()

    dashboards.unregister(PLAYER_ID)  # must not raise


async def test_unload_unregisters_all_endpoints() -> None:
    """Unload unregisters every endpoint and blocks further registration."""
    player = _make_player()
    dashboards = _make_dashboards(player)
    unregister = MagicMock()
    dashboards.mass.dashboard.register_dashboard_handler.return_value = unregister  # type: ignore[attr-defined]
    dashboards.register(player)

    await dashboards.unload()

    unregister.assert_called_once()
    assert dashboards._unloaded
    assert not dashboards._unregister_callbacks

    dashboards.register(player)
    dashboards.mass.dashboard.register_dashboard_handler.assert_called_once()  # type: ignore[attr-defined]


### on_show


async def test_on_show_wakes_and_loads_url_in_order() -> None:
    """on_show resolves the local url and drives the device in the documented order."""
    dashboards = _make_dashboards()
    player = _make_player()
    dashboards.mass.players.get_player.return_value = player  # type: ignore[attr-defined]
    target = "http://192.168.1.10:8095/?dashboard=ABC123&path=%2Fparty"
    dashboards.mass.dashboard.resolve_dashboard_url = AsyncMock(return_value=target)  # type: ignore[method-assign]
    client = _client_of(player)
    order = MagicMock()
    order.attach_mock(client.screenOn, "screen_on")
    order.attach_mock(client.stopScreensaver, "stop_screensaver")
    order.attach_mock(client.toForeground, "to_foreground")
    order.attach_mock(client.loadUrl, "load_url")

    await dashboards._on_show(PLAYER_ID, DashboardType.PARTY, None)

    dashboards.mass.dashboard.resolve_dashboard_url.assert_awaited_once_with(
        DashboardType.PARTY, None, dashboard_id=PLAYER_ID, prefer_local=True
    )
    assert [call[0] for call in order.mock_calls] == [
        "screen_on",
        "stop_screensaver",
        "to_foreground",
        "load_url",
    ]
    client.loadUrl.assert_awaited_once_with(target)


async def test_on_show_url_carries_the_endpoints_dashboard_id() -> None:
    """The url identifies this endpoint's session, so its viewer resolves the caster's prefs."""
    dashboards = _make_dashboards()
    player = _make_player()
    dashboards.mass.players.get_player.return_value = player  # type: ignore[attr-defined]
    dashboards.mass.dashboard.resolve_dashboard_url = AsyncMock(return_value="http://host/")  # type: ignore[method-assign]

    await dashboards._on_show(PLAYER_ID, DashboardType.NOW_PLAYING, "target_player")

    dashboards.mass.dashboard.resolve_dashboard_url.assert_awaited_once_with(
        DashboardType.NOW_PLAYING, "target_player", dashboard_id=PLAYER_ID, prefer_local=True
    )


async def test_on_show_missing_player_raises_unavailable() -> None:
    """on_show for a player that vanished raises PlayerUnavailableError."""
    dashboards = _make_dashboards()
    dashboards.mass.players.get_player.return_value = None  # type: ignore[attr-defined]

    with pytest.raises(PlayerUnavailableError):
        await dashboards._on_show(PLAYER_ID, DashboardType.PARTY, None)


async def test_on_show_disconnected_player_raises_unavailable() -> None:
    """on_show for a player without an active client connection raises PlayerUnavailableError."""
    dashboards = _make_dashboards()
    player = _make_player(connected=False)
    dashboards.mass.players.get_player.return_value = player  # type: ignore[attr-defined]

    with pytest.raises(PlayerUnavailableError):
        await dashboards._on_show(PLAYER_ID, DashboardType.PARTY, None)


async def test_on_show_device_error_raises_translated_unavailable_without_leaking_url() -> None:
    """A device-side failure surfaces as a translated error that never echoes the url."""
    dashboards = _make_dashboards()
    player = _make_player(display_name="Living Room Kiosk")
    dashboards.mass.players.get_player.return_value = player  # type: ignore[attr-defined]
    target = "http://192.168.1.10:8095/?dashboard=SECRETCODE&path=%2Fparty"
    dashboards.mass.dashboard.resolve_dashboard_url = AsyncMock(return_value=target)  # type: ignore[method-assign]
    _client_of(player).toForeground.side_effect = FullyKioskError("Error", "boom")

    with pytest.raises(PlayerUnavailableError) as exc_info:
        await dashboards._on_show(PLAYER_ID, DashboardType.PARTY, None)

    assert exc_info.value.translation_key == "show_dashboard_failed"
    assert exc_info.value.translation_owner == "provider.fully_kiosk"
    assert exc_info.value.translation_args == ["Living Room Kiosk"]
    assert "SECRETCODE" not in str(exc_info.value)
    assert target not in str(exc_info.value)
    assert exc_info.value.__cause__ is None


### on_hide


async def test_on_hide_loads_start_url() -> None:
    """on_hide returns the kiosk to its configured start page."""
    dashboards = _make_dashboards()
    player = _make_player()
    dashboards.mass.players.get_player.return_value = player  # type: ignore[attr-defined]

    await dashboards._on_hide(PLAYER_ID)

    _client_of(player).loadStartUrl.assert_awaited_once()


async def test_on_hide_disconnected_player_is_noop() -> None:
    """on_hide for an already-disconnected player is a graceful no-op."""
    dashboards = _make_dashboards()
    player = _make_player(connected=False)
    dashboards.mass.players.get_player.return_value = player  # type: ignore[attr-defined]

    await dashboards._on_hide(PLAYER_ID)  # must not raise


async def test_on_hide_missing_player_is_noop() -> None:
    """on_hide for a player that vanished is a graceful no-op."""
    dashboards = _make_dashboards()
    dashboards.mass.players.get_player.return_value = None  # type: ignore[attr-defined]

    await dashboards._on_hide(PLAYER_ID)  # must not raise


async def test_on_hide_device_error_raises_command_failed() -> None:
    """A device-side failure while hiding surfaces as PlayerCommandFailed."""
    dashboards = _make_dashboards()
    player = _make_player()
    dashboards.mass.players.get_player.return_value = player  # type: ignore[attr-defined]
    _client_of(player).loadStartUrl.side_effect = FullyKioskError("Error", "boom")

    with pytest.raises(PlayerCommandFailed) as exc_info:
        await dashboards._on_hide(PLAYER_ID)

    assert exc_info.value.__cause__ is None
