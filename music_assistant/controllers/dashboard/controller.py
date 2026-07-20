"""Core controller that casts Music Assistant dashboards to display devices."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from music_assistant_models.auth import Scope
from music_assistant_models.enums import ProviderFeature, ProviderType
from music_assistant_models.errors import ActionUnavailable, ProviderUnavailableError

from music_assistant.helpers.api import api_command
from music_assistant.helpers.guest_access import get_or_create_guest_user, get_or_create_join_code
from music_assistant.models.core_controller import CoreController
from music_assistant.models.player_provider import PlayerProvider

if TYPE_CHECKING:
    from music_assistant_models.dashboard import DashboardDevice

    from music_assistant.mass import MusicAssistant

CAST_VIEWER_USERNAME = "cast_viewer"
CAST_VIEWER_DISPLAY_NAME = "Dashboard Cast Viewer"
CAST_CODE_EXPIRY_HOURS = 1


class DashboardController(CoreController):
    """Casts Music Assistant dashboards (e.g. Party mode) to display devices."""

    domain: str = "dashboard"

    def __init__(self, mass: MusicAssistant) -> None:
        """Initialize the DashboardController."""
        super().__init__(mass)
        self.manifest.name = "Dashboard"
        self.manifest.description = "Casts Music Assistant dashboards to display devices."

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

    @api_command("dashboard/show", required_scope=Scope.PLAYERS_CONTROL)
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

        cast_code = await self._get_cast_code()
        await provider.show_dashboard(
            device_id=device_id,
            path=path,
            remote_id=remote_access.remote_id,
            cast_code=cast_code,
        )

    async def _get_cast_code(self) -> str:
        """Mint a one-time code a cast receiver can exchange for a viewer token."""
        user = await get_or_create_guest_user(
            self.mass, CAST_VIEWER_USERNAME, CAST_VIEWER_DISPLAY_NAME
        )
        return await get_or_create_join_code(
            self.mass,
            user,
            expires_in_hours=CAST_CODE_EXPIRY_HOURS,
            max_uses=1,
            device_name="Cast Receiver",
        )
