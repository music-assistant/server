"""Tests for the DashboardController."""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlparse

import pytest
from music_assistant_models.dashboard import DashboardDevice, DashboardSession
from music_assistant_models.enums import DashboardType, EventType
from music_assistant_models.errors import ActionUnavailable, InvalidCommand, MusicAssistantError

from music_assistant.controllers.dashboard import DashboardController
from music_assistant.controllers.dashboard.controller import (
    ALL_DASHBOARD_TYPES,
    DASHBOARD_VIEWER_USERNAME,
    _RegisteredDashboard,
)
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
    controller._dashboards = {}
    controller._sessions = {}
    # neither casting mechanism configured by default: individual tests opt in
    controller.mass.webserver.base_url = ""
    controller.mass.webserver.remote_access.is_enabled = False
    controller.mass.webserver.remote_access.remote_id = ""
    controller.mass.version = "0.0.0"
    return controller


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


async def test_register_dashboard_stores_registration_and_signals() -> None:
    """Registering stores the dashboard endpoint and signals DASHBOARDS_UPDATED."""
    controller = _make_controller()

    with patch(
        "music_assistant.controllers.dashboard.controller.get_current_client_id",
        return_value=None,
    ):
        await controller.register_dashboard("dash1", "Living Room")

    device = controller._dashboards["dash1"].device
    assert device == DashboardDevice(
        dashboard_id="dash1", name="Living Room", supported_types=set(ALL_DASHBOARD_TYPES)
    )
    controller.mass.signal_event.assert_called_once_with(  # type: ignore[attr-defined]
        EventType.DASHBOARDS_UPDATED, data=[device]
    )


async def test_register_dashboard_respects_explicit_supported_types() -> None:
    """Explicit supported_types are stored as given, not defaulted to all types."""
    controller = _make_controller()

    with patch(
        "music_assistant.controllers.dashboard.controller.get_current_client_id",
        return_value=None,
    ):
        await controller.register_dashboard(
            "dash1", "Living Room", supported_types={DashboardType.PARTY}
        )

    assert controller._dashboards["dash1"].device.supported_types == {DashboardType.PARTY}


async def test_register_dashboard_stores_calling_client_id() -> None:
    """The registering websocket client id is captured on the registration."""
    controller = _make_controller()

    with patch(
        "music_assistant.controllers.dashboard.controller.get_current_client_id",
        return_value="client123",
    ):
        await controller.register_dashboard("dash1", "Living Room")

    assert controller._dashboards["dash1"].client_id == "client123"


async def test_register_dashboard_replaces_existing_registration() -> None:
    """Re-registering an existing dashboard_id replaces it (e.g. client reconnect)."""
    controller = _make_controller()

    with patch(
        "music_assistant.controllers.dashboard.controller.get_current_client_id",
        return_value=None,
    ):
        await controller.register_dashboard("dash1", "Old Name")
        await controller.register_dashboard(
            "dash1", "New Name", supported_types={DashboardType.PARTY}
        )

    assert len(controller._dashboards) == 1
    assert controller._dashboards["dash1"].device.name == "New Name"
    assert controller._dashboards["dash1"].device.supported_types == {DashboardType.PARTY}


async def test_register_dashboard_unchanged_reregistration_does_not_signal() -> None:
    """Re-registering with identical device data does not spam DASHBOARDS_UPDATED."""
    controller = _make_controller()

    with patch(
        "music_assistant.controllers.dashboard.controller.get_current_client_id",
        return_value=None,
    ):
        await controller.register_dashboard("dash1", "Living Room")
        controller.mass.signal_event.reset_mock()  # type: ignore[attr-defined]
        await controller.register_dashboard("dash1", "Living Room")

    controller.mass.signal_event.assert_not_called()  # type: ignore[attr-defined]


async def test_register_dashboard_handler_unchanged_reregistration_does_not_signal() -> None:
    """Handler re-registration with identical device data does not spam DASHBOARDS_UPDATED."""
    controller = _make_controller()
    device = DashboardDevice(
        dashboard_id="dash1", name="Living Room", supported_types={DashboardType.PARTY}
    )

    controller.register_dashboard_handler(device, AsyncMock(), AsyncMock())
    controller.mass.signal_event.reset_mock()  # type: ignore[attr-defined]
    controller.register_dashboard_handler(device, AsyncMock(), AsyncMock())

    controller.mass.signal_event.assert_not_called()  # type: ignore[attr-defined]


async def test_unregister_dashboard_removes_registration_and_session() -> None:
    """Unregistering drops the registration and any active session, signaling both events."""
    controller = _make_controller()
    device = DashboardDevice(dashboard_id="dash1", name="Living Room")
    controller._dashboards["dash1"] = _RegisteredDashboard(device=device)
    controller._sessions["dash1"] = DashboardSession(
        dashboard_id="dash1", name="Living Room", dashboard=DashboardType.PARTY
    )

    await controller.unregister_dashboard("dash1")

    assert "dash1" not in controller._dashboards
    assert "dash1" not in controller._sessions
    controller.mass.signal_event.assert_any_call(  # type: ignore[attr-defined]
        EventType.DASHBOARDS_UPDATED, data=[]
    )
    controller.mass.signal_event.assert_any_call(  # type: ignore[attr-defined]
        EventType.DASHBOARD_SESSIONS_UPDATED, data=[]
    )
    assert controller.mass.signal_event.call_count == 2  # type: ignore[attr-defined]


async def test_unregister_dashboard_without_session_only_signals_dashboards() -> None:
    """Unregistering an endpoint with no active session doesn't signal sessions updated."""
    controller = _make_controller()
    device = DashboardDevice(dashboard_id="dash1", name="Living Room")
    controller._dashboards["dash1"] = _RegisteredDashboard(device=device)

    await controller.unregister_dashboard("dash1")

    controller.mass.signal_event.assert_called_once_with(  # type: ignore[attr-defined]
        EventType.DASHBOARDS_UPDATED, data=[]
    )


async def test_unregister_dashboard_unknown_id_is_noop() -> None:
    """Unregistering an unknown dashboard_id is a graceful no-op, no events signaled."""
    controller = _make_controller()

    await controller.unregister_dashboard("unknown")

    controller.mass.signal_event.assert_not_called()  # type: ignore[attr-defined]


async def test_get_dashboards_returns_all_registered() -> None:
    """All registered dashboard endpoints are returned when no filter is given."""
    controller = _make_controller()
    device1 = DashboardDevice(dashboard_id="dash1", name="Living Room")
    device2 = DashboardDevice(
        dashboard_id="dash2", name="Kitchen", supported_types={DashboardType.PARTY}
    )
    controller._dashboards["dash1"] = _RegisteredDashboard(device=device1)
    controller._dashboards["dash2"] = _RegisteredDashboard(device=device2)

    devices = await controller.get_dashboards()

    assert devices == [device1, device2]


async def test_get_dashboards_filters_by_supported_type() -> None:
    """Only endpoints declaring the requested dashboard type are returned."""
    controller = _make_controller()
    device1 = DashboardDevice(
        dashboard_id="dash1", name="Living Room", supported_types={DashboardType.PARTY}
    )
    device2 = DashboardDevice(
        dashboard_id="dash2", name="Kitchen", supported_types={DashboardType.NOW_PLAYING}
    )
    controller._dashboards["dash1"] = _RegisteredDashboard(device=device1)
    controller._dashboards["dash2"] = _RegisteredDashboard(device=device2)

    devices = await controller.get_dashboards(dashboard=DashboardType.PARTY)

    assert devices == [device1]


async def test_show_dashboard_rejects_unknown_dashboard_id() -> None:
    """Showing fails when the dashboard_id does not resolve to a registration."""
    controller = _make_controller()

    with pytest.raises(InvalidCommand):
        await controller.show_dashboard("unknown", DashboardType.PARTY)


async def test_show_dashboard_rejects_unsupported_type() -> None:
    """Showing fails when the registered endpoint doesn't support the requested type."""
    controller = _make_controller()
    device = DashboardDevice(
        dashboard_id="dash1", name="Living Room", supported_types={DashboardType.NOW_PLAYING}
    )
    controller._dashboards["dash1"] = _RegisteredDashboard(device=device)

    with pytest.raises(InvalidCommand):
        await controller.show_dashboard("dash1", DashboardType.PARTY)


async def test_show_dashboard_callback_path_resolves_url_and_invokes_on_show() -> None:
    """A callback registration resolves the url first, then delegates to on_show."""
    controller = _make_controller()
    controller.mass.webserver.remote_access.is_enabled = True  # type: ignore[misc]
    controller.mass.webserver.remote_access.remote_id = "remote123"  # type: ignore[misc]
    device = DashboardDevice(
        dashboard_id="dash1", name="Living Room", supported_types=set(ALL_DASHBOARD_TYPES)
    )
    on_show = AsyncMock()
    controller._dashboards["dash1"] = _RegisteredDashboard(
        device=device, on_show=on_show, on_hide=AsyncMock()
    )

    with patch.object(
        DashboardController, "_get_dashboard_code", AsyncMock(return_value="code456")
    ):
        await controller.show_dashboard("dash1", DashboardType.PARTY)

    on_show.assert_awaited_once()
    dashboard_arg, url_arg, player_id_arg = on_show.await_args.args  # type: ignore[union-attr]
    assert dashboard_arg == DashboardType.PARTY
    assert player_id_arg is None
    assert _query(url_arg) == {
        "remote_id": "remote123",
        "dashboard": "code456",
        "path": "/party",
    }

    session = controller._sessions["dash1"]
    assert session == DashboardSession(
        dashboard_id="dash1", name="Living Room", dashboard=DashboardType.PARTY
    )
    controller.mass.signal_event.assert_called_once_with(  # type: ignore[attr-defined]
        EventType.DASHBOARD_SESSIONS_UPDATED, data=[session]
    )


async def test_show_dashboard_callback_error_propagates_without_storing_session() -> None:
    """If on_show raises, the error propagates and no session is ever stored."""
    controller = _make_controller()
    controller.mass.webserver.remote_access.is_enabled = True  # type: ignore[misc]
    controller.mass.webserver.remote_access.remote_id = "remote123"  # type: ignore[misc]
    device = DashboardDevice(
        dashboard_id="dash1", name="Living Room", supported_types=set(ALL_DASHBOARD_TYPES)
    )
    on_show = AsyncMock(side_effect=MusicAssistantError("cast failed"))
    controller._dashboards["dash1"] = _RegisteredDashboard(device=device, on_show=on_show)

    with (
        patch.object(DashboardController, "_get_dashboard_code", AsyncMock(return_value="code456")),
        pytest.raises(MusicAssistantError),
    ):
        await controller.show_dashboard("dash1", DashboardType.PARTY)

    assert "dash1" not in controller._sessions
    controller.mass.signal_event.assert_not_called()  # type: ignore[attr-defined]


async def test_show_dashboard_api_path_emits_event_without_url() -> None:
    """An API registration emits DASHBOARD_SHOW with the session but no url, and stores it."""
    controller = _make_controller()
    device = DashboardDevice(
        dashboard_id="dash1", name="Living Room", supported_types=set(ALL_DASHBOARD_TYPES)
    )
    controller._dashboards["dash1"] = _RegisteredDashboard(device=device)

    await controller.show_dashboard("dash1", DashboardType.NOW_PLAYING, player_id="player1")

    session = DashboardSession(
        dashboard_id="dash1",
        name="Living Room",
        dashboard=DashboardType.NOW_PLAYING,
        player_id="player1",
    )
    show_call = next(
        call
        for call in controller.mass.signal_event.call_args_list  # type: ignore[attr-defined]
        if call.args[0] == EventType.DASHBOARD_SHOW
    )
    assert show_call.kwargs == {"object_id": "dash1", "data": session}
    assert controller._sessions["dash1"] == session


async def test_hide_dashboard_callback_path_invokes_on_hide() -> None:
    """A callback registration's on_hide is awaited, and the session is dropped."""
    controller = _make_controller()
    device = DashboardDevice(dashboard_id="dash1", name="Living Room")
    on_hide = AsyncMock()
    controller._dashboards["dash1"] = _RegisteredDashboard(device=device, on_hide=on_hide)
    controller._sessions["dash1"] = DashboardSession(
        dashboard_id="dash1", name="Living Room", dashboard=DashboardType.PARTY
    )

    await controller.hide_dashboard("dash1")

    on_hide.assert_awaited_once()
    assert "dash1" not in controller._sessions
    controller.mass.signal_event.assert_called_once_with(  # type: ignore[attr-defined]
        EventType.DASHBOARD_SESSIONS_UPDATED, data=[]
    )


async def test_hide_dashboard_callback_error_is_swallowed() -> None:
    """A MusicAssistantError raised by on_hide is caught and logged, not propagated."""
    controller = _make_controller()
    device = DashboardDevice(dashboard_id="dash1", name="Living Room")
    on_hide = AsyncMock(side_effect=MusicAssistantError("nothing to hide"))
    controller._dashboards["dash1"] = _RegisteredDashboard(device=device, on_hide=on_hide)
    controller._sessions["dash1"] = DashboardSession(
        dashboard_id="dash1", name="Living Room", dashboard=DashboardType.PARTY
    )

    await controller.hide_dashboard("dash1")  # should not raise

    assert "dash1" not in controller._sessions


async def test_hide_dashboard_api_path_emits_event() -> None:
    """An API registration emits DASHBOARD_HIDE and drops the session."""
    controller = _make_controller()
    device = DashboardDevice(dashboard_id="dash1", name="Living Room")
    controller._dashboards["dash1"] = _RegisteredDashboard(device=device)
    controller._sessions["dash1"] = DashboardSession(
        dashboard_id="dash1", name="Living Room", dashboard=DashboardType.PARTY
    )

    await controller.hide_dashboard("dash1")

    controller.mass.signal_event.assert_any_call(  # type: ignore[attr-defined]
        EventType.DASHBOARD_HIDE, object_id="dash1"
    )
    controller.mass.signal_event.assert_any_call(  # type: ignore[attr-defined]
        EventType.DASHBOARD_SESSIONS_UPDATED, data=[]
    )
    assert "dash1" not in controller._sessions


async def test_hide_dashboard_unknown_id_is_graceful_noop() -> None:
    """Hiding an unknown dashboard_id just drops any stale session, never raises."""
    controller = _make_controller()

    await controller.hide_dashboard("unknown")  # should not raise

    controller.mass.signal_event.assert_called_once_with(  # type: ignore[attr-defined]
        EventType.DASHBOARD_SESSIONS_UPDATED, data=[]
    )


async def test_get_dashboard_sessions_returns_stored_sessions() -> None:
    """The sessions command returns all currently tracked sessions."""
    controller = _make_controller()
    session = DashboardSession(
        dashboard_id="dash1", name="Living Room", dashboard=DashboardType.PARTY
    )
    controller._sessions["dash1"] = session

    sessions = await controller.get_dashboard_sessions()

    assert sessions == [session]


async def test_get_url_for_dashboard_returns_resolved_url() -> None:
    """The public get_url_for_dashboard method is a thin wrapper around URL resolution."""
    controller = _make_controller()
    controller.mass.webserver.base_url = "https://mass.example.com"  # type: ignore[misc]

    with patch.object(
        DashboardController, "_get_dashboard_code", AsyncMock(return_value="code456")
    ):
        url = await controller.get_url_for_dashboard(DashboardType.PARTY)

    assert _query(url) == {"dashboard": "code456", "path": "/party"}


async def test_handle_client_disconnected_removes_owned_registrations_and_sessions() -> None:
    """Disconnecting a client drops only that client's registrations and their sessions."""
    controller = _make_controller()
    device1 = DashboardDevice(dashboard_id="dash1", name="Living Room")
    device2 = DashboardDevice(dashboard_id="dash2", name="Kitchen")
    controller._dashboards["dash1"] = _RegisteredDashboard(device=device1, client_id="client-a")
    controller._dashboards["dash2"] = _RegisteredDashboard(device=device2, client_id="client-b")
    controller._sessions["dash1"] = DashboardSession(
        dashboard_id="dash1", name="Living Room", dashboard=DashboardType.PARTY
    )

    controller.handle_client_disconnected("client-a")

    assert "dash1" not in controller._dashboards
    assert "dash1" not in controller._sessions
    assert "dash2" in controller._dashboards
    controller.mass.signal_event.assert_any_call(  # type: ignore[attr-defined]
        EventType.DASHBOARDS_UPDATED, data=[device2]
    )
    controller.mass.signal_event.assert_any_call(  # type: ignore[attr-defined]
        EventType.DASHBOARD_SESSIONS_UPDATED, data=[]
    )


async def test_handle_client_disconnected_unknown_client_is_noop() -> None:
    """Disconnecting a client with no registrations doesn't touch other clients' state."""
    controller = _make_controller()
    device1 = DashboardDevice(dashboard_id="dash1", name="Living Room")
    controller._dashboards["dash1"] = _RegisteredDashboard(device=device1, client_id="client-a")

    controller.handle_client_disconnected("client-other")

    assert "dash1" in controller._dashboards
    controller.mass.signal_event.assert_not_called()  # type: ignore[attr-defined]


async def test_handle_client_disconnected_only_signals_sessions_when_changed() -> None:
    """Sessions-updated is only signaled when a session was actually dropped."""
    controller = _make_controller()
    device1 = DashboardDevice(dashboard_id="dash1", name="Living Room")
    controller._dashboards["dash1"] = _RegisteredDashboard(device=device1, client_id="client-a")

    controller.handle_client_disconnected("client-a")

    controller.mass.signal_event.assert_called_once_with(  # type: ignore[attr-defined]
        EventType.DASHBOARDS_UPDATED, data=[]
    )


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
