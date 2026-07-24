"""Tests for the AirPlay provider's dashboard registration and tvOS app launching."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import DashboardType
from music_assistant_models.errors import PlayerCommandFailed, PlayerUnavailableError

from music_assistant.controllers.dashboard.controller import DashboardController
from music_assistant.providers.airplay.constants import CONF_EXPOSE_DASHBOARD, TVOS_APP_BUNDLE_ID
from music_assistant.providers.airplay.control_player import AirPlayControlPlayer
from music_assistant.providers.airplay.dashboard import AirPlayDashboards
from music_assistant.providers.airplay.player import GenericAirPlayPlayer

PLAYER_ID = "ap1234567890ab"


def _make_dashboards() -> AirPlayDashboards:
    """Build an AirPlayDashboards instance with a fully mocked owning provider."""
    provider = MagicMock()
    provider.domain = "airplay"
    provider.translation_owner = "provider.airplay"
    provider.mass.players.get_player.return_value = None
    provider.mass.config.get_raw_player_config_value.return_value = True
    provider.mass.dashboard.register_dashboard_handler.return_value = MagicMock()
    return AirPlayDashboards(provider)


def _make_player(
    *,
    player_id: str = PLAYER_ID,
    manufacturer: str = "Apple",
    model: str = "Apple TV 4K",
    available: bool = True,
    enabled: bool = True,
    companion_connected: bool = True,
    installed_apps: frozenset[str] | None = frozenset({TVOS_APP_BUNDLE_ID}),
    display_name: str = "Living Room",
) -> MagicMock:
    """Build a control-player mock with configurable dashboard-eligibility state."""
    player = MagicMock(spec=AirPlayControlPlayer)
    player.player_id = player_id
    player.display_name = display_name
    player.available = available
    player.enabled = enabled
    player.companion_connected = companion_connected
    player.device_info = MagicMock(manufacturer=manufacturer, model=model)
    player.async_list_installed_app_ids = AsyncMock(return_value=installed_apps)
    player.async_launch_app = AsyncMock()
    player.wake = AsyncMock()
    return player


### Eligibility


@pytest.mark.parametrize(
    ("kwargs", "eligible"),
    [
        ({}, True),
        ({"model": "HomePod Mini"}, False),
        ({"companion_connected": False}, False),
        ({"installed_apps": frozenset({"com.other.app"})}, False),
        ({"installed_apps": None}, False),
        ({"enabled": False}, False),
        ({"available": False}, False),
    ],
    ids=[
        "eligible-apple-tv",
        "non-apple-tv-model",
        "no-companion-connection",
        "app-not-installed",
        "app-list-unavailable",
        "player-disabled",
        "player-unavailable",
    ],
)
async def test_eligibility_matrix(kwargs: dict[str, object], eligible: bool) -> None:
    """Only an enabled, available, Companion-connected Apple TV with the app is exposed."""
    dashboards = _make_dashboards()
    player = _make_player(**kwargs)  # type: ignore[arg-type]
    dashboards.mass.players.get_player.return_value = player  # type: ignore[attr-defined]

    await dashboards._async_reconcile(PLAYER_ID)

    assert (PLAYER_ID in dashboards._unregister_callbacks) is eligible


async def test_config_disabled_is_ineligible() -> None:
    """Disabling dashboard exposure in the player config unregisters the endpoint."""
    dashboards = _make_dashboards()
    player = _make_player()
    dashboards.mass.players.get_player.return_value = player  # type: ignore[attr-defined]
    dashboards.mass.config.get_raw_player_config_value.side_effect = (  # type: ignore[attr-defined]
        lambda _player_id, key, default=None: False if key == CONF_EXPOSE_DASHBOARD else default
    )

    await dashboards._async_reconcile(PLAYER_ID)

    assert PLAYER_ID not in dashboards._unregister_callbacks


async def test_protocol_player_is_ineligible() -> None:
    """A non-control (PROTOCOL) AirPlay player is never a dashboard endpoint."""
    dashboards = _make_dashboards()
    dashboards.mass.players.get_player.return_value = MagicMock(  # type: ignore[attr-defined]
        spec=GenericAirPlayPlayer
    )

    await dashboards._async_reconcile(PLAYER_ID)

    assert PLAYER_ID not in dashboards._unregister_callbacks


async def test_missing_player_is_ineligible() -> None:
    """Reconciling a player that no longer exists drops any registration."""
    dashboards = _make_dashboards()
    dashboards.mass.players.get_player.return_value = None  # type: ignore[attr-defined]

    await dashboards._async_reconcile(PLAYER_ID)

    assert PLAYER_ID not in dashboards._unregister_callbacks


### Registration lifecycle


async def test_eligible_player_registers_once() -> None:
    """An eligible Apple TV registers exactly one dashboard endpoint."""
    dashboards = _make_dashboards()
    player = _make_player(display_name="Living Room")
    dashboards.mass.players.get_player.return_value = player  # type: ignore[attr-defined]

    await dashboards._async_reconcile(PLAYER_ID)

    handler = dashboards.mass.dashboard.register_dashboard_handler
    handler.assert_called_once()  # type: ignore[attr-defined]
    device = handler.call_args[0][0]  # type: ignore[attr-defined]
    assert device.dashboard_id == f"airplay_{PLAYER_ID}"
    assert device.name == "Living Room"
    assert device.supported_types == {DashboardType.NOW_PLAYING}
    assert device.provider_domain_hint == "airplay"


async def test_availability_loss_unregisters() -> None:
    """Losing availability after registration unregisters the endpoint."""
    dashboards = _make_dashboards()
    player = _make_player()
    unregister = MagicMock()
    dashboards.mass.dashboard.register_dashboard_handler.return_value = unregister  # type: ignore[attr-defined]
    dashboards.mass.players.get_player.return_value = player  # type: ignore[attr-defined]
    await dashboards._async_reconcile(PLAYER_ID)
    assert PLAYER_ID in dashboards._unregister_callbacks

    player.available = False
    await dashboards._async_reconcile(PLAYER_ID)

    unregister.assert_called_once()
    assert PLAYER_ID not in dashboards._unregister_callbacks


async def test_unregister_calls_callback_and_clears_state() -> None:
    """Unregister drops the controller registration and all cached state."""
    dashboards = _make_dashboards()
    player = _make_player()
    unregister = MagicMock()
    dashboards.mass.dashboard.register_dashboard_handler.return_value = unregister  # type: ignore[attr-defined]
    dashboards.mass.players.get_player.return_value = player  # type: ignore[attr-defined]
    await dashboards._async_reconcile(PLAYER_ID)

    dashboards.unregister(PLAYER_ID)

    unregister.assert_called_once()
    assert PLAYER_ID not in dashboards._unregister_callbacks
    assert PLAYER_ID not in dashboards._installed_apps
    assert PLAYER_ID not in dashboards._companion_connected


async def test_unload_unregisters_all_endpoints() -> None:
    """Unload unregisters every endpoint and blocks further scheduling."""
    dashboards = _make_dashboards()
    player = _make_player()
    unregister = MagicMock()
    dashboards.mass.dashboard.register_dashboard_handler.return_value = unregister  # type: ignore[attr-defined]
    dashboards.mass.players.get_player.return_value = player  # type: ignore[attr-defined]
    await dashboards._async_reconcile(PLAYER_ID)

    await dashboards.unload()

    unregister.assert_called_once()
    assert dashboards._unloaded
    assert not dashboards._unregister_callbacks
    dashboards.reconcile(PLAYER_ID)
    dashboards.mass.create_task.assert_not_called()  # type: ignore[attr-defined]


async def test_app_list_refetched_on_companion_reconnect() -> None:
    """The installed-app cache is reused between reconciles but refreshed on a reconnect."""
    dashboards = _make_dashboards()
    player = _make_player()
    dashboards.mass.players.get_player.return_value = player  # type: ignore[attr-defined]

    await dashboards._async_reconcile(PLAYER_ID)
    assert player.async_list_installed_app_ids.await_count == 1
    assert PLAYER_ID in dashboards._unregister_callbacks

    # a second reconcile with no connection change reuses the cache
    await dashboards._async_reconcile(PLAYER_ID)
    assert player.async_list_installed_app_ids.await_count == 1

    # a Companion drop unregisters and invalidates the cache
    player.companion_connected = False
    await dashboards._async_reconcile(PLAYER_ID)
    assert PLAYER_ID not in dashboards._unregister_callbacks

    # the reconnect edge refetches the app list
    player.companion_connected = True
    await dashboards._async_reconcile(PLAYER_ID)
    assert player.async_list_installed_app_ids.await_count == 2
    assert PLAYER_ID in dashboards._unregister_callbacks


### setup_player / reconcile scheduling


def test_setup_player_wires_companion_callback() -> None:
    """setup_player schedules a reconcile and wires the Companion state callback."""
    dashboards = _make_dashboards()
    player = _make_player()

    with patch.object(dashboards, "reconcile") as mock_reconcile:
        dashboards.setup_player(player)
        mock_reconcile.assert_called_once_with(player.player_id)
        # the wired callback triggers a further reconcile
        assert player.on_companion_state_change is not None
        player.on_companion_state_change()
        assert mock_reconcile.call_count == 2


def test_reconcile_schedules_background_task() -> None:
    """Reconcile schedules a deduplicated background reconcile task."""
    dashboards = _make_dashboards()

    dashboards.reconcile(PLAYER_ID)

    create_task = dashboards.mass.create_task
    create_task.assert_called_once()  # type: ignore[attr-defined]
    args, kwargs = create_task.call_args  # type: ignore[attr-defined]
    assert args[0] == dashboards._async_reconcile
    assert args[1] == PLAYER_ID
    assert kwargs["abort_existing"] is True


### on_show / on_hide


async def test_on_show_builds_contract_launch_uri() -> None:
    """on_show resolves the local url and launches the exact contract launch URI."""
    dashboards = _make_dashboards()
    player = _make_player(player_id=PLAYER_ID, display_name="Living Room")
    dashboards.mass.players.get_player.return_value = player  # type: ignore[attr-defined]
    target = (
        "http://192.168.1.10:8095/?dashboard=ABC123&path=%2Fnow-playing%3Fplayer%3Dap1234567890ab"
    )
    dashboards.mass.dashboard.resolve_dashboard_url = AsyncMock(return_value=target)  # type: ignore[method-assign]

    await dashboards._on_show(PLAYER_ID, DashboardType.NOW_PLAYING, PLAYER_ID)

    dashboards.mass.dashboard.resolve_dashboard_url.assert_awaited_once_with(
        DashboardType.NOW_PLAYING, PLAYER_ID, prefer_local=True
    )
    expected = (
        "musicassistant://dashboard/show?v=1&type=now_playing"
        "&target=http%3A%2F%2F192.168.1.10%3A8095%2F%3Fdashboard%3DABC123"
        "%26path%3D%252Fnow-playing%253Fplayer%253Dap1234567890ab"
        "&dashboard_id=airplay_ap1234567890ab"
    )
    player.async_launch_app.assert_awaited_once_with(expected)


async def test_on_show_wakes_before_launching() -> None:
    """on_show wakes the device before launching the app."""
    dashboards = _make_dashboards()
    player = _make_player()
    dashboards.mass.players.get_player.return_value = player  # type: ignore[attr-defined]
    dashboards.mass.dashboard.resolve_dashboard_url = AsyncMock(  # type: ignore[method-assign]
        return_value="http://192.168.1.10:8095/?dashboard=ABC123&path=%2Fnow-playing%3Fplayer%3Dap"
    )
    order = MagicMock()
    order.attach_mock(player.wake, "wake")
    order.attach_mock(player.async_launch_app, "launch")

    await dashboards._on_show(PLAYER_ID, DashboardType.NOW_PLAYING, PLAYER_ID)

    assert [call[0] for call in order.mock_calls] == ["wake", "launch"]


async def test_on_show_launch_failure_raises_player_unavailable() -> None:
    """A failed launch RPC surfaces as a translated PlayerUnavailableError."""
    dashboards = _make_dashboards()
    player = _make_player(display_name="Living Room")
    player.async_launch_app = AsyncMock(side_effect=PlayerCommandFailed("boom"))
    dashboards.mass.players.get_player.return_value = player  # type: ignore[attr-defined]
    dashboards.mass.dashboard.resolve_dashboard_url = AsyncMock(  # type: ignore[method-assign]
        return_value="http://192.168.1.10:8095/?dashboard=ABC123&path=%2Fnow-playing%3Fplayer%3Dap"
    )

    with pytest.raises(PlayerUnavailableError) as exc_info:
        await dashboards._on_show(PLAYER_ID, DashboardType.NOW_PLAYING, PLAYER_ID)

    assert exc_info.value.translation_key == "show_dashboard_failed"
    assert exc_info.value.translation_owner == "provider.airplay"
    assert exc_info.value.translation_args == ["Living Room"]


async def test_on_show_missing_player_raises() -> None:
    """on_show for a player that vanished raises PlayerUnavailableError."""
    dashboards = _make_dashboards()
    dashboards.mass.players.get_player.return_value = None  # type: ignore[attr-defined]

    with pytest.raises(PlayerUnavailableError):
        await dashboards._on_show(PLAYER_ID, DashboardType.NOW_PLAYING, PLAYER_ID)


async def test_on_hide_performs_no_device_action() -> None:
    """Hide is session-driven: the adapter performs no Companion action."""
    dashboards = _make_dashboards()
    player = _make_player()
    dashboards.mass.players.get_player.return_value = player  # type: ignore[attr-defined]

    await dashboards._on_hide(PLAYER_ID)

    dashboards.mass.players.get_player.assert_not_called()  # type: ignore[attr-defined]
    player.wake.assert_not_awaited()
    player.async_launch_app.assert_not_awaited()


async def test_launch_failure_leaves_no_controller_session() -> None:
    """A launch failure propagates through the real controller without leaving a session."""
    controller = DashboardController.__new__(DashboardController)
    controller.mass = MagicMock()
    controller.logger = MagicMock()
    controller._dashboards = {}
    controller._sessions = {}
    controller.resolve_dashboard_url = AsyncMock(  # type: ignore[method-assign]
        return_value="http://192.168.1.10:8095/?dashboard=ABC123&path=%2Fnow-playing%3Fplayer%3Dap"
    )

    dashboards = _make_dashboards()
    dashboards.mass.dashboard = controller
    player = _make_player()
    player.async_launch_app = AsyncMock(side_effect=PlayerCommandFailed("boom"))
    dashboards.mass.players.get_player.return_value = player  # type: ignore[attr-defined]
    dashboards._register(player)
    dashboard_id = f"airplay_{PLAYER_ID}"
    assert dashboard_id in controller._dashboards

    with pytest.raises(PlayerUnavailableError):
        await controller.show_dashboard(
            dashboard_id, DashboardType.NOW_PLAYING, player_id=PLAYER_ID
        )

    assert dashboard_id not in controller._sessions
