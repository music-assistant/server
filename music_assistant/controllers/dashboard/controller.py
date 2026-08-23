"""Core controller that casts Music Assistant dashboards to display devices."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

from music_assistant_models.auth import Scope
from music_assistant_models.dashboard import DashboardDevice, DashboardSession
from music_assistant_models.enums import DashboardType, EventType
from music_assistant_models.errors import (
    ActionUnavailable,
    InsufficientPermissions,
    InvalidCommand,
    MusicAssistantError,
)

from music_assistant.controllers.webserver.helpers.auth_middleware import (
    get_current_client_id,
    get_current_user,
    has_scope,
)
from music_assistant.helpers.api import api_command
from music_assistant.helpers.guest_access import get_or_create_guest_user
from music_assistant.models.core_controller import CoreController

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

DASHBOARD_VIEWER_USERNAME = "dashboard_viewer"
DASHBOARD_VIEWER_DISPLAY_NAME = "Dashboard Viewer"
DASHBOARD_CODE_EXPIRY_HOURS = 1
APP_MA_HOST = "https://app.music-assistant.io"
# every real dashboard type, i.e. what a registration supports when not given explicitly
ALL_DASHBOARD_TYPES = frozenset(t for t in DashboardType if t != DashboardType.UNKNOWN)

# the frontend's router leaves these literal in a query value; escaped, it never matches
ROUTE_SAFE_CHARS = ":!'()*@,;$/"


@dataclass
class _RegisteredDashboard:
    """A dashboard endpoint tracked by the controller, either API- or callback-registered."""

    device: DashboardDevice
    # callbacks set for in-server registrations (e.g. chromecast); API registrations use events
    on_show: Callable[[DashboardType, str | None], Awaitable[None]] | None = None
    on_hide: Callable[[], Awaitable[None]] | None = None
    client_id: str | None = None  # websocket connection that owns an API registration


class DashboardController(CoreController):
    """Casts Music Assistant dashboards (e.g. Party mode) to display devices."""

    domain: str = "dashboard"

    def __init__(self, mass: MusicAssistant) -> None:
        """Initialize the DashboardController."""
        super().__init__(mass)
        self.manifest.name = "Dashboard"
        self.manifest.description = "Casts Music Assistant dashboards to display devices."
        self._dashboards: dict[str, _RegisteredDashboard] = {}
        self._sessions: dict[str, DashboardSession] = {}
        # who cast each session: dashboard viewers follow that user's visualizer preferences
        self._session_owners: dict[str, str] = {}

    @api_command("dashboard/register")
    async def register_dashboard(
        self,
        dashboard_id: str,
        name: str,
        supported_types: set[DashboardType] | None = None,
        provider_domain_hint: str | None = None,
    ) -> None:
        """
        Register the calling client as a dashboard endpoint.

        :param dashboard_id: Unique id chosen by the registering client.
        :param name: Display name for the dashboard endpoint.
        :param supported_types: Dashboard types this endpoint can show, defaults to all
            when omitted; an explicitly empty set is rejected.
        :param provider_domain_hint: Optional provider domain used to resolve the endpoint's icon.
        :raises InvalidCommand: If not called from a websocket client, if supported_types
            is an empty set, or if dashboard_id is already registered by another owner.
        """
        client_id = get_current_client_id()
        if client_id is None:
            msg = "Registering a dashboard is only available for websocket clients"
            raise InvalidCommand(msg)
        if supported_types is not None and not supported_types:
            msg = "supported_types cannot be an empty set"
            raise InvalidCommand(msg)

        existing = self._dashboards.get(dashboard_id)
        if existing is not None and existing.client_id != client_id:
            msg = f"Dashboard {dashboard_id} is already registered by another owner"
            raise InvalidCommand(msg)

        device = DashboardDevice(
            dashboard_id=dashboard_id,
            name=name,
            supported_types=set(supported_types)
            if supported_types is not None
            else set(ALL_DASHBOARD_TYPES),
            provider_domain_hint=provider_domain_hint,
        )
        self._dashboards[dashboard_id] = _RegisteredDashboard(device=device, client_id=client_id)
        # don't spam subscribers when a re-registration changed nothing
        if existing is None or existing.device != device:
            self._signal_dashboards_updated()

    @api_command("dashboard/unregister")
    async def unregister_dashboard(self, dashboard_id: str) -> None:
        """
        Unregister a dashboard endpoint, dropping any active session for it.

        :param dashboard_id: Id of a registered dashboard endpoint.
        :raises InvalidCommand: If not called from a websocket client, or if dashboard_id
            is registered by another owner.
        """
        client_id = get_current_client_id()
        if client_id is None:
            msg = "Unregistering a dashboard is only available for websocket clients"
            raise InvalidCommand(msg)

        existing = self._dashboards.get(dashboard_id)
        if existing is None:
            return
        if existing.client_id != client_id:
            msg = f"Dashboard {dashboard_id} is registered by another owner"
            raise InvalidCommand(msg)

        del self._dashboards[dashboard_id]
        self._signal_dashboards_updated()
        if self._sessions.pop(dashboard_id, None) is not None:
            self._signal_sessions_updated()

    @api_command("dashboard/dashboards")
    async def get_dashboards(self, dashboard: DashboardType | None = None) -> list[DashboardDevice]:
        """
        Return all registered dashboard endpoints.

        :param dashboard: When given, only return endpoints that support this dashboard type.
        """
        devices = [registration.device for registration in self._dashboards.values()]
        if dashboard is None:
            return devices
        return [device for device in devices if dashboard in device.supported_types]

    @api_command("dashboard/sessions")
    async def get_dashboard_sessions(self) -> list[DashboardSession]:
        """Return all active dashboard cast sessions."""
        return list(self._sessions.values())

    @api_command("dashboard/show", required_scope=Scope.USERS_INVITE)
    async def show_dashboard(
        self,
        dashboard_id: str,
        dashboard: DashboardType,
        player_id: str | None = None,
    ) -> None:
        """
        Show a Music Assistant dashboard on a registered dashboard endpoint.

        :param dashboard_id: Id of a registered dashboard endpoint, as returned
            by `dashboard/dashboards`.
        :param dashboard: Dashboard to show.
        :param player_id: Player to show, required when dashboard is NOW_PLAYING.
        :raises InvalidCommand: If dashboard_id is unknown, doesn't support this dashboard,
            or the dashboard/player_id combination isn't routable.
        """
        registration = self._dashboards.get(dashboard_id)
        if registration is None:
            msg = f"Unknown dashboard: {dashboard_id}"
            raise InvalidCommand(msg)
        if dashboard not in registration.device.supported_types:
            msg = f"Dashboard {dashboard_id} does not support {dashboard}"
            raise InvalidCommand(msg)
        # validate intent up-front so both branches below reject it identically
        self._dashboard_route(dashboard, player_id)

        session = DashboardSession(
            dashboard_id=dashboard_id,
            name=registration.device.name,
            dashboard=dashboard,
            player_id=player_id,
        )

        if registration.on_show is not None:
            # the consumer resolves its own url if needed; raises before showing on failure
            await registration.on_show(dashboard, player_id)
        else:
            # API registration: url-based clients resolve their own url via `dashboard/get_url`
            self.mass.signal_event(EventType.DASHBOARD_SHOW, object_id=dashboard_id, data=session)

        # pop first so dict order stays show order: viewer_preferences takes the last match
        self._sessions.pop(dashboard_id, None)
        self._sessions[dashboard_id] = session
        if (user := get_current_user()) is not None:
            self._session_owners[dashboard_id] = user.user_id
        else:
            self._session_owners.pop(dashboard_id, None)
        self._signal_sessions_updated()

    @api_command("dashboard/hide", required_scope=Scope.USERS_INVITE)
    async def hide_dashboard(self, dashboard_id: str) -> None:
        """
        Hide a Music Assistant dashboard from a registered dashboard endpoint.

        :param dashboard_id: Id of a registered dashboard endpoint, as returned
            by `dashboard/dashboards`.
        """
        registration = self._dashboards.get(dashboard_id)
        if registration is not None:
            if registration.on_hide is not None:
                try:
                    await registration.on_hide()
                except MusicAssistantError:
                    # the endpoint may simply not have been showing a dashboard: not fatal
                    self.logger.debug(
                        "Dashboard %s could not hide its dashboard", dashboard_id, exc_info=True
                    )
            else:
                self.mass.signal_event(EventType.DASHBOARD_HIDE, object_id=dashboard_id)

        self._sessions.pop(dashboard_id, None)
        self._session_owners.pop(dashboard_id, None)
        self._signal_sessions_updated()

    @api_command("dashboard/viewer_preferences", required_scope=Scope.PROVIDERS_READ)
    async def get_viewer_preferences(
        self, dashboard: DashboardType, player_id: str | None = None
    ) -> dict[str, Any]:
        """
        Return the visualizer preferences a dashboard viewer should render with.

        A dashboard session runs as the shared viewer user, which has no
        preferences and no way to set any; it follows the preferences of the
        user who cast it instead. PROVIDERS_READ (held by guests) so the
        viewer itself can read this; only visualizer settings are exposed.

        :param dashboard: Dashboard the viewer is showing.
        :param player_id: Player the viewer is showing, when dashboard is NOW_PLAYING.
        """
        owner_id: str | None = None
        for session_dashboard_id, session in self._sessions.items():
            if session.dashboard != dashboard:
                continue
            if dashboard == DashboardType.NOW_PLAYING and session.player_id != player_id:
                continue
            # multiple matches: the most recently shown session wins
            owner_id = self._session_owners.get(session_dashboard_id, owner_id)
        if owner_id is None:
            return {}
        owner = await self.mass.webserver.auth.get_user(owner_id)
        if owner is None:
            return {}
        return {
            key: value
            for key, value in (owner.preferences or {}).items()
            if key.startswith("visualizer_")
        }

    @api_command("dashboard/get_url")
    async def get_url_for_dashboard(
        self, dashboard: DashboardType, player_id: str | None = None, prefer_local: bool = False
    ) -> str:
        """
        Return a fully-qualified dashboard URL for a client to load itself.

        :param dashboard: Dashboard to load.
        :param player_id: Player to show, required when dashboard is NOW_PLAYING.
        :param prefer_local: Return the plain local base url form (native LAN viewers
            that are not bound by the https/remote-access requirement).
        :raises InsufficientPermissions: If the caller has neither the required scope
            nor a matching active session of its own.
        """
        if not self._can_resolve_url_for_caller(dashboard, player_id):
            msg = "Insufficient permissions to resolve a dashboard url"
            raise InsufficientPermissions(msg)
        return await self.resolve_dashboard_url(dashboard, player_id, prefer_local=prefer_local)

    def register_dashboard_handler(
        self,
        device: DashboardDevice,
        on_show: Callable[[DashboardType, str | None], Awaitable[None]],
        on_hide: Callable[[], Awaitable[None]],
    ) -> Callable[[], None]:
        """
        Register an in-server dashboard endpoint (e.g. chromecast) with show/hide callbacks.

        :param device: Metadata for the dashboard endpoint being registered.
        :param on_show: Called with (dashboard, player_id) to show a dashboard.
        :param on_hide: Called to hide whatever dashboard is currently showing.
        :return: Callable that unregisters the endpoint and drops its active session.
        """
        dashboard_id = device.dashboard_id
        existing = self._dashboards.get(dashboard_id)
        self._dashboards[dashboard_id] = _RegisteredDashboard(
            device=device, on_show=on_show, on_hide=on_hide
        )
        # don't spam subscribers when a re-registration changed nothing
        if existing is None or existing.device != device:
            self._signal_dashboards_updated()

        def unregister() -> None:
            self._dashboards.pop(dashboard_id, None)
            self._signal_dashboards_updated()
            self._session_owners.pop(dashboard_id, None)
            if self._sessions.pop(dashboard_id, None) is not None:
                self._signal_sessions_updated()

        return unregister

    def handle_client_disconnected(self, client_id: str) -> None:
        """Drop all dashboard registrations (and sessions) owned by a disconnected client."""
        stale_ids = [
            dashboard_id
            for dashboard_id, registration in self._dashboards.items()
            if registration.client_id == client_id
        ]
        if not stale_ids:
            return

        sessions_changed = False
        for dashboard_id in stale_ids:
            del self._dashboards[dashboard_id]
            self._session_owners.pop(dashboard_id, None)
            if self._sessions.pop(dashboard_id, None) is not None:
                sessions_changed = True

        self._signal_dashboards_updated()
        if sessions_changed:
            self._signal_sessions_updated()

    def notify_user_preferences_changed(self, user_id: str) -> None:
        """
        Signal active dashboard sessions when their owner's preferences changed.

        Viewers re-fetch `dashboard/viewer_preferences` on the sessions-updated
        event, so a preference change reaches a cast display live.

        :param user_id: The user whose preferences were just updated.
        """
        if user_id in self._session_owners.values():
            self._signal_sessions_updated()

    def end_session(self, dashboard_id: str, reason: str) -> None:
        """
        End the active session for an endpoint that stopped showing its dashboard.

        :param dashboard_id: Id of a registered dashboard endpoint.
        :param reason: Human-readable cause, logged as a warning.
        """
        session = self._sessions.pop(dashboard_id, None)
        self._session_owners.pop(dashboard_id, None)
        if session is None:
            return
        self.logger.warning("Dashboard session on %s ended: %s", session.name, reason)
        self._signal_sessions_updated()

    async def resolve_dashboard_url(
        self, dashboard: DashboardType, player_id: str | None, *, prefer_local: bool = False
    ) -> str:
        """
        Build the fully-qualified URL a dashboard endpoint should load to show a dashboard.

        By default an externally-reachable https base url (reverse-proxied server, same
        origin) is preferred over remote access (the app.music-assistant.io signaling portal),
        as required by cast receivers. With ``prefer_local`` the server's own base url is
        always returned, plain http included: native apps on the LAN are not bound by the cast
        receiver's https requirement. In-server consumers (e.g. the chromecast provider) call
        this to resolve the url themselves.

        :param dashboard: Dashboard to show.
        :param player_id: Player to show, required when dashboard is NOW_PLAYING.
        :param prefer_local: Return the plain local base url instead of the https/remote form.
        :raises ActionUnavailable: If neither an https base url nor remote access is configured
            (never raised when ``prefer_local`` is set).
        """
        route = self._dashboard_route(dashboard, player_id)
        base_url = self.mass.webserver.base_url
        if prefer_local:
            # native LAN apps talk straight to this server, no https/remote gate needed
            query = {"dashboard": await self._get_dashboard_code(), "path": route}
            return f"{base_url}?{urlencode(query)}"
        remote_access = self.mass.webserver.remote_access
        use_https_base = base_url.startswith("https://")
        if not use_https_base and not (remote_access.is_enabled and remote_access.remote_id):
            msg = "Remote access or an https base url is required to cast dashboards"
            raise ActionUnavailable(
                msg,
                translation_key="remote_access_required",
                translation_owner=self.translation_owner,
            )

        dashboard_code = await self._get_dashboard_code()
        if use_https_base:
            # same origin: the receiver talks straight to this server, no remote_id needed
            query = {"dashboard": dashboard_code, "path": route}
            return f"{base_url}?{urlencode(query)}"

        query = {"remote_id": remote_access.remote_id, "dashboard": dashboard_code, "path": route}
        channel = self._frontend_channel()
        return f"{APP_MA_HOST}/{channel}/?{urlencode(query)}"

    def _can_resolve_url_for_caller(self, dashboard: DashboardType, player_id: str | None) -> bool:
        """
        Return whether the current caller may resolve a dashboard url for itself.

        :param dashboard: Dashboard the caller wants a url for.
        :param player_id: Player the caller wants a url for, when dashboard is NOW_PLAYING.
        """
        user = get_current_user()
        if user is not None and has_scope(user, Scope.USERS_INVITE):
            return True

        client_id = get_current_client_id()
        if client_id is None:
            return False
        for dashboard_id, registration in self._dashboards.items():
            if registration.client_id != client_id:
                continue
            session = self._sessions.get(dashboard_id)
            if session is None or session.dashboard != dashboard:
                continue
            if dashboard == DashboardType.NOW_PLAYING and session.player_id != player_id:
                continue
            return True
        return False

    async def _get_dashboard_code(self) -> str:
        """Mint a fresh one-time code a cast receiver can exchange for a viewer token."""
        # exchanged viewer tokens are fixed-lifetime guest tokens; a re-cast mints a fresh code
        user = await get_or_create_guest_user(
            self.mass, DASHBOARD_VIEWER_USERNAME, DASHBOARD_VIEWER_DISPLAY_NAME
        )
        code, _expires_at = await self.mass.webserver.auth.generate_join_code(
            user,
            expires_in_hours=DASHBOARD_CODE_EXPIRY_HOURS,
            max_uses=1,
            device_name="Dashboard Receiver",
        )
        return code

    def _dashboard_route(self, dashboard: DashboardType, player_id: str | None) -> str:
        """
        Map a dashboard type to its frontend route.

        :param dashboard: Dashboard to show.
        :param player_id: Player to show, required when dashboard is NOW_PLAYING.
        :raises InvalidCommand: If dashboard is NOW_PLAYING without a player_id, or unsupported.
        """
        if dashboard == DashboardType.PARTY:
            return "/party"
        if dashboard == DashboardType.NOW_PLAYING:
            if not player_id:
                msg = "player_id is required to show the now_playing dashboard"
                raise InvalidCommand(msg)
            return f"/now-playing?{urlencode({'player': player_id}, safe=ROUTE_SAFE_CHARS)}"
        if dashboard == DashboardType.MUSIC_QUIZ:
            # the viewer-only kiosk view: the host page needs USERS_INVITE, which a
            # dashboard viewer never has
            return "/music-quiz/dashboard"
        msg = f"Unsupported dashboard type: {dashboard}"
        raise InvalidCommand(msg)

    def _frontend_channel(self) -> str:
        """Derive the app.music-assistant.io frontend channel from the server version."""
        version = self.mass.version
        if version == "0.0.0" or ".dev" in version:
            return "nightly"
        if "b" in version or "rc" in version:
            return "beta"
        return "stable"

    def _signal_dashboards_updated(self) -> None:
        """Signal the current list of registered dashboard endpoints to subscribers."""
        self.mass.signal_event(
            EventType.DASHBOARDS_UPDATED,
            data=[registration.device for registration in self._dashboards.values()],
        )

    def _signal_sessions_updated(self) -> None:
        """Signal the current list of dashboard cast sessions to subscribers."""
        self.mass.signal_event(
            EventType.DASHBOARD_SESSIONS_UPDATED, data=list(self._sessions.values())
        )
