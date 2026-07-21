"""Core controller that casts Music Assistant dashboards to display devices."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from urllib.parse import urlencode

from music_assistant_models.auth import Scope
from music_assistant_models.dashboard import DashboardSession
from music_assistant_models.enums import DashboardType, EventType, ProviderFeature, ProviderType
from music_assistant_models.errors import (
    ActionUnavailable,
    InvalidCommand,
    MusicAssistantError,
    ProviderUnavailableError,
)

from music_assistant.helpers.api import api_command
from music_assistant.helpers.guest_access import get_or_create_guest_user
from music_assistant.models.core_controller import CoreController
from music_assistant.models.player_provider import PlayerProvider

if TYPE_CHECKING:
    from music_assistant_models.dashboard import DashboardDevice

    from music_assistant.mass import MusicAssistant

DASHBOARD_VIEWER_USERNAME = "dashboard_viewer"
DASHBOARD_VIEWER_DISPLAY_NAME = "Dashboard Viewer"
DASHBOARD_CODE_EXPIRY_HOURS = 1
APP_MA_HOST = "https://app.music-assistant.io"


class DashboardController(CoreController):
    """Casts Music Assistant dashboards (e.g. Party mode) to display devices."""

    domain: str = "dashboard"

    def __init__(self, mass: MusicAssistant) -> None:
        """Initialize the DashboardController."""
        super().__init__(mass)
        self.manifest.name = "Dashboard"
        self.manifest.description = "Casts Music Assistant dashboards to display devices."
        # in-memory only: not reconciled after a server restart
        self._sessions: dict[tuple[str, str], DashboardSession] = {}

    @api_command("dashboard/devices")
    async def get_dashboard_devices(self) -> list[DashboardDevice]:
        """Return all display devices capable of showing a MA dashboard."""
        devices: list[DashboardDevice] = []
        for provider in self.mass.get_providers_supporting_feature(
            ProviderFeature.SHOW_DASHBOARD, priority=(ProviderType.PLAYER,)
        ):
            provider = cast("PlayerProvider", provider)
            devices.extend(await provider.get_dashboard_devices())
        return devices

    @api_command("dashboard/sessions")
    async def get_dashboard_sessions(self) -> list[DashboardSession]:
        """Return all active dashboard cast sessions."""
        return list(self._sessions.values())

    @api_command("dashboard/show", required_scope=Scope.USERS_INVITE)
    async def show_dashboard(
        self,
        provider_instance: str,
        device_id: str,
        dashboard: DashboardType,
        player_id: str | None = None,
    ) -> None:
        """
        Show a Music Assistant dashboard on a display device.

        :param provider_instance: Instance ID of the provider owning the display device.
        :param device_id: Provider-scoped device ID, as returned by `dashboard/devices`.
        :param dashboard: Dashboard to show on the display.
        :param player_id: Player to show, required when dashboard is NOW_PLAYING.
        """
        provider = self.mass.get_provider(provider_instance, provider_type=PlayerProvider)
        # get_provider's provider_type is only a type hint, so guard at runtime
        if provider is None or provider.type != ProviderType.PLAYER:
            msg = f"Player provider not found: {provider_instance}"
            raise ProviderUnavailableError(msg)
        provider.check_feature(ProviderFeature.SHOW_DASHBOARD)

        # resolve everything that can fail before the cast, so a failure never
        # leaves a dashboard showing without a tracked session
        device_name = await self._resolve_device_name(provider, device_id)
        url = await self._resolve_dashboard_url(dashboard, player_id)
        await provider.show_dashboard(device_id=device_id, url=url)
        self._sessions[(provider_instance, device_id)] = DashboardSession(
            device_id=device_id,
            provider_instance=provider_instance,
            name=device_name,
            dashboard=dashboard,
            player_id=player_id,
        )
        self._signal_sessions_updated()

    @api_command("dashboard/hide", required_scope=Scope.USERS_INVITE)
    async def hide_dashboard(self, provider_instance: str, device_id: str) -> None:
        """
        Hide a Music Assistant dashboard from a display device.

        :param provider_instance: Instance ID of the provider owning the display device.
        :param device_id: Provider-scoped device ID, as returned by `dashboard/devices`.
        """
        provider = self.mass.get_provider(provider_instance, provider_type=PlayerProvider)
        # get_provider's provider_type is only a type hint, so guard at runtime
        if provider is None or provider.type != ProviderType.PLAYER:
            msg = f"Player provider not found: {provider_instance}"
            raise ProviderUnavailableError(msg)
        provider.check_feature(ProviderFeature.SHOW_DASHBOARD)

        try:
            await provider.hide_dashboard(device_id)
        except MusicAssistantError:
            # the receiver app may simply not have been showing a dashboard: not fatal
            self.logger.debug(
                "Provider %s could not hide dashboard on %s",
                provider_instance,
                device_id,
                exc_info=True,
            )

        self._sessions.pop((provider_instance, device_id), None)
        self._signal_sessions_updated()

    async def _resolve_device_name(self, provider: PlayerProvider, device_id: str) -> str:
        """Return the display name for a device, falling back to its id."""
        for device in await provider.get_dashboard_devices():
            if device.device_id == device_id:
                return device.name
        return device_id

    async def _resolve_dashboard_url(self, dashboard: DashboardType, player_id: str | None) -> str:
        """
        Build the fully-qualified URL a cast receiver should load to show a dashboard.

        Prefers an externally-reachable https base url (reverse-proxied server, same origin)
        over remote access (via the app.music-assistant.io signaling portal).

        :param dashboard: Dashboard to show.
        :param player_id: Player to show, required when dashboard is NOW_PLAYING.
        :raises ActionUnavailable: If neither an https base url nor remote access is configured.
        """
        route = self._dashboard_route(dashboard, player_id)
        base_url = self.mass.webserver.base_url
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
            return f"/now-playing?player={player_id}"
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

    def _signal_sessions_updated(self) -> None:
        """Signal the current list of dashboard cast sessions to subscribers."""
        self.mass.signal_event(
            EventType.DASHBOARD_SESSIONS_UPDATED, data=list(self._sessions.values())
        )
