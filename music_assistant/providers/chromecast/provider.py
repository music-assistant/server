"""Chromecast Player Provider implementation."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from typing import TYPE_CHECKING, cast
from uuid import UUID

from pychromecast.controllers.multizone import MultizoneManager
from pychromecast.discovery import CastBrowser, SimpleCastListener

from music_assistant.constants import (
    CONF_ENTRY_MANUAL_DISCOVERY_IPS,
    CONF_LOG_LEVEL,
    VERBOSE_LOG_LEVEL,
)
from music_assistant.helpers.json import SerializableType
from music_assistant.models.player_provider import PlayerProvider

from .constants import MULTICHANNEL_RECHECK_INTERVAL
from .dashboard import ChromecastDashboards
from .helpers import ChromecastInfo, without_ipv6_host_services
from .player import ChromecastPlayer
from .sendspin_bridge import SendspinBridgeManager

if TYPE_CHECKING:
    from music_assistant_models.config_entries import ConfigEntry, ProviderConfig
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
        # Handle config option for manual IP's. Read a default: at construction the config
        # carries only the server defaults + stored raw values (typed option entries are
        # applied right after by the config controller).
        manual_ip_config = cast(
            "list[str]", config.get_value(CONF_ENTRY_MANUAL_DISCOVERY_IPS.key) or []
        )
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
        self.dashboards = ChromecastDashboards(self)
        self._set_pychromecast_log_level()

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return Config entries to configure this provider."""
        return (CONF_ENTRY_MANUAL_DISCOVERY_IPS,)

    async def discover_players(self) -> None:
        """Start the discovery of Cast devices, or offer again the ones it found unattached."""
        assert self.browser is not None  # for type checking
        if self._discovery_running:
            # the browser reports a device again only when its announcement changes, so a
            # removed or re-enabled one is offered from what the browser already knows
            await self.mass.loop.run_in_executor(None, self._offer_known_devices)
            return
        self._discovery_running = True
        await self.mass.loop.run_in_executor(None, self.browser.start_discovery)

    async def unload(self, is_removed: bool = False) -> None:
        """Handle close/cleanup of the provider."""
        await self.dashboards.unload()

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

    async def update_config(self, config: ProviderConfig, changed_keys: set[str]) -> None:
        """Handle logic when the config is updated."""
        await super().update_config(config, changed_keys)
        # a log level(-only) change does not reload the provider,
        # so realign pychromecast's logger here
        if f"values/{CONF_LOG_LEVEL}" in changed_keys:
            self._set_pychromecast_log_level()

    async def get_diagnostics(self) -> dict[str, SerializableType]:
        """Return diagnostics info for this provider to include in diagnostics reports."""
        cast_players = [player for player in self.protocols if isinstance(player, ChromecastPlayer)]
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

    def _set_pychromecast_log_level(self) -> None:
        """Align pychromecast's log level with the provider's log level."""
        # pychromecast is very chatty at debug level (it logs every socket
        # message of each cast connection), so only pass through its debug
        # logging when verbose logging is enabled
        if self.logger.isEnabledFor(VERBOSE_LOG_LEVEL):
            logging.getLogger("pychromecast").setLevel(logging.DEBUG)
        else:
            logging.getLogger("pychromecast").setLevel(self.logger.level + 10)

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
            # filter here so the socket client and the eureka_info HTTP calls below
            # only receive addresses pychromecast can connect to
            disc_info: CastInfo = without_ipv6_host_services(self.browser.devices[uuid])

            if disc_info.uuid is None:
                self.logger.error("Discovered chromecast without uuid %s", disc_info)  # type: ignore[unreachable]
                return
            disc_uuid: UUID = disc_info.uuid

            protocol_id = str(disc_uuid)

            # (Re-)register as a dashboard endpoint; includes devices disabled as a
            # player, since dashboard casting targets a display, not a MA player.
            self.mass.loop.call_soon_threadsafe(self.dashboards.register, disc_uuid, disc_info)

            # If player already registered, just update cast info (fast path)
            castplayer = self.get_protocol(protocol_id)
            if castplayer:
                assert isinstance(castplayer, ChromecastPlayer)  # for type checking
                castplayer.cast_info.update(disc_info)
                # a protocol that is still attaching connects with the updated cast info
                if castplayer.status_listener is not None:
                    socket_client = castplayer.cc.socket_client
                    if socket_client.services != disc_info.services:
                        socket_client.services.clear()
                        socket_client.services.update(disc_info.services)
                self.mass.loop.call_soon_threadsafe(castplayer.update_state)
                # An unavailable player may be a passive multichannel endpoint that
                # slipped past the discovery filter (e.g. incomplete multizone info
                # while the stereo pair was rebooting). Re-evaluate and remove it
                # if it is now positively identified as such.
                if self._should_recheck_multichannel_child(castplayer, protocol_id):
                    self._pending_discoveries.add(protocol_id)
                    try:
                        asyncio.run_coroutine_threadsafe(
                            self._recheck_multichannel_child(protocol_id, disc_info),
                            loop=self.mass.loop,
                        )
                    except RuntimeError:
                        # event loop already closed (shutdown): release the marker
                        self._pending_discoveries.discard(protocol_id)
                return

            # Prevent duplicate discovery while async setup is in progress
            if protocol_id in self._pending_discoveries:
                return
            self._pending_discoveries.add(protocol_id)

        # Blocking work outside the lock to avoid blocking the discovery thread
        # for other devices while HTTP calls are in progress.
        # On success, _create_and_register_player clears _pending_discoveries.
        # On early return or exception, we clean it up here via finally.
        scheduled = False
        try:
            if not self.mass.players.wants_protocol(protocol_id):
                self.logger.debug("Ignoring disabled player: %s", protocol_id)
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
            # create and register the new ChromeCastPlayer
            asyncio.run_coroutine_threadsafe(
                self._create_and_register_player(protocol_id, cast_info),
                loop=self.mass.loop,
            )
            scheduled = True
        finally:
            if not scheduled:
                self._pending_discoveries.discard(protocol_id)

    def _offer_known_devices(self) -> None:
        """
        Offer every device the discovery found that has no attached protocol.

        NOTE: Runs in a worker thread like the discovery callbacks, NOT async friendly!
        """
        assert self.browser is not None  # for type checking
        for uuid in list(self.browser.devices):
            if not self.get_protocol(str(uuid)):
                self._on_chromecast_discovered(uuid, "")

    async def _create_and_register_player(
        self, protocol_id: str, cast_info: ChromecastInfo
    ) -> None:
        """Create and register a new ChromecastPlayer."""
        try:
            await self.mass.players.register(
                ChromecastPlayer(self, protocol_id, cast_info=cast_info)
            )
        finally:
            self._pending_discoveries.discard(protocol_id)

    def _on_chromecast_removed(
        self,
        uuid: UUID,
        service: str,
        cast_info: CastInfo,
    ) -> None:
        """Handle zeroconf discovery of a removed Chromecast."""
        self.logger.debug("Chromecast removed: %s - %s", cast_info.friendly_name, uuid)
        # we ignore this for the player itself, as the Chromecast socket client handles that,
        # but the dashboard registration has no such fallback and must be dropped explicitly
        self.mass.loop.call_soon_threadsafe(self.dashboards.unregister, uuid)

    def _should_recheck_multichannel_child(
        self, castplayer: ChromecastPlayer, protocol_id: str
    ) -> bool:
        """Return whether an unavailable player should be re-evaluated as a multichannel child."""
        if protocol_id in self._pending_discoveries:
            return False
        if castplayer.available or castplayer.cast_info.is_audio_group:
            return False
        return (
            time.monotonic() - castplayer.last_multichannel_check
        ) > MULTICHANNEL_RECHECK_INTERVAL

    async def _recheck_multichannel_child(self, protocol_id: str, disc_info: CastInfo) -> None:
        """Re-evaluate a player and remove it if it is a passive multichannel endpoint."""
        try:
            castplayer = self.get_protocol(protocol_id)
            if not isinstance(castplayer, ChromecastPlayer):
                return
            castplayer.last_multichannel_check = time.monotonic()
            cast_info = ChromecastInfo.from_cast_info(disc_info)
            await asyncio.to_thread(
                cast_info.fill_out_missing_chromecast_info, self.mass.discovery.aiozc.zeroconf
            )
            if cast_info.is_multichannel_child and self.get_protocol(protocol_id):
                self.logger.info(
                    "Removing %s as it is now identified as a passive (multichannel) endpoint",
                    castplayer.cast_info.friendly_name,
                )
                # its bridge goes with it: the detach stops it, its bridge client is removed
                # along with the protocol it rides on
                await self.mass.players.remove_protocol(protocol_id)
        except Exception:
            self.logger.debug("Multichannel re-check failed for %s", protocol_id, exc_info=True)
        finally:
            self._pending_discoveries.discard(protocol_id)
