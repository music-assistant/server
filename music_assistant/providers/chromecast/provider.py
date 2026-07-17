"""Chromecast Player Provider implementation."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from typing import TYPE_CHECKING, cast
from uuid import UUID

import pychromecast
from music_assistant_models.auth import Scope
from music_assistant_models.errors import ActionUnavailable, PlayerUnavailableError
from pychromecast.const import CAST_TYPE_CHROMECAST
from pychromecast.controllers.multizone import MultizoneManager
from pychromecast.discovery import CastBrowser, SimpleCastListener

from music_assistant.constants import (
    CONF_ENABLED,
    CONF_ENTRY_MANUAL_DISCOVERY_IPS,
    VERBOSE_LOG_LEVEL,
)
from music_assistant.helpers.cast_dashboard import get_cast_code
from music_assistant.helpers.json import SerializableType
from music_assistant.models.player_provider import PlayerProvider

from .constants import CONF_DASHBOARD_APP_ID, MASS_APP_ID, MULTICHANNEL_RECHECK_INTERVAL
from .dashboard_controller import MusicAssistantCastController
from .helpers import ChromecastInfo
from .player import ChromecastPlayer
from .sendspin_bridge import SendspinBridgeManager

if TYPE_CHECKING:
    from collections.abc import Callable

    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.enums import ProviderFeature
    from music_assistant_models.provider import ProviderManifest
    from pychromecast.models import CastInfo

    from music_assistant.mass import MusicAssistant


class ChromecastProvider(PlayerProvider):
    """Player provider for Chromecast based players."""

    mz_mgr: MultizoneManager | None = None
    browser: CastBrowser | None = None
    _discover_lock: threading.Lock

    def __init__(
        self,
        mass: MusicAssistant,
        manifest: ProviderManifest,
        config: ProviderConfig,
        supported_features: set[ProviderFeature],
    ) -> None:
        """Handle async initialization of the provider."""
        super().__init__(mass, manifest, config, supported_features)
        self._discover_lock = threading.Lock()
        self._pending_discoveries: set[str] = set()
        self.mz_mgr = MultizoneManager()
        # Handle config option for manual IP's
        manual_ip_config = cast("list[str]", config.get_value(CONF_ENTRY_MANUAL_DISCOVERY_IPS.key))
        self.browser = CastBrowser(
            SimpleCastListener(
                add_callback=self._on_chromecast_discovered,
                remove_callback=self._on_chromecast_removed,
                update_callback=self._on_chromecast_discovered,
            ),
            self.mass.discovery.aiozc.zeroconf,
            known_hosts=manual_ip_config,
        )
        self._discovery_running = False
        self.bridge_manager = SendspinBridgeManager(self)
        # Cast connections opened on-demand for dashboard casting (not registered players)
        self._dashboard_connections: dict[str, pychromecast.Chromecast] = {}
        # One dashboard controller per device: pychromecast keeps registered handlers in a
        # set, so registering a fresh instance per cast would accumulate handlers forever.
        self._dashboard_controllers: dict[str, MusicAssistantCastController] = {}
        self._unregister_dashboard_commands: list[Callable[[], None]] = []
        # set-up pychromecast logging
        if self.logger.isEnabledFor(VERBOSE_LOG_LEVEL):
            logging.getLogger("pychromecast").setLevel(logging.DEBUG)
        else:
            logging.getLogger("pychromecast").setLevel(self.logger.level + 10)

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""
        self._unregister_dashboard_commands = [
            self.mass.register_api_command("chromecast/display_devices", self.get_display_devices),
            self.mass.register_api_command(
                "chromecast/show_dashboard",
                self.show_dashboard,
                required_scope=Scope.PLAYERS_CONTROL,
            ),
        ]

    async def discover_players(self) -> None:
        """Discover Cast players on the network."""
        if self._discovery_running:
            return
        self._discovery_running = True
        assert self.browser is not None  # for type checking
        await self.mass.loop.run_in_executor(None, self.browser.start_discovery)

    async def unload(self, is_removed: bool = False) -> None:
        """Handle close/cleanup of the provider."""
        for unregister in self._unregister_dashboard_commands:
            unregister()
        self._unregister_dashboard_commands.clear()

        for device_id, controller in self._dashboard_controllers.items():
            if (chromecast := self._resolve_dashboard_chromecast(device_id)) is not None:
                with contextlib.suppress(Exception):
                    chromecast.unregister_handler(controller)
        self._dashboard_controllers.clear()

        dashboard_connections = list(self._dashboard_connections.values())
        self._dashboard_connections.clear()
        for chromecast in dashboard_connections:
            if self.mass.closing:
                # Non-blocking disconnect: close socket, don't wait for thread.
                # Socket threads are daemon threads and die on process exit.
                chromecast.disconnect(0)
            else:
                await self.mass.loop.run_in_executor(None, chromecast.disconnect, 10)

        # Stop all Sendspin bridges and remove listeners
        await self.bridge_manager.close()

        # Suppress pychromecast's noisy ERROR logs during disconnect
        # (PyChromecastStopped race in socket thread is unavoidable)
        logging.getLogger("pychromecast").setLevel(logging.CRITICAL)

        # Stop discovery first to prevent new callbacks during disconnect
        if self.browser:

            def stop_discovery() -> None:
                """Stop the chromecast discovery threads."""
                assert self.browser is not None  # for type checking
                if self.browser._zc_browser:
                    with contextlib.suppress(RuntimeError):
                        self.browser._zc_browser.cancel()

                self.browser.host_browser.stop.set()
                self.browser.host_browser.join()

                self._discovery_running = False

            await self.mass.loop.run_in_executor(None, stop_discovery)

    async def get_diagnostics(self) -> dict[str, SerializableType]:
        """Return diagnostics info for this provider to include in diagnostics reports."""
        cast_players = [player for player in self.players if isinstance(player, ChromecastPlayer)]
        models: dict[str, int] = {}
        for cast_player in cast_players:
            model_name = cast_player.cast_info.model_name
            models[model_name] = models.get(model_name, 0) + 1
        return {
            "discovery_running": self._discovery_running,
            "pending_discoveries": len(self._pending_discoveries),
            "cast_groups": sum(
                cast_player.cast_info.is_audio_group for cast_player in cast_players
            ),
            "cast_speakers": sum(
                not cast_player.cast_info.is_audio_group for cast_player in cast_players
            ),
            "models": models,
        }

    async def get_display_devices(self) -> list[dict[str, str]]:
        """
        Return all discovered video-capable Cast devices.

        Includes devices disabled as a player: dashboard casting targets a
        display, not a Music Assistant player.
        """
        assert self.browser is not None  # for type checking
        return [
            {"device_id": str(uuid), "name": cast_info.friendly_name or str(uuid)}
            for uuid, cast_info in self.browser.devices.items()
            if cast_info.cast_type == CAST_TYPE_CHROMECAST
        ]

    async def show_dashboard(self, device_id: str, path: str) -> None:
        """
        Show a Music Assistant dashboard on a Cast display device.

        :param device_id: Device ID as returned by `chromecast/display_devices`.
        :param path: Frontend route to show on the display (e.g. "/party").
        """
        remote_access = self.mass.webserver.remote_access
        if not remote_access.is_enabled or not remote_access.remote_id:
            msg = "Remote access must be enabled to cast dashboards"
            raise ActionUnavailable(
                msg,
                translation_key="remote_access_required",
                translation_owner=self.translation_owner,
            )

        chromecast = await self._get_or_create_chromecast(device_id)
        cast_code = await get_cast_code(self.mass)
        app_id = self.get_config_value(CONF_DASHBOARD_APP_ID, MASS_APP_ID, return_type=str)
        controller = self._dashboard_controllers.get(device_id)
        if controller is not None and controller.supporting_app_id != app_id:
            # configured app id changed: swap out the stale controller
            with contextlib.suppress(Exception):
                chromecast.unregister_handler(controller)
            controller = None
        if controller is None:
            controller = MusicAssistantCastController(app_id)
            self._dashboard_controllers[device_id] = controller
        # handler registration is set-based, so (re)registering the cached
        # controller is a safe no-op on an already-registered socket
        chromecast.register_handler(controller)
        await self.mass.loop.run_in_executor(
            None,
            controller.show_dashboard,
            remote_access.remote_id,
            cast_code,
            path,
            str(self.mass.version),
        )

    ### Discovery callbacks

    def _on_chromecast_discovered(self, uuid: UUID, _: str) -> None:
        """
        Handle Chromecast discovered callback.

        NOTE: Called from pychromecast's discovery thread, NOT async friendly!
        """
        if self.mass.closing:
            return

        assert self.browser is not None  # for type checking

        # Quick lookup and existing-player update under lock (fast path)
        with self._discover_lock:
            disc_info: CastInfo = self.browser.devices[uuid]

            if disc_info.uuid is None:
                self.logger.error("Discovered chromecast without uuid %s", disc_info)  # type: ignore[unreachable]
                return

            player_id = str(disc_info.uuid)

            # If player already registered, just update cast info (fast path)
            castplayer = self.mass.players.get_player(player_id)
            if castplayer:
                assert isinstance(castplayer, ChromecastPlayer)  # for type checking
                castplayer.cast_info.update(disc_info)
                socket_client = castplayer.cc.socket_client
                if socket_client.services != disc_info.services:
                    socket_client.services.clear()
                    socket_client.services.update(disc_info.services)
                self.mass.loop.call_soon_threadsafe(castplayer.update_state)
                # An unavailable player may be a passive multichannel endpoint that
                # slipped past the discovery filter (e.g. incomplete multizone info
                # while the stereo pair was rebooting). Re-evaluate and remove it
                # if it is now positively identified as such.
                if self._should_recheck_multichannel_child(castplayer, player_id):
                    self._pending_discoveries.add(player_id)
                    try:
                        asyncio.run_coroutine_threadsafe(
                            self._recheck_multichannel_child(player_id, disc_info),
                            loop=self.mass.loop,
                        )
                    except RuntimeError:
                        # event loop already closed (shutdown): release the marker
                        self._pending_discoveries.discard(player_id)
                return

            # Prevent duplicate discovery while async setup is in progress
            if player_id in self._pending_discoveries:
                return
            self._pending_discoveries.add(player_id)

        # Blocking work outside the lock to avoid blocking the discovery thread
        # for other devices while HTTP calls are in progress.
        # On success, _create_and_register_player clears _pending_discoveries.
        # On early return or exception, we clean it up here via finally.
        scheduled = False
        try:
            if not self.mass.config.get_raw_player_config_value(player_id, CONF_ENABLED, True):
                self.logger.debug("Ignoring disabled player: %s", player_id)
                return

            self.logger.debug("Discovered new chromecast %s", disc_info)

            cast_info = ChromecastInfo.from_cast_info(disc_info)
            cast_info.fill_out_missing_chromecast_info(self.mass.discovery.aiozc.zeroconf)
            if cast_info.is_dynamic_group:
                self.logger.debug("Discovered a dynamic cast group which will be ignored.")
                return
            if cast_info.is_multichannel_child:
                self.logger.debug(
                    "Discovered a passive (multichannel) endpoint which will be ignored."
                )
                return
            # create new Chromecast instance
            chromecast = pychromecast.get_chromecast_from_cast_info(
                disc_info,
                self.mass.discovery.aiozc.zeroconf,
            )
            # create and register the new ChromeCastPlayer
            asyncio.run_coroutine_threadsafe(
                self._create_and_register_player(player_id, cast_info, chromecast),
                loop=self.mass.loop,
            )
            scheduled = True
        finally:
            if not scheduled:
                self._pending_discoveries.discard(player_id)

    async def _create_and_register_player(
        self, player_id: str, cast_info: ChromecastInfo, chromecast: pychromecast.Chromecast
    ) -> None:
        """Create and register a new ChromecastPlayer."""
        try:
            castplayer = ChromecastPlayer(
                self, player_id, cast_info=cast_info, chromecast=chromecast
            )
            await castplayer.async_setup()
            await self.mass.players.register_or_update(castplayer)
            # Set up Sendspin bridge
            await self.bridge_manager.evaluate_bridge(castplayer)
        finally:
            self._pending_discoveries.discard(player_id)

    def _on_chromecast_removed(
        self,
        uuid: UUID,
        service: str,
        cast_info: CastInfo,
    ) -> None:
        """Handle zeroconf discovery of a removed Chromecast."""
        player_id = str(uuid)
        self.logger.debug("Chromecast removed: %s - %s", cast_info.friendly_name, player_id)
        # we ignore this event completely as the Chromecast socket client handles this itself

    def _should_recheck_multichannel_child(
        self, castplayer: ChromecastPlayer, player_id: str
    ) -> bool:
        """Return whether an unavailable player should be re-evaluated as a multichannel child."""
        if player_id in self._pending_discoveries:
            return False
        if castplayer.available or castplayer.cast_info.is_audio_group:
            return False
        return (
            time.monotonic() - castplayer.last_multichannel_check
        ) > MULTICHANNEL_RECHECK_INTERVAL

    async def _recheck_multichannel_child(self, player_id: str, disc_info: CastInfo) -> None:
        """Re-evaluate a player and remove it if it is a passive multichannel endpoint."""
        try:
            castplayer = self.mass.players.get_player(player_id)
            if not isinstance(castplayer, ChromecastPlayer):
                return
            castplayer.last_multichannel_check = time.monotonic()
            cast_info = ChromecastInfo.from_cast_info(disc_info)
            await asyncio.to_thread(
                cast_info.fill_out_missing_chromecast_info, self.mass.discovery.aiozc.zeroconf
            )
            if cast_info.is_multichannel_child and self.mass.players.get_player(player_id):
                self.logger.info(
                    "Removing %s as it is now identified as a passive (multichannel) endpoint",
                    castplayer.cast_info.friendly_name,
                )
                await self.mass.players.unregister(player_id, permanent=True)
        except Exception:
            self.logger.debug("Multichannel re-check failed for %s", player_id, exc_info=True)
        finally:
            self._pending_discoveries.discard(player_id)

    async def _get_or_create_chromecast(self, device_id: str) -> pychromecast.Chromecast:
        """Resolve a device_id to a connected Chromecast, reusing an existing connection."""
        castplayer = self.mass.players.get_player(device_id)
        if isinstance(castplayer, ChromecastPlayer) and castplayer.cc.socket_client.is_connected:
            return castplayer.cc

        if (chromecast := self._dashboard_connections.get(device_id)) is not None:
            if chromecast.socket_client.is_connected:
                return chromecast
            del self._dashboard_connections[device_id]

        assert self.browser is not None  # for type checking
        try:
            disc_info = self.browser.devices[UUID(device_id)]
        except (KeyError, ValueError) as err:
            msg = f"Unknown Cast device: {device_id}"
            raise PlayerUnavailableError(msg) from err

        def _connect() -> pychromecast.Chromecast:
            """Create the Chromecast connection and wait for it to come up (blocking)."""
            chromecast = pychromecast.get_chromecast_from_cast_info(
                disc_info, self.mass.discovery.aiozc.zeroconf
            )
            chromecast.wait()
            return chromecast

        chromecast = await self.mass.loop.run_in_executor(None, _connect)
        self._dashboard_connections[device_id] = chromecast
        return chromecast

    def _resolve_dashboard_chromecast(self, device_id: str) -> pychromecast.Chromecast | None:
        """Return an existing Chromecast object for the device, without connecting."""
        if (chromecast := self._dashboard_connections.get(device_id)) is not None:
            return chromecast
        castplayer = self.mass.players.get_player(device_id)
        if isinstance(castplayer, ChromecastPlayer):
            return castplayer.cc
        return None
