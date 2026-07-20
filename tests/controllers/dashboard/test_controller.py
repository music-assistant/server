"""Tests for the DashboardController."""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.dashboard import DashboardDevice, DashboardSession
from music_assistant_models.enums import EventType, ProviderFeature
from music_assistant_models.errors import (
    ActionUnavailable,
    MusicAssistantError,
    ProviderUnavailableError,
    UnsupportedFeaturedException,
)

from music_assistant.controllers.dashboard import DashboardController
from music_assistant.controllers.dashboard.controller import DASHBOARD_VIEWER_USERNAME
from music_assistant.mass import MusicAssistant


@pytest.fixture(autouse=True)
def _use_ephemeral_ports() -> Generator[None]:
    """
    Bind the webserver and streamserver to OS-assigned ephemeral ports.

    Avoids clashing with a Music Assistant instance already running on the
    default ports (8095/8097) on the developer's machine. Autouse ensures the
    patch is active before the `mass` fixture boots the server.
    """
    with (
        patch("music_assistant.controllers.webserver.controller.DEFAULT_SERVER_PORT", 0),
        patch("music_assistant.controllers.streams.controller.DEFAULT_PORT", 0),
    ):
        yield


def _make_controller() -> DashboardController:
    """Build a DashboardController instance without running its (network-touching) __init__."""
    controller = DashboardController.__new__(DashboardController)
    controller.mass = MagicMock()
    controller.logger = MagicMock()
    controller._sessions = {}
    return controller


def _make_provider(*, supports_dashboard: bool) -> MagicMock:
    """Build a mock player provider, optionally declaring the SHOW_DASHBOARD feature."""
    provider = MagicMock()
    provider.supported_features = {ProviderFeature.SHOW_DASHBOARD} if supports_dashboard else set()
    if not supports_dashboard:
        provider.check_feature.side_effect = UnsupportedFeaturedException(
            "Provider does not support feature show_dashboard"
        )
    provider.get_dashboard_devices = AsyncMock(return_value=[])
    provider.show_dashboard = AsyncMock()
    provider.hide_dashboard = AsyncMock()
    return provider


async def test_get_dashboard_code_creates_viewer_user_and_code(mass: MusicAssistant) -> None:
    """A dashboard code can be exchanged for a token belonging to the cast viewer user."""
    code = await mass.dashboard._get_dashboard_code()

    assert code
    result = await mass.webserver.auth.exchange_join_code(code)

    assert result["success"] is True
    user = await mass.webserver.auth.authenticate_with_token(result["access_token"])
    assert user is not None
    assert user.username == DASHBOARD_VIEWER_USERNAME


async def test_show_dashboard_rejects_unknown_provider() -> None:
    """Casting fails when the given provider instance does not resolve to a provider."""
    controller = _make_controller()
    controller.mass.get_provider.return_value = None  # type: ignore[attr-defined]

    with pytest.raises(ProviderUnavailableError):
        await controller.show_dashboard("unknown", "device1", "/party")


async def test_show_dashboard_rejects_provider_without_feature() -> None:
    """Casting fails when the resolved provider does not declare SHOW_DASHBOARD."""
    controller = _make_controller()
    provider = _make_provider(supports_dashboard=False)
    controller.mass.get_provider.return_value = provider  # type: ignore[attr-defined]

    with pytest.raises(UnsupportedFeaturedException):
        await controller.show_dashboard("chromecast", "device1", "/party")

    provider.show_dashboard.assert_not_awaited()


async def test_show_dashboard_raises_when_remote_access_disabled() -> None:
    """Casting a dashboard requires remote access to be enabled."""
    controller = _make_controller()
    provider = _make_provider(supports_dashboard=True)
    controller.mass.get_provider.return_value = provider  # type: ignore[attr-defined]
    controller.mass.webserver.remote_access.is_enabled = False  # type: ignore[misc]
    controller.mass.webserver.remote_access.remote_id = ""  # type: ignore[misc]

    with pytest.raises(ActionUnavailable):
        await controller.show_dashboard("chromecast", "device1", "/party")

    provider.show_dashboard.assert_not_awaited()


async def test_show_dashboard_happy_path() -> None:
    """A dashboard code is minted and delegated to the resolved provider's transport."""
    controller = _make_controller()
    provider = _make_provider(supports_dashboard=True)
    controller.mass.get_provider.return_value = provider  # type: ignore[attr-defined]
    controller.mass.webserver.remote_access.is_enabled = True  # type: ignore[misc]
    controller.mass.webserver.remote_access.remote_id = "remote123"  # type: ignore[misc]

    with patch.object(
        DashboardController, "_get_dashboard_code", AsyncMock(return_value="code456")
    ):
        await controller.show_dashboard("chromecast", "device1", "/party")

    provider.show_dashboard.assert_awaited_once_with(
        device_id="device1", path="/party", remote_id="remote123", dashboard_code="code456"
    )


async def test_show_dashboard_stores_session_and_signals_event() -> None:
    """A successful show stores the session and signals the sessions-updated event."""
    controller = _make_controller()
    provider = _make_provider(supports_dashboard=True)
    controller.mass.get_provider.return_value = provider  # type: ignore[attr-defined]
    controller.mass.webserver.remote_access.is_enabled = True  # type: ignore[misc]
    controller.mass.webserver.remote_access.remote_id = "remote123"  # type: ignore[misc]

    with patch.object(
        DashboardController, "_get_dashboard_code", AsyncMock(return_value="code456")
    ):
        await controller.show_dashboard("chromecast", "device1", "/party")

    session = DashboardSession(device_id="device1", provider_instance="chromecast", path="/party")
    assert controller._sessions[("chromecast", "device1")] == session
    controller.mass.signal_event.assert_called_once_with(  # type: ignore[attr-defined]
        EventType.DASHBOARD_SESSIONS_UPDATED, data=[session]
    )


async def test_show_dashboard_replaces_existing_session_for_same_device() -> None:
    """Re-showing on the same device replaces its previous session entry."""
    controller = _make_controller()
    provider = _make_provider(supports_dashboard=True)
    controller.mass.get_provider.return_value = provider  # type: ignore[attr-defined]
    controller.mass.webserver.remote_access.is_enabled = True  # type: ignore[misc]
    controller.mass.webserver.remote_access.remote_id = "remote123"  # type: ignore[misc]

    with patch.object(
        DashboardController, "_get_dashboard_code", AsyncMock(return_value="code456")
    ):
        await controller.show_dashboard("chromecast", "device1", "/party")
        await controller.show_dashboard("chromecast", "device1", "/queue")

    assert controller._sessions == {
        ("chromecast", "device1"): DashboardSession(
            device_id="device1", provider_instance="chromecast", path="/queue"
        )
    }


async def test_get_dashboard_sessions_returns_stored_sessions() -> None:
    """The sessions command returns all currently tracked sessions."""
    controller = _make_controller()
    session = DashboardSession(device_id="device1", provider_instance="chromecast", path="/party")
    controller._sessions[("chromecast", "device1")] = session

    sessions = await controller.get_dashboard_sessions()

    assert sessions == [session]


async def test_hide_dashboard_rejects_unknown_provider() -> None:
    """Hiding fails when the given provider instance does not resolve to a provider."""
    controller = _make_controller()
    controller.mass.get_provider.return_value = None  # type: ignore[attr-defined]

    with pytest.raises(ProviderUnavailableError):
        await controller.hide_dashboard("unknown", "device1")


async def test_hide_dashboard_rejects_provider_without_feature() -> None:
    """Hiding fails when the resolved provider does not declare SHOW_DASHBOARD."""
    controller = _make_controller()
    provider = _make_provider(supports_dashboard=False)
    controller.mass.get_provider.return_value = provider  # type: ignore[attr-defined]

    with pytest.raises(UnsupportedFeaturedException):
        await controller.hide_dashboard("chromecast", "device1")

    provider.hide_dashboard.assert_not_awaited()


async def test_hide_dashboard_removes_session_and_signals_event() -> None:
    """A successful hide drops the tracked session and signals the sessions-updated event."""
    controller = _make_controller()
    provider = _make_provider(supports_dashboard=True)
    controller.mass.get_provider.return_value = provider  # type: ignore[attr-defined]
    controller._sessions[("chromecast", "device1")] = DashboardSession(
        device_id="device1", provider_instance="chromecast", path="/party"
    )

    await controller.hide_dashboard("chromecast", "device1")

    provider.hide_dashboard.assert_awaited_once_with("device1")
    assert ("chromecast", "device1") not in controller._sessions
    controller.mass.signal_event.assert_called_once_with(  # type: ignore[attr-defined]
        EventType.DASHBOARD_SESSIONS_UPDATED, data=[]
    )


async def test_hide_dashboard_cleans_up_even_when_provider_raises() -> None:
    """The session is dropped and the event signaled even if the provider had nothing to hide."""
    controller = _make_controller()
    provider = _make_provider(supports_dashboard=True)
    provider.hide_dashboard = AsyncMock(side_effect=MusicAssistantError("nothing to hide"))
    controller.mass.get_provider.return_value = provider  # type: ignore[attr-defined]
    controller._sessions[("chromecast", "device1")] = DashboardSession(
        device_id="device1", provider_instance="chromecast", path="/party"
    )

    await controller.hide_dashboard("chromecast", "device1")

    assert ("chromecast", "device1") not in controller._sessions
    controller.mass.signal_event.assert_called_once_with(  # type: ignore[attr-defined]
        EventType.DASHBOARD_SESSIONS_UPDATED, data=[]
    )


async def test_get_dashboard_devices_aggregates_across_feature_providers() -> None:
    """Devices are aggregated only from providers the mass lookup reports as SHOW_DASHBOARD."""
    controller = _make_controller()
    device = DashboardDevice(device_id="d1", provider_instance="chromecast", name="Living Room")
    supporting_provider = _make_provider(supports_dashboard=True)
    supporting_provider.get_dashboard_devices = AsyncMock(return_value=[device])
    controller.mass.get_providers_supporting_feature.return_value = [  # type: ignore[attr-defined]
        supporting_provider
    ]

    devices = await controller.get_dashboard_devices()

    assert devices == [device]
    supporting_provider.get_dashboard_devices.assert_awaited_once()
