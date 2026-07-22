"""Dashboard casting support for the Chromecast provider."""

from __future__ import annotations

import functools
from typing import TYPE_CHECKING
from uuid import UUID

import pychromecast
from music_assistant_models.dashboard import DashboardDevice
from music_assistant_models.enums import DashboardType
from music_assistant_models.errors import PlayerUnavailableError
from pychromecast.const import CAST_TYPE_CHROMECAST

from .helpers import send_hide_dashboard, send_show_dashboard
from .player import ChromecastPlayer

if TYPE_CHECKING:
    from collections.abc import Callable

    from pychromecast.models import CastInfo

    from .provider import ChromecastProvider

# Seconds to wait for an on-demand dashboard cast connection before giving up.
DASHBOARD_CONNECT_TIMEOUT = 10.0

# Dashboard types the chromecast receiver app has routes for.
SUPPORTED_DASHBOARD_TYPES = {DashboardType.PARTY, DashboardType.NOW_PLAYING}


class ChromecastDashboards:
    """Registers video-capable Cast devices as dashboard endpoints and casts to them."""

    def __init__(self, provider: ChromecastProvider) -> None:
        """
        Initialize dashboard handling for a Chromecast provider.

        :param provider: The Chromecast provider owning the discovered Cast devices.
        """
        self.provider = provider
        self.mass = provider.mass
        self.logger = provider.logger.getChild("dashboard")
        # last known CastInfo per device_id, so a player appearing/disappearing
        # later can re-register without needing a fresh discovery callback
        self._cast_info: dict[str, CastInfo] = {}
        self._unregister_callbacks: dict[str, Callable[[], None]] = {}
        # Cast connections opened on-demand for dashboard casting (not registered players)
        self._dashboard_connections: dict[str, pychromecast.Chromecast] = {}
        self._unloaded = False

    def register(self, uuid: UUID, cast_info: CastInfo) -> None:
        """
        Register (or refresh) a discovered Cast device as a dashboard endpoint.

        Non video-capable devices (audio speakers/groups) are ignored. A no-op
        once the provider has unloaded, closing a race with late discovery callbacks.

        :param uuid: Cast device uuid, as reported by discovery.
        :param cast_info: Discovery info for the Cast device.
        """
        if self._unloaded or cast_info.cast_type != CAST_TYPE_CHROMECAST:
            return
        device_id = str(uuid)
        self._cast_info[device_id] = cast_info
        self._register_device(device_id, cast_info)

    def unregister(self, uuid: UUID) -> None:
        """
        Unregister a Cast device that is no longer discovered.

        :param uuid: Cast device uuid, as reported by discovery.
        """
        device_id = str(uuid)
        self._cast_info.pop(device_id, None)
        if unregister_callback := self._unregister_callbacks.pop(device_id, None):
            unregister_callback()

    def refresh_player_link(self, player_id: str) -> None:
        """
        Re-register a dashboard endpoint after its linked MA player appeared or disappeared.

        Graceful no-op if the device was never registered as a dashboard endpoint.

        :param player_id: Cast device uuid (as string) whose MA player registration changed.
        """
        if cast_info := self._cast_info.get(player_id):
            self._register_device(player_id, cast_info)

    async def unload(self) -> None:
        """Unregister all dashboard endpoints and disconnect cached on-demand connections."""
        self._unloaded = True
        for unregister_callback in list(self._unregister_callbacks.values()):
            unregister_callback()
        self._unregister_callbacks.clear()
        self._cast_info.clear()

        dashboard_connections = list(self._dashboard_connections.values())
        self._dashboard_connections.clear()
        for chromecast in dashboard_connections:
            if self.mass.closing:
                # Non-blocking disconnect: close socket, don't wait for thread.
                # Socket threads are daemon threads and die on process exit.
                chromecast.disconnect(0)
            else:
                await self.mass.loop.run_in_executor(None, chromecast.disconnect, 10)

    def _register_device(self, device_id: str, cast_info: CastInfo) -> None:
        """Build a DashboardDevice for device_id and (re-)register it with the controller."""
        player_id = device_id if self.mass.players.get_player(device_id) else None
        device = DashboardDevice(
            dashboard_id=f"chromecast_{device_id}",
            name=cast_info.friendly_name or device_id,
            supported_types=SUPPORTED_DASHBOARD_TYPES,
            icon="cast",
            player_id=player_id,
        )
        self._unregister_callbacks[device_id] = self.mass.dashboard.register_dashboard_handler(
            device,
            functools.partial(self._on_show, device_id),
            functools.partial(self._on_hide, device_id),
        )

    async def _on_show(
        self, device_id: str, _dashboard: DashboardType, url: str, _player_id: str | None
    ) -> None:
        """
        Show a Music Assistant dashboard on a Cast display device.

        :param device_id: Cast device uuid (as string) to show the dashboard on.
        :param url: Fully-qualified dashboard URL for the receiver to load.
        """
        chromecast = await self._get_or_create_chromecast(device_id)
        try:
            await self.mass.loop.run_in_executor(None, send_show_dashboard, chromecast, url)
        except TimeoutError as err:
            msg = f"Timed out launching app on {chromecast.name}"
            raise PlayerUnavailableError(
                msg,
                translation_key="app_launch_timeout",
                translation_owner=self.provider.translation_owner,
                translation_args=[chromecast.name],
            ) from err

    async def _on_hide(self, device_id: str) -> None:
        """
        Hide a Music Assistant dashboard from a Cast display device.

        :param device_id: Cast device uuid (as string) to hide the dashboard from.
        """
        chromecast = self._get_existing_chromecast(device_id)
        if chromecast is None:
            # nothing connected to this device: it can't be showing a dashboard
            return

        hidden = await self.mass.loop.run_in_executor(None, send_hide_dashboard, chromecast)
        if not hidden:
            self.logger.debug("No dashboard was showing on %s", chromecast.name)

        if device_id in self._dashboard_connections:
            del self._dashboard_connections[device_id]
            await self.mass.loop.run_in_executor(None, chromecast.disconnect, 10)

    async def _get_or_create_chromecast(self, device_id: str) -> pychromecast.Chromecast:
        """Resolve a device_id to a connected Chromecast, reusing an existing connection."""
        castplayer = self.mass.players.get_player(device_id)
        if isinstance(castplayer, ChromecastPlayer) and castplayer.cc.socket_client.is_connected:
            return castplayer.cc

        if (chromecast := self._dashboard_connections.get(device_id)) is not None:
            if chromecast.socket_client.is_connected:
                return chromecast
            del self._dashboard_connections[device_id]

        assert self.provider.browser is not None  # for type checking
        try:
            disc_info = self.provider.browser.devices[UUID(device_id)]
        except (KeyError, ValueError) as err:
            msg = f"Unknown Cast device: {device_id}"
            raise PlayerUnavailableError(msg) from err

        def _connect() -> pychromecast.Chromecast:
            """Create the Chromecast connection and wait for it to come up (blocking)."""
            chromecast = pychromecast.get_chromecast_from_cast_info(
                disc_info, self.mass.discovery.aiozc.zeroconf
            )
            chromecast.wait(timeout=DASHBOARD_CONNECT_TIMEOUT)
            if not chromecast.socket_client.is_connected:
                chromecast.disconnect(0)
                msg = f"Timed out connecting to Cast device: {disc_info.friendly_name}"
                raise PlayerUnavailableError(msg)
            return chromecast

        chromecast = await self.mass.loop.run_in_executor(None, _connect)
        self._dashboard_connections[device_id] = chromecast
        return chromecast

    def _get_existing_chromecast(self, device_id: str) -> pychromecast.Chromecast | None:
        """Return an already-connected Chromecast for device_id, without opening a new one."""
        castplayer = self.mass.players.get_player(device_id)
        if isinstance(castplayer, ChromecastPlayer) and castplayer.cc.socket_client.is_connected:
            return castplayer.cc

        if (chromecast := self._dashboard_connections.get(device_id)) is not None:
            if chromecast.socket_client.is_connected:
                return chromecast
            del self._dashboard_connections[device_id]

        return None
