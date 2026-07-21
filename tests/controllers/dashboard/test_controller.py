"""Tests for the DashboardController."""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlparse

import pytest
from music_assistant_models.dashboard import DashboardDevice, DashboardSession
from music_assistant_models.enums import (
    DashboardType,
    EventType,
    ProviderFeature,
    ProviderType,
)
from music_assistant_models.errors import (
    ActionUnavailable,
    InvalidCommand,
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
    # neither casting mechanism configured by default: individual tests opt in
    controller.mass.webserver.base_url = ""
    controller.mass.webserver.remote_access.is_enabled = False
    controller.mass.webserver.remote_access.remote_id = ""
    controller.mass.version = "0.0.0"
    return controller


def _make_provider(*, supports_dashboard: bool) -> MagicMock:
    """Build a mock player provider, optionally declaring the SHOW_DASHBOARD feature."""
    provider = MagicMock()
    provider.type = ProviderType.PLAYER
    provider.supported_features = {ProviderFeature.SHOW_DASHBOARD} if supports_dashboard else set()
    if not supports_dashboard:
        provider.check_feature.side_effect = UnsupportedFeaturedException(
            "Provider does not support feature show_dashboard"
        )
    provider.get_dashboard_devices = AsyncMock(return_value=[])
    provider.show_dashboard = AsyncMock()
    provider.hide_dashboard = AsyncMock()
    return provider


def _query(url: str) -> dict[str, str]:
    """Parse a URL's query string into a flat dict for easy assertions."""
    return {key: values[0] for key, values in parse_qs(urlparse(url).query).items()}


async def test_resolve_dashboard_url_uses_https_base_url_when_configured() -> None:
    """An https base url is preferred and used same-origin, without a remote_id."""
    controller = _make_controller()
    controller.mass.webserver.base_url = "https://mass.example.com"  # type: ignore[misc]
    controller.mass.webserver.remote_access.is_enabled = True  # type: ignore[misc]
    controller.mass.webserver.remote_access.remote_id = "remote123"  # type: ignore[misc]

    with patch.object(
        DashboardController, "_get_dashboard_code", AsyncMock(return_value="code456")
    ):
        url = await controller._resolve_dashboard_url(DashboardType.PARTY, None)

    assert url.startswith("https://mass.example.com?")
    query = _query(url)
    assert query == {"dashboard": "code456", "path": "/party"}


async def test_resolve_dashboard_url_uses_remote_access_when_no_https_base() -> None:
    """Falls back to the app.music-assistant.io signaling portal, including the remote_id."""
    controller = _make_controller()
    controller.mass.webserver.base_url = "http://192.168.1.10:8095"  # type: ignore[misc]
    controller.mass.webserver.remote_access.is_enabled = True  # type: ignore[misc]
    controller.mass.webserver.remote_access.remote_id = "remote123"  # type: ignore[misc]
    controller.mass.version = "2.17.150"

    with patch.object(
        DashboardController, "_get_dashboard_code", AsyncMock(return_value="code456")
    ):
        url = await controller._resolve_dashboard_url(DashboardType.PARTY, None)

    assert url.startswith("https://app.music-assistant.io/stable/?")
    query = _query(url)
    assert query == {"remote_id": "remote123", "dashboard": "code456", "path": "/party"}


async def test_resolve_dashboard_url_raises_when_neither_configured() -> None:
    """Neither an https base url nor remote access is available: casting is unavailable."""
    controller = _make_controller()

    with pytest.raises(ActionUnavailable):
        await controller._resolve_dashboard_url(DashboardType.PARTY, None)


async def test_resolve_dashboard_url_now_playing_requires_player_id() -> None:
    """The now_playing dashboard cannot be shown without a player_id."""
    controller = _make_controller()
    controller.mass.webserver.base_url = "https://mass.example.com"  # type: ignore[misc]

    with pytest.raises(InvalidCommand):
        await controller._resolve_dashboard_url(DashboardType.NOW_PLAYING, None)


async def test_resolve_dashboard_url_now_playing_embeds_player_id_in_path() -> None:
    """The now_playing route carries the player_id as its own query param."""
    controller = _make_controller()
    controller.mass.webserver.base_url = "https://mass.example.com"  # type: ignore[misc]

    with patch.object(
        DashboardController, "_get_dashboard_code", AsyncMock(return_value="code456")
    ):
        url = await controller._resolve_dashboard_url(DashboardType.NOW_PLAYING, "player1")

    query = _query(url)
    assert query["path"] == "/now-playing?player=player1"


async def test_resolve_dashboard_url_rejects_unknown_dashboard_type() -> None:
    """An UNKNOWN dashboard type is never a valid target to cast."""
    controller = _make_controller()
    controller.mass.webserver.base_url = "https://mass.example.com"  # type: ignore[misc]

    with pytest.raises(InvalidCommand):
        await controller._resolve_dashboard_url(DashboardType.UNKNOWN, None)


@pytest.mark.parametrize(
    ("version", "expected_channel"),
    [
        ("0.0.0", "nightly"),
        ("2.17.150.dev202501271200", "nightly"),
        ("2.17.150b1", "beta"),
        ("2.17.150rc1", "beta"),
        ("2.17.150", "stable"),
    ],
)
def test_frontend_channel_derivation(version: str, expected_channel: str) -> None:
    """The app.music-assistant.io channel is derived from the server version string."""
    controller = _make_controller()
    controller.mass.version = version

    assert controller._frontend_channel() == expected_channel


async def test_show_dashboard_rejects_unknown_provider() -> None:
    """Casting fails when the given provider instance does not resolve to a provider."""
    controller = _make_controller()
    controller.mass.get_provider.return_value = None  # type: ignore[attr-defined]

    with pytest.raises(ProviderUnavailableError):
        await controller.show_dashboard("unknown", "device1", DashboardType.PARTY)


async def test_show_dashboard_rejects_non_player_provider() -> None:
    """Casting fails cleanly when the instance id resolves to a non-player provider."""
    controller = _make_controller()
    provider = _make_provider(supports_dashboard=True)
    provider.type = ProviderType.MUSIC
    controller.mass.get_provider.return_value = provider  # type: ignore[attr-defined]

    with pytest.raises(ProviderUnavailableError):
        await controller.show_dashboard("spotify", "device1", DashboardType.PARTY)

    provider.show_dashboard.assert_not_awaited()


async def test_show_dashboard_rejects_provider_without_feature() -> None:
    """Casting fails when the resolved provider does not declare SHOW_DASHBOARD."""
    controller = _make_controller()
    provider = _make_provider(supports_dashboard=False)
    controller.mass.get_provider.return_value = provider  # type: ignore[attr-defined]

    with pytest.raises(UnsupportedFeaturedException):
        await controller.show_dashboard("chromecast", "device1", DashboardType.PARTY)

    provider.show_dashboard.assert_not_awaited()


async def test_show_dashboard_raises_when_neither_https_base_nor_remote_access() -> None:
    """Casting a dashboard requires an https base url or remote access to be enabled."""
    controller = _make_controller()
    provider = _make_provider(supports_dashboard=True)
    controller.mass.get_provider.return_value = provider  # type: ignore[attr-defined]

    with pytest.raises(ActionUnavailable):
        await controller.show_dashboard("chromecast", "device1", DashboardType.PARTY)

    provider.show_dashboard.assert_not_awaited()


async def test_show_dashboard_happy_path() -> None:
    """A dashboard code is minted and a fully-built URL is delegated to the provider's transport."""
    controller = _make_controller()
    provider = _make_provider(supports_dashboard=True)
    controller.mass.get_provider.return_value = provider  # type: ignore[attr-defined]
    controller.mass.webserver.remote_access.is_enabled = True  # type: ignore[misc]
    controller.mass.webserver.remote_access.remote_id = "remote123"  # type: ignore[misc]

    with patch.object(
        DashboardController, "_get_dashboard_code", AsyncMock(return_value="code456")
    ):
        await controller.show_dashboard("chromecast", "device1", DashboardType.PARTY)

    provider.show_dashboard.assert_awaited_once()
    call_kwargs = provider.show_dashboard.await_args.kwargs
    assert call_kwargs["device_id"] == "device1"
    query = _query(call_kwargs["url"])
    assert query == {"remote_id": "remote123", "dashboard": "code456", "path": "/party"}


async def test_show_dashboard_stores_session_and_signals_event() -> None:
    """A successful show stores the session (dashboard + player_id) and signals the event."""
    controller = _make_controller()
    provider = _make_provider(supports_dashboard=True)
    controller.mass.get_provider.return_value = provider  # type: ignore[attr-defined]
    controller.mass.webserver.remote_access.is_enabled = True  # type: ignore[misc]
    controller.mass.webserver.remote_access.remote_id = "remote123"  # type: ignore[misc]

    with patch.object(
        DashboardController, "_get_dashboard_code", AsyncMock(return_value="code456")
    ):
        await controller.show_dashboard("chromecast", "device1", DashboardType.PARTY)

    session = DashboardSession(
        device_id="device1",
        provider_instance="chromecast",
        name="device1",
        dashboard=DashboardType.PARTY,
    )
    assert controller._sessions[("chromecast", "device1")] == session
    controller.mass.signal_event.assert_called_once_with(  # type: ignore[attr-defined]
        EventType.DASHBOARD_SESSIONS_UPDATED, data=[session]
    )


async def test_show_dashboard_now_playing_stores_player_id_in_session() -> None:
    """Casting the now_playing dashboard stores its player_id on the session."""
    controller = _make_controller()
    provider = _make_provider(supports_dashboard=True)
    controller.mass.get_provider.return_value = provider  # type: ignore[attr-defined]
    controller.mass.webserver.remote_access.is_enabled = True  # type: ignore[misc]
    controller.mass.webserver.remote_access.remote_id = "remote123"  # type: ignore[misc]

    with patch.object(
        DashboardController, "_get_dashboard_code", AsyncMock(return_value="code456")
    ):
        await controller.show_dashboard(
            "chromecast", "device1", DashboardType.NOW_PLAYING, player_id="player1"
        )

    session = controller._sessions[("chromecast", "device1")]
    assert session.dashboard == DashboardType.NOW_PLAYING
    assert session.player_id == "player1"


async def test_show_dashboard_resolves_device_name_from_provider_devices() -> None:
    """The stored session carries the display device's name, resolved via get_dashboard_devices."""
    controller = _make_controller()
    provider = _make_provider(supports_dashboard=True)
    provider.get_dashboard_devices = AsyncMock(
        return_value=[
            DashboardDevice(
                device_id="device1", provider_instance="chromecast", name="Living Room TV"
            )
        ]
    )
    controller.mass.get_provider.return_value = provider  # type: ignore[attr-defined]
    controller.mass.webserver.remote_access.is_enabled = True  # type: ignore[misc]
    controller.mass.webserver.remote_access.remote_id = "remote123"  # type: ignore[misc]

    with patch.object(
        DashboardController, "_get_dashboard_code", AsyncMock(return_value="code456")
    ):
        await controller.show_dashboard("chromecast", "device1", DashboardType.PARTY)

    assert controller._sessions[("chromecast", "device1")].name == "Living Room TV"


async def test_show_dashboard_falls_back_to_device_id_when_not_found() -> None:
    """The stored session falls back to the device_id when it isn't in get_dashboard_devices."""
    controller = _make_controller()
    provider = _make_provider(supports_dashboard=True)
    controller.mass.get_provider.return_value = provider  # type: ignore[attr-defined]
    controller.mass.webserver.remote_access.is_enabled = True  # type: ignore[misc]
    controller.mass.webserver.remote_access.remote_id = "remote123"  # type: ignore[misc]

    with patch.object(
        DashboardController, "_get_dashboard_code", AsyncMock(return_value="code456")
    ):
        await controller.show_dashboard("chromecast", "device1", DashboardType.PARTY)

    assert controller._sessions[("chromecast", "device1")].name == "device1"


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
        await controller.show_dashboard("chromecast", "device1", DashboardType.PARTY)
        await controller.show_dashboard(
            "chromecast", "device1", DashboardType.NOW_PLAYING, player_id="player1"
        )

    assert controller._sessions == {
        ("chromecast", "device1"): DashboardSession(
            device_id="device1",
            provider_instance="chromecast",
            name="device1",
            dashboard=DashboardType.NOW_PLAYING,
            player_id="player1",
        )
    }


async def test_get_dashboard_sessions_returns_stored_sessions() -> None:
    """The sessions command returns all currently tracked sessions."""
    controller = _make_controller()
    session = DashboardSession(
        device_id="device1",
        provider_instance="chromecast",
        name="device1",
        dashboard=DashboardType.PARTY,
    )
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
        device_id="device1",
        provider_instance="chromecast",
        name="device1",
        dashboard=DashboardType.PARTY,
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
        device_id="device1",
        provider_instance="chromecast",
        name="device1",
        dashboard=DashboardType.PARTY,
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


async def test_get_dashboard_code_creates_viewer_user_and_code(mass: MusicAssistant) -> None:
    """A dashboard code can be exchanged for a token belonging to the cast viewer user."""
    code = await mass.dashboard._get_dashboard_code()

    assert code
    result = await mass.webserver.auth.exchange_join_code(code)

    assert result["success"] is True
    user = await mass.webserver.auth.authenticate_with_token(result["access_token"])
    assert user is not None
    assert user.username == DASHBOARD_VIEWER_USERNAME


async def test_get_dashboard_code_mints_fresh_code_each_call(mass: MusicAssistant) -> None:
    """Two consecutive mints return different codes and both can be exchanged."""
    code1 = await mass.dashboard._get_dashboard_code()
    code2 = await mass.dashboard._get_dashboard_code()

    assert code1 != code2

    result1 = await mass.webserver.auth.exchange_join_code(code1)
    result2 = await mass.webserver.auth.exchange_join_code(code2)

    assert result1["success"] is True
    assert result2["success"] is True
