"""Dashboard support for the Fully Kiosk Browser provider."""

from __future__ import annotations

import asyncio
import functools
from typing import TYPE_CHECKING

from aiohttp import ClientError
from fullykiosk.exceptions import FullyKioskError
from music_assistant_models.dashboard import DashboardDevice
from music_assistant_models.enums import DashboardType
from music_assistant_models.errors import PlayerCommandFailed, PlayerUnavailableError

from .player import FullyKioskPlayer

if TYPE_CHECKING:
    from collections.abc import Callable

    from .provider import FullyKioskProvider

# Fully Kiosk's webview can show any dashboard route, so all types are supported.
SUPPORTED_DASHBOARD_TYPES = {
    DashboardType.NOW_PLAYING,
    DashboardType.PARTY,
    DashboardType.MUSIC_QUIZ,
}


class FullyKioskDashboards:
    """Registers connected Fully Kiosk devices as dashboard endpoints and drives their webview."""

    def __init__(self, provider: FullyKioskProvider) -> None:
        """
        Initialize dashboard handling for a Fully Kiosk provider.

        :param provider: The Fully Kiosk provider owning the managed devices.
        """
        self.provider = provider
        self.mass = provider.mass
        self.logger = provider.logger.getChild("dashboard")
        self._unregister_callbacks: dict[str, Callable[[], None]] = {}
        self._unloaded = False

    def register(self, player: FullyKioskPlayer) -> None:
        """
        Register (or refresh) a connected player as a dashboard endpoint.

        A no-op once the provider has unloaded or the player itself is gone,
        closing a race with a late connect resolving after unload or removal.

        :param player: The connected Fully Kiosk player to register.
        """
        if self._unloaded:
            return
        # a connect awaiting the device response may resolve after the player was
        # removed; never resurrect an endpoint for it
        if self.provider.get_protocol(player.protocol_id) is not player:
            return
        device = DashboardDevice(
            dashboard_id=player.protocol_id,
            name=player.display_name,
            supported_types=set(SUPPORTED_DASHBOARD_TYPES),
            provider_domain_hint=self.provider.domain,
        )
        self._unregister_callbacks[player.protocol_id] = (
            self.mass.dashboard.register_dashboard_handler(
                device,
                functools.partial(self._on_show, player.protocol_id),
                functools.partial(self._on_hide, player.protocol_id),
            )
        )

    def unregister(self, protocol_id: str) -> None:
        """
        Drop a player's dashboard registration.

        :param protocol_id: The id of the player whose registration to drop.
        """
        if unregister_callback := self._unregister_callbacks.pop(protocol_id, None):
            unregister_callback()

    async def unload(self) -> None:
        """Unregister all dashboard endpoints owned by this provider."""
        self._unloaded = True
        for unregister_callback in list(self._unregister_callbacks.values()):
            unregister_callback()
        self._unregister_callbacks.clear()

    async def _on_show(
        self, protocol_id: str, dashboard: DashboardType, target_player_id: str | None
    ) -> None:
        """Wake a Fully Kiosk device and load a Music Assistant dashboard in its webview."""
        player = self.provider.get_protocol(protocol_id)
        if not isinstance(player, FullyKioskPlayer) or player.fully_kiosk is None:
            raise PlayerUnavailableError(f"Fully Kiosk device {protocol_id} is no longer available")
        client = player.fully_kiosk
        url = await self.mass.dashboard.resolve_dashboard_url(
            dashboard, target_player_id, prefer_local=True
        )
        # never log the resolved url: it embeds a one-time viewer code
        self.logger.debug("Showing %s dashboard on %s", dashboard.value, player.display_name)
        try:
            async with asyncio.timeout(15):
                await client.screenOn()
                await client.stopScreensaver()
                await client.toForeground()
                await client.loadUrl(url)
        except FullyKioskError, ClientError, TimeoutError:
            # never chain the caught error: its repr may embed the request url with the password
            raise PlayerUnavailableError(
                f"Unable to show the dashboard on {player.display_name}",
                translation_key="show_dashboard_failed",
                translation_owner=self.provider.translation_owner,
                translation_args=[player.display_name],
            ) from None

    async def _on_hide(self, protocol_id: str) -> None:
        """Return a Fully Kiosk device's webview to its configured start page."""
        player = self.provider.get_protocol(protocol_id)
        if not isinstance(player, FullyKioskPlayer) or player.fully_kiosk is None:
            # nothing connected to this device: it can't be showing a dashboard
            return
        client = player.fully_kiosk
        try:
            async with asyncio.timeout(15):
                await client.loadStartUrl()
        except FullyKioskError, ClientError, TimeoutError:
            # never chain the caught error: its repr may embed the request url with the password
            raise PlayerCommandFailed(
                f"Unable to hide the dashboard on {player.display_name}"
            ) from None
