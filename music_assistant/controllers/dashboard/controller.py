"""Core controller that casts Music Assistant dashboards to display devices."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from music_assistant_models.auth import Scope
from music_assistant_models.dashboard import DashboardSession
from music_assistant_models.enums import EventType, ProviderFeature, ProviderType
from music_assistant_models.errors import (
    ActionUnavailable,
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
    async def show_dashboard(self, provider_instance: str, device_id: str, path: str) -> None:
        """
        Show a Music Assistant dashboard on a display device.

        :param provider_instance: Instance ID of the provider owning the display device.
        :param device_id: Provider-scoped device ID, as returned by `dashboard/devices`.
        :param path: Frontend route to show on the display (e.g. "/party").
        """
        provider = self.mass.get_provider(provider_instance, provider_type=PlayerProvider)
        if provider is None:
            msg = f"Provider not found: {provider_instance}"
            raise ProviderUnavailableError(msg)
        provider.check_feature(ProviderFeature.SHOW_DASHBOARD)

        remote_access = self.mass.webserver.remote_access
        if not remote_access.is_enabled or not remote_access.remote_id:
            msg = "Remote access must be enabled to cast dashboards"
            raise ActionUnavailable(
                msg,
                translation_key="remote_access_required",
                translation_owner=self.translation_owner,
            )

        dashboard_code = await self._get_dashboard_code()
        await provider.show_dashboard(
            device_id=device_id,
            path=path,
            remote_id=remote_access.remote_id,
            dashboard_code=dashboard_code,
        )
        device_name = device_id
        for device in await provider.get_dashboard_devices():
            if device.device_id == device_id:
                device_name = device.name
                break
        self._sessions[(provider_instance, device_id)] = DashboardSession(
            device_id=device_id, provider_instance=provider_instance, name=device_name, path=path
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
        if provider is None:
            msg = f"Provider not found: {provider_instance}"
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

    def _signal_sessions_updated(self) -> None:
        """Signal the current list of dashboard cast sessions to subscribers."""
        self.mass.signal_event(
            EventType.DASHBOARD_SESSIONS_UPDATED, data=list(self._sessions.values())
        )
