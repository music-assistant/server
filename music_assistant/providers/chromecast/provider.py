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
from pychromecast.controllers.multizone import MultizoneManager
from pychromecast.discovery import CastBrowser, SimpleCastListener

from music_assistant.constants import (
    CONF_ENABLED,
    CONF_ENTRY_MANUAL_DISCOVERY_IPS,
    VERBOSE_LOG_LEVEL,
)
from music_assistant.helpers.json import SerializableType
from music_assistant.models.player_provider import PlayerProvider

from .constants import MULTICHANNEL_RECHECK_INTERVAL
from .helpers import ChromecastInfo
from .player import ChromecastPlayer
from .sendspin_bridge import SendspinBridgeManager

if TYPE_CHECKING:
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
        # set-up pychromecast logging
        if self.logger.isEnabledFor(VERBOSE_LOG_LEVEL):
            logging.getLogger("pychromecast").setLevel(logging.DEBUG)
        else:
            logging.getLogger("pychromecast").setLevel(self.logger.level + 10)

    async def discover_players(self) -> None:
        """Discover Cast players on the network."""
        if self._discovery_running:
            return
        self._discovery_running = True
        assert self.browser is not None  # for type checking
        await self.mass.loop.run_in_executor(None, self.browser.start_discovery)

    async def unload(self, is_removed: bool = False) -> None:
        """Handle close/cleanup of the provider."""
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
                # Re-link the running socket client to the browser's current
                # mDNS services. If the device dropped off the network entirely
                # and re-announced (e.g. a cast group whose leadership moved to
                # another speaker, which changes the mDNS instance name), the
                # CastBrowser has created a fresh services set for this uuid;
                # the long-lived SocketClient still iterates its original,
                # now-orphaned set and retries the dead host/port forever.
                # SocketClient explicitly supports in-place modification of the
                # set and picks up changes on the next reconnect attempt.
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
