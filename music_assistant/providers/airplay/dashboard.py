"""Dashboard support for the AirPlay provider (Apple TV via the tvOS app)."""

from __future__ import annotations

import functools
from typing import TYPE_CHECKING
from urllib.parse import urlencode, urlsplit

from music_assistant_models.dashboard import DashboardDevice
from music_assistant_models.enums import DashboardType
from music_assistant_models.errors import PlayerCommandFailed, PlayerUnavailableError

from .constants import TVOS_APP_BUNDLE_ID
from .control_player import AirPlayControlPlayer
from .helpers import is_apple_tv

if TYPE_CHECKING:
    from collections.abc import Callable

    from .provider import AirPlayProvider

# Version of the launch contract this adapter emits (see tvos/docs/launch-contract.md).
LAUNCH_CONTRACT_VERSION = "1"
# Dashboard types the tvOS app has native views for.
SUPPORTED_DASHBOARD_TYPES = {
    DashboardType.NOW_PLAYING,
    DashboardType.PARTY,
    DashboardType.MUSIC_QUIZ,
}


class AirPlayDashboards:
    """Registers eligible Apple TVs as dashboard endpoints and launches the tvOS app on them."""

    def __init__(self, provider: AirPlayProvider) -> None:
        """
        Initialize dashboard handling for an AirPlay provider.

        :param provider: The AirPlay provider owning the discovered Apple devices.
        """
        self.provider = provider
        self.mass = provider.mass
        self.logger = provider.logger.getChild("dashboard")
        self._unregister_callbacks: dict[str, Callable[[], None]] = {}
        # installed-app bundle ids per player: frozenset when known, None on fetch
        # failure, absent until first fetched
        self._installed_apps: dict[str, frozenset[str] | None] = {}
        # last-seen Companion connection state, to detect (re)connect edges
        self._companion_connected: dict[str, bool] = {}
        self._unloaded = False

    def setup_player(self, player: AirPlayControlPlayer) -> None:
        """
        Start tracking a control player for dashboard eligibility.

        :param player: The control-capable AirPlay player to track.
        """
        if self._unloaded:
            return
        player.on_companion_state_change = functools.partial(self.reconcile, player.player_id)
        self.reconcile(player.player_id)

    def reconcile(self, player_id: str) -> None:
        """
        Re-evaluate a player's dashboard eligibility in the background.

        :param player_id: The player to re-evaluate.
        """
        if self._unloaded:
            return
        self.mass.create_task(
            self._async_reconcile,
            player_id,
            task_id=f"airplay_dashboard_reconcile_{player_id}",
            abort_existing=True,
        )

    def unregister(self, player_id: str) -> None:
        """
        Drop a player's dashboard registration and cached state.

        :param player_id: The player whose registration to drop.
        """
        self._companion_connected.pop(player_id, None)
        self._installed_apps.pop(player_id, None)
        self._unregister_endpoint(player_id)

    async def unload(self) -> None:
        """Unregister all dashboard endpoints owned by this provider."""
        self._unloaded = True
        for unregister_callback in list(self._unregister_callbacks.values()):
            unregister_callback()
        self._unregister_callbacks.clear()
        self._installed_apps.clear()
        self._companion_connected.clear()

    async def _async_reconcile(self, player_id: str) -> None:
        """Evaluate a player's eligibility and (un)register it accordingly."""
        if self._unloaded:
            return
        player = self.mass.players.get_player(player_id)
        if not isinstance(player, AirPlayControlPlayer):
            self.unregister(player_id)
            return
        # a Companion (re)connect re-checks whether the tvOS app is installed
        connected = player.companion_connected
        if connected != self._companion_connected.get(player_id, False):
            self._companion_connected[player_id] = connected
            self._installed_apps.pop(player_id, None)
        if await self._is_eligible(player):
            self._register(player)
        else:
            self._unregister_endpoint(player_id)

    async def _is_eligible(self, player: AirPlayControlPlayer) -> bool:
        """Return whether a control player should be exposed as a dashboard endpoint."""
        if not player.available or not player.enabled:
            return False
        if not is_apple_tv(player.device_info.manufacturer, player.device_info.model):
            return False
        if not player.companion_connected:
            return False
        app_ids = await self._installed_app_ids(player)
        return app_ids is not None and TVOS_APP_BUNDLE_ID in app_ids

    async def _installed_app_ids(self, player: AirPlayControlPlayer) -> frozenset[str] | None:
        """Return the cached installed-app bundle ids, fetching them once if still unknown."""
        if player.player_id not in self._installed_apps:
            app_ids = await player.async_list_installed_app_ids()
            self._installed_apps[player.player_id] = (
                frozenset(app_ids) if app_ids is not None else None
            )
        return self._installed_apps[player.player_id]

    def _register(self, player: AirPlayControlPlayer) -> None:
        """Register (or refresh) a player as a dashboard endpoint with the controller."""
        # a reconcile awaiting the app list may resolve after unload(); never resurrect
        if self._unloaded:
            return
        device = DashboardDevice(
            dashboard_id=self._dashboard_id(player.player_id),
            name=player.display_name,
            supported_types=set(SUPPORTED_DASHBOARD_TYPES),
            provider_domain_hint=self.provider.domain,
        )
        self._unregister_callbacks[player.player_id] = (
            self.mass.dashboard.register_dashboard_handler(
                device,
                functools.partial(self._on_show, player.player_id),
                functools.partial(self._on_hide, player.player_id),
            )
        )

    def _unregister_endpoint(self, player_id: str) -> None:
        """Unregister a player's dashboard endpoint if it is registered."""
        if unregister_callback := self._unregister_callbacks.pop(player_id, None):
            unregister_callback()

    async def _on_show(
        self, player_id: str, dashboard: DashboardType, target_player_id: str | None
    ) -> None:
        """
        Launch the tvOS app on an Apple TV to show a dashboard.

        :param player_id: The Apple TV endpoint to show the dashboard on.
        :param dashboard: Dashboard to show.
        :param target_player_id: Player to show, required when the dashboard is NOW_PLAYING.
        :raises PlayerUnavailableError: If the Apple TV is gone or the launch fails.
        """
        player = self.mass.players.get_player(player_id)
        if not isinstance(player, AirPlayControlPlayer):
            raise PlayerUnavailableError(f"Apple TV {player_id} is no longer available")
        target_url = await self.mass.dashboard.resolve_dashboard_url(
            dashboard, target_player_id, prefer_local=True
        )
        dashboard_id = self._dashboard_id(player_id)
        launch_uri = self._build_launch_uri(dashboard, target_url, dashboard_id)
        # never log the full target url or the one-time viewer code embedded in it
        self.logger.debug(
            "Showing %s dashboard %s on %s (target origin %s)",
            dashboard.value,
            dashboard_id,
            player.display_name,
            self._target_origin(target_url),
        )
        try:
            await player.wake()
            await player.async_launch_app(launch_uri)
        except PlayerCommandFailed as err:
            raise PlayerUnavailableError(
                f"Unable to show the dashboard on {player.display_name}",
                translation_key="show_dashboard_failed",
                translation_owner=self.provider.translation_owner,
                translation_args=[player.display_name],
            ) from err

    async def _on_hide(self, player_id: str) -> None:
        """
        Handle a hide request for an Apple TV endpoint.

        :param player_id: The Apple TV endpoint the dashboard was showing on.
        """
        # hide is session-driven: the app returns to idle when its session disappears.
        # pyatv has no foreground-app control, so there is nothing to do on the device.
        self.logger.debug("Hide requested for dashboard %s (session-driven)", player_id)

    def _build_launch_uri(
        self, dashboard: DashboardType, target_url: str, dashboard_id: str
    ) -> str:
        """Build the versioned tvOS launch URI wrapping the resolved dashboard url."""
        query = urlencode(
            {
                "v": LAUNCH_CONTRACT_VERSION,
                "type": dashboard.value,
                "target": target_url,
                "dashboard_id": dashboard_id,
            }
        )
        return f"musicassistant://dashboard/show?{query}"

    @staticmethod
    def _dashboard_id(player_id: str) -> str:
        """Return the dashboard endpoint id for an AirPlay player."""
        return f"airplay_{player_id}"

    @staticmethod
    def _target_origin(url: str) -> str:
        """Return only the scheme://host[:port] of a url, for safe logging."""
        parts = urlsplit(url)
        if parts.scheme and parts.netloc:
            return f"{parts.scheme}://{parts.netloc}"
        return "unknown"
