"""Tests for Chromecast dashboard session-liveness detection."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from music_assistant_models.enums import DashboardType
from pychromecast.socket_client import CONNECTION_STATUS_CONNECTED, CONNECTION_STATUS_LOST

from music_assistant.providers.chromecast.constants import MASS_APP_ID
from music_assistant.providers.chromecast.dashboard import (
    DASHBOARD_CONNECTION_LOSS_GRACE,
    ChromecastDashboards,
    _ActiveDashboardCast,
)


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
    provider.mass.loop.call_soon_threadsafe = MagicMock(side_effect=lambda func, *args: func(*args))
    provider.mass.loop.call_later = MagicMock()
    provider.mass.players.get_player.return_value = None
    return ChromecastDashboards(provider)


def _chromecast(app_id: str = MASS_APP_ID, session_id: str = "session-1") -> MagicMock:
    """Build a connected Chromecast mock, already showing the MA receiver app."""
    chromecast = MagicMock()
    chromecast.socket_client.is_connected = True
    chromecast.status = MagicMock(app_id=app_id, session_id=session_id)
    return chromecast


async def _show(dashboards: ChromecastDashboards, device_id: str, chromecast: MagicMock) -> None:
    """Drive a successful _on_show for device_id against an already-connected chromecast."""
    dashboards.mass.dashboard.resolve_dashboard_url = AsyncMock(return_value="https://x")
    dashboards._dashboard_connections[device_id] = chromecast
    with patch("music_assistant.providers.chromecast.dashboard.send_show_dashboard"):
        await dashboards._on_show(device_id, DashboardType.PARTY, None)


### Cast status: receiver app identity changes


async def test_cast_status_different_app_ends_active_session() -> None:
    """A cast status reporting a different app than ours ends the dashboard session."""
    dashboards = _make_dashboards()
    device_id = str(uuid4())
    chromecast = _chromecast()
    await _show(dashboards, device_id, chromecast)
    listener = chromecast.register_status_listener.call_args[0][0]

    other_status = MagicMock(app_id="OTHERAPP", session_id="other-session", display_name="YouTube")
    listener.new_cast_status(other_status)

    dashboards.mass.dashboard.end_session.assert_called_once_with(  # type: ignore[attr-defined]
        f"chromecast_{device_id}", "the receiver app was closed (active app: YouTube)"
    )
    assert device_id not in dashboards._active_casts


async def test_cast_status_same_app_and_session_keeps_active_session() -> None:
    """MA audio playing in the same receiver app session leaves the dashboard session intact."""
    dashboards = _make_dashboards()
    device_id = str(uuid4())
    chromecast = _chromecast(session_id="session-1")
    await _show(dashboards, device_id, chromecast)
    listener = chromecast.register_status_listener.call_args[0][0]

    same_status = MagicMock(app_id=MASS_APP_ID, session_id="session-1")
    listener.new_cast_status(same_status)

    dashboards.mass.dashboard.end_session.assert_not_called()  # type: ignore[attr-defined]
    assert device_id in dashboards._active_casts


def test_handle_cast_status_without_active_cast_is_noop() -> None:
    """A cast status for a device with no tracked active dashboard cast is ignored."""
    dashboards = _make_dashboards()
    status = MagicMock(app_id="OTHERAPP", session_id="other-session")

    dashboards._handle_cast_status(str(uuid4()), status)

    dashboards.mass.dashboard.end_session.assert_not_called()  # type: ignore[attr-defined]


async def test_on_show_reuses_listener_for_same_chromecast_object() -> None:
    """Re-showing on the same connection does not stack duplicate listeners."""
    dashboards = _make_dashboards()
    device_id = str(uuid4())
    chromecast = _chromecast()

    await _show(dashboards, device_id, chromecast)
    await _show(dashboards, device_id, chromecast)

    chromecast.register_status_listener.assert_called_once()
    chromecast.register_connection_listener.assert_called_once()


### Connection status: loss grace timer


def test_connection_lost_arms_timer_and_reconnect_cancels_it() -> None:
    """A lost connection arms the grace timer; reconnecting before it fires cancels it."""
    dashboards = _make_dashboards()
    device_id = str(uuid4())
    timer_handle = MagicMock()
    dashboards.mass.loop.call_later.return_value = timer_handle  # type: ignore[attr-defined]
    dashboards._active_casts[device_id] = _ActiveDashboardCast(cast_session_id="session-1")

    dashboards._handle_connection_status(device_id, MagicMock(status=CONNECTION_STATUS_LOST))

    dashboards.mass.loop.call_later.assert_called_once_with(  # type: ignore[attr-defined]
        DASHBOARD_CONNECTION_LOSS_GRACE,
        dashboards._session_lost,
        device_id,
        "connection to the device was lost",
    )
    assert dashboards._active_casts[device_id].loss_timer is timer_handle

    dashboards._handle_connection_status(device_id, MagicMock(status=CONNECTION_STATUS_CONNECTED))

    timer_handle.cancel.assert_called_once()
    assert dashboards._active_casts[device_id].loss_timer is None


def test_connection_lost_does_not_stack_a_second_timer() -> None:
    """A second LOST status while a timer is already pending does not arm another one."""
    dashboards = _make_dashboards()
    device_id = str(uuid4())
    dashboards._active_casts[device_id] = _ActiveDashboardCast(cast_session_id="session-1")

    dashboards._handle_connection_status(device_id, MagicMock(status=CONNECTION_STATUS_LOST))
    dashboards._handle_connection_status(device_id, MagicMock(status=CONNECTION_STATUS_LOST))

    dashboards.mass.loop.call_later.assert_called_once()  # type: ignore[attr-defined]


def test_connection_loss_timer_callback_ends_session() -> None:
    """The grace timer firing reports the session lost with the connection-loss reason."""
    dashboards = _make_dashboards()
    device_id = str(uuid4())
    dashboards._active_casts[device_id] = _ActiveDashboardCast(cast_session_id="session-1")

    dashboards._session_lost(device_id, "connection to the device was lost")

    dashboards.mass.dashboard.end_session.assert_called_once_with(  # type: ignore[attr-defined]
        f"chromecast_{device_id}", "connection to the device was lost"
    )
    assert device_id not in dashboards._active_casts


def test_handle_connection_status_without_active_cast_is_noop() -> None:
    """A connection status for a device with no tracked active dashboard cast is ignored."""
    dashboards = _make_dashboards()

    dashboards._handle_connection_status(str(uuid4()), MagicMock(status=CONNECTION_STATUS_LOST))

    dashboards.mass.loop.call_later.assert_not_called()  # type: ignore[attr-defined]


### Hide: no false-positive liveness report


async def test_hide_cancels_loss_timer_without_ending_session() -> None:
    """Hiding cancels a pending loss timer and drops the entry, without reporting it lost."""
    dashboards = _make_dashboards()
    device_id = str(uuid4())
    timer_handle = MagicMock()
    dashboards._active_casts[device_id] = _ActiveDashboardCast(
        cast_session_id="session-1", loss_timer=timer_handle
    )
    dashboards.provider.browser = MagicMock(devices={})

    with patch("music_assistant.providers.chromecast.dashboard.send_hide_dashboard"):
        await dashboards._on_hide(device_id)

    timer_handle.cancel.assert_called_once()
    assert device_id not in dashboards._active_casts
    dashboards.mass.dashboard.end_session.assert_not_called()  # type: ignore[attr-defined]
