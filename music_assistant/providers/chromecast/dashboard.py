"""Dashboard casting support for the Chromecast provider."""

from __future__ import annotations

import functools
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast
from uuid import UUID

import pychromecast
from music_assistant_models.dashboard import DashboardDevice
from music_assistant_models.enums import DashboardType, PlaybackState
from music_assistant_models.errors import PlayerUnavailableError
from pychromecast.const import CAST_TYPE_CHROMECAST
from pychromecast.socket_client import CONNECTION_STATUS_CONNECTED, CONNECTION_STATUS_LOST

from .constants import MASS_APP_ID
from .helpers import send_hide_dashboard, send_show_dashboard
from .player import ChromecastPlayer

if TYPE_CHECKING:
    import asyncio
    from collections.abc import Callable

    from pychromecast.controllers.receiver import CastStatus
    from pychromecast.controllers.receiver import CastStatusListener as ReceiverStatusListener
    from pychromecast.models import CastInfo
    from pychromecast.socket_client import ConnectionStatus, ConnectionStatusListener

    from .provider import ChromecastProvider

# Seconds to wait for an on-demand dashboard cast connection before giving up.
DASHBOARD_CONNECT_TIMEOUT = 10.0

# Seconds a lost connection is allowed to recover before we consider the dashboard gone.
DASHBOARD_CONNECTION_LOSS_GRACE = 60.0

# Dashboard types the chromecast receiver app has routes for.
SUPPORTED_DASHBOARD_TYPES = {
    DashboardType.PARTY,
    DashboardType.NOW_PLAYING,
    DashboardType.MUSIC_QUIZ,
}


@dataclass
class _ActiveDashboardCast:
    """Tracks a dashboard currently showing on a Cast device, to detect it dying externally."""

    cast_session_id: str | None
    listener: _DashboardCastListener
    loss_timer: asyncio.TimerHandle | None = None


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
        self._unregister_callbacks: dict[str, Callable[[], None]] = {}
        # Cast connections opened on-demand for dashboard casting (not registered players)
        self._dashboard_connections: dict[str, pychromecast.Chromecast] = {}
        self._active_casts: dict[str, _ActiveDashboardCast] = {}
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
        self._register_device(str(uuid), cast_info)

    def unregister(self, uuid: UUID) -> None:
        """
        Unregister a Cast device that is no longer discovered.

        :param uuid: Cast device uuid, as reported by discovery.
        """
        device_id = str(uuid)
        if unregister_callback := self._unregister_callbacks.pop(device_id, None):
            unregister_callback()
        if chromecast := self._dashboard_connections.pop(device_id, None):
            # non-blocking: close the socket, the daemon thread exits on its own
            chromecast.disconnect(0)
        self._drop_active_cast(device_id)

    async def unload(self) -> None:
        """Unregister all dashboard endpoints and disconnect cached on-demand connections."""
        self._unloaded = True
        for unregister_callback in list(self._unregister_callbacks.values()):
            unregister_callback()
        self._unregister_callbacks.clear()

        for device_id in list(self._active_casts):
            self._drop_active_cast(device_id)

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
        device = DashboardDevice(
            dashboard_id=f"chromecast_{device_id}",
            name=cast_info.friendly_name or device_id,
            supported_types=SUPPORTED_DASHBOARD_TYPES,
            provider_domain_hint=self.provider.domain,
        )
        self._unregister_callbacks[device_id] = self.mass.dashboard.register_dashboard_handler(
            device,
            functools.partial(self._on_show, device_id),
            functools.partial(self._on_hide, device_id),
        )

    async def _on_show(
        self, device_id: str, dashboard: DashboardType, player_id: str | None
    ) -> None:
        """
        Show a Music Assistant dashboard on a Cast display device.

        :param device_id: Cast device uuid (as string) to show the dashboard on.
        :param dashboard: Dashboard to show.
        :param player_id: Player to show, when dashboard is NOW_PLAYING.
        """
        player = self.mass.players.get_player(device_id)
        castplayer = player if isinstance(player, ChromecastPlayer) else None
        release_on_the_wire = False
        if castplayer is not None:
            # an earlier stop on the player may still have a release of the receiver
            # app pending, which would close the dashboard we are about to show
            castplayer.cancel_pending_app_quit()
            # a release that is already on the wire will close the app anyway, so the
            # receiver's 'already running' short-circuit has to be bypassed
            release_on_the_wire = castplayer.app_quit_sent
        # also launch fresh when we track no active cast on the device: an "already
        # running" receiver may be backgrounded (Android TV's launcher hides it) and
        # only a real launch brings it back to the foreground. never force past playback
        # though: relaunching tears it down, and a playing device is showing the receiver
        # anyway. a paused one may well be backgrounded, so it is worth the relaunch
        is_playing = castplayer is not None and castplayer.playback_state == PlaybackState.PLAYING
        force_launch = release_on_the_wire or (
            device_id not in self._active_casts and not is_playing
        )
        url = await self.mass.dashboard.resolve_dashboard_url(
            dashboard, player_id, dashboard_id=f"chromecast_{device_id}"
        )
        chromecast = await self._get_or_create_chromecast(device_id)
        try:
            await self.mass.loop.run_in_executor(
                None,
                functools.partial(send_show_dashboard, chromecast, url, force_launch=force_launch),
            )
        except TimeoutError as err:
            # the helper's message already carries device name + failure reason
            raise PlayerUnavailableError(
                str(err),
                translation_key="show_dashboard_failed",
                translation_owner=self.provider.translation_owner,
                translation_args=[chromecast.name],
            ) from err

        if release_on_the_wire and castplayer is not None:
            # this launch replaced the session the recorded release was closing
            castplayer.app_quit_sent = False

        self._drop_active_cast(device_id)
        session_id = chromecast.status.session_id if chromecast.status else None
        listener = _DashboardCastListener(self, device_id)
        self._active_casts[device_id] = _ActiveDashboardCast(
            cast_session_id=session_id, listener=listener
        )
        chromecast.register_status_listener(cast("ReceiverStatusListener", listener))
        chromecast.register_connection_listener(cast("ConnectionStatusListener", listener))

    async def _on_hide(self, device_id: str) -> None:
        """
        Hide a Music Assistant dashboard from a Cast display device.

        :param device_id: Cast device uuid (as string) to hide the dashboard from.
        """
        self._drop_active_cast(device_id)

        chromecast = self._get_existing_chromecast(device_id)
        if chromecast is None:
            # nothing connected to this device: it can't be showing a dashboard
            return

        hidden = await self.mass.loop.run_in_executor(None, send_hide_dashboard, chromecast)
        if not hidden:
            self.logger.debug("No dashboard was showing on %s", chromecast.name)

        # only tear down a connection we opened on-demand; never an active player's own cc
        on_demand = self._dashboard_connections.pop(device_id, None)
        if on_demand is not None:
            await self.mass.loop.run_in_executor(None, on_demand.disconnect, 10)

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

    def _handle_cast_status(self, device_id: str, status: CastStatus) -> None:
        """Detect the dashboard receiver app being replaced or restarted, on the event loop."""
        entry = self._active_casts.get(device_id)
        if entry is None:
            return
        if status.app_id != MASS_APP_ID or status.session_id != entry.cast_session_id:
            reason = f"the receiver app was closed (active app: {status.display_name or status.app_id or 'none'})"
            self._session_lost(device_id, reason)

    def _handle_connection_status(self, device_id: str, status: ConnectionStatus) -> None:
        """Arm/disarm the connection-loss grace timer, on the event loop."""
        entry = self._active_casts.get(device_id)
        if entry is None:
            return
        if status.status == CONNECTION_STATUS_LOST:
            if entry.loss_timer is None:
                entry.loss_timer = self.mass.loop.call_later(
                    DASHBOARD_CONNECTION_LOSS_GRACE,
                    self._session_lost,
                    device_id,
                    "connection to the device was lost",
                )
        elif status.status == CONNECTION_STATUS_CONNECTED:
            # the receiver status that follows reconnection re-verifies the app itself
            self._cancel_loss_timer(device_id)

    def _session_lost(self, device_id: str, reason: str) -> None:
        """Report a dashboard session as lost and drop the on-demand connection, if any."""
        if device_id not in self._active_casts:
            return
        self._drop_active_cast(device_id)
        self.mass.dashboard.end_session(f"chromecast_{device_id}", reason)

        if chromecast := self._dashboard_connections.pop(device_id, None):
            # non-blocking: close the socket, the daemon thread exits on its own
            chromecast.disconnect(0)

    def _cancel_loss_timer(self, device_id: str) -> None:
        """Cancel the pending connection-loss timer for device_id, if any."""
        entry = self._active_casts.get(device_id)
        if entry is not None and entry.loss_timer is not None:
            entry.loss_timer.cancel()
            entry.loss_timer = None

    def _drop_active_cast(self, device_id: str) -> None:
        """Stop tracking device_id: cancel its loss timer and invalidate its listener."""
        entry = self._active_casts.pop(device_id, None)
        if entry is None:
            return
        if entry.loss_timer is not None:
            entry.loss_timer.cancel()
        entry.listener.invalidate()


class _DashboardCastListener:
    """
    Watches a Cast device to detect its dashboard receiver dying externally.

    pychromecast has no API to unregister a listener from a Chromecast object,
    so a superseded listener is invalidated instead: it stays registered but
    its callbacks become no-ops.
    """

    def __init__(self, dashboards: ChromecastDashboards, device_id: str) -> None:
        """Bind this listener to the dashboards owner and the Cast device it watches."""
        self.dashboards = dashboards
        self.device_id = device_id
        self._valid = True

    def invalidate(self) -> None:
        """Stop forwarding callbacks from this listener."""
        self._valid = False

    def new_cast_status(self, status: CastStatus) -> None:
        """Handle updated CastStatus (called from the pychromecast socket thread)."""
        if not self._valid or self.dashboards.mass.closing:
            return
        self.dashboards.mass.loop.call_soon_threadsafe(
            self.dashboards._handle_cast_status, self.device_id, status
        )

    def new_connection_status(self, status: ConnectionStatus) -> None:
        """Handle updated ConnectionStatus (called from the pychromecast socket thread)."""
        if not self._valid or self.dashboards.mass.closing:
            return
        self.dashboards.mass.loop.call_soon_threadsafe(
            self.dashboards._handle_connection_status, self.device_id, status
        )
