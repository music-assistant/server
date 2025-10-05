"""Chromecast Player Provider implementation."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from typing import TYPE_CHECKING, cast

import pychromecast
from pychromecast.controllers.multizone import MultizoneManager
from pychromecast.discovery import CastBrowser, SimpleCastListener

from music_assistant.constants import CONF_ENTRY_MANUAL_DISCOVERY_IPS, VERBOSE_LOG_LEVEL
from music_assistant.models.player_provider import PlayerProvider

from .helpers import ChromecastInfo
from .player import ChromecastPlayer

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
        self.mz_mgr = MultizoneManager()
        # Handle config option for manual IP's
        manual_ip_config = cast("list[str]", config.get_value(CONF_ENTRY_MANUAL_DISCOVERY_IPS.key))
        self.browser = CastBrowser(
            SimpleCastListener(
                add_callback=self._on_chromecast_discovered,
                remove_callback=self._on_chromecast_removed,
                update_callback=self._on_chromecast_discovered,
            ),
            self.mass.aiozc.zeroconf,
            known_hosts=manual_ip_config,
        )
        # set-up pychromecast logging
        if self.logger.isEnabledFor(VERBOSE_LOG_LEVEL):
            logging.getLogger("pychromecast").setLevel(logging.DEBUG)
        else:
            logging.getLogger("pychromecast").setLevel(self.logger.level + 10)

    async def discover_players(self) -> None:
        """Discover Cast players on the network."""
        assert self.browser is not None  # for type checking
        await self.mass.loop.run_in_executor(None, self.browser.start_discovery)

    async def unload(self, is_removed: bool = False) -> None:
        """Handle close/cleanup of the provider."""
        if not self.browser:
            return

        # stop discovery
        def stop_discovery() -> None:
            """Stop the chromecast discovery threads."""
            assert self.browser is not None  # for type checking
            if self.browser._zc_browser:
                with contextlib.suppress(RuntimeError):
                    self.browser._zc_browser.cancel()

            self.browser.host_browser.stop.set()
            self.browser.host_browser.join()

        await self.mass.loop.run_in_executor(None, stop_discovery)

    ### Discovery callbacks

    def _on_chromecast_discovered(self, uuid: str, _: object) -> None:
        """
        Handle Chromecast discovered callback.

        NOTE: NOT async friendly!
        """
        if self.mass.closing:
            return

        assert self.browser is not None  # for type checking
        with self._discover_lock:
            disc_info: CastInfo = self.browser.devices[uuid]

            if disc_info.uuid is None:
                self.logger.error("Discovered chromecast without uuid %s", disc_info)
                return

            player_id = str(disc_info.uuid)

            enabled = self.mass.config.get(f"players/{player_id}/enabled", True)
            if not enabled:
                self.logger.debug("Ignoring disabled player: %s", player_id)
                return

            self.logger.debug("Discovered new or updated chromecast %s", disc_info)

            castplayer = self.mass.players.get(player_id)
            if castplayer:
                assert isinstance(castplayer, ChromecastPlayer)  # for type checking
                # if player was already added, the player will take care of reconnects itself.
                castplayer.cast_info.update(disc_info)
                self.mass.loop.call_soon_threadsafe(castplayer.update_state)
                return
            # new player discovered

            cast_info = ChromecastInfo.from_cast_info(disc_info)
            cast_info.fill_out_missing_chromecast_info(self.mass.aiozc.zeroconf)
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
                self.mass.aiozc.zeroconf,
            )
            # create and register the new ChromeCastPlayer
            asyncio.run_coroutine_threadsafe(
                self._create_and_register_player(player_id, cast_info, chromecast),
                loop=self.mass.loop,
            )

    async def _create_and_register_player(
        self, player_id: str, cast_info: ChromecastInfo, chromecast: pychromecast.Chromecast
    ) -> None:
        """Create and register a new ChromecastPlayer."""
        castplayer = ChromecastPlayer(self, player_id, cast_info=cast_info, chromecast=chromecast)
        await self.mass.players.register_or_update(castplayer)

    def _on_chromecast_removed(self, uuid: str, service: object, cast_info: object) -> None:
        """Handle zeroconf discovery of a removed Chromecast."""
        player_id = str(service[1])
        friendly_name = service[3]
        self.logger.debug("Chromecast removed: %s - %s", friendly_name, player_id)
        # we ignore this event completely as the Chromecast socket client handles this itself
