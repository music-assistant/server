"""Sonos S1 Player Provider implementation."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, cast

from music_assistant_models.enums import PlayerFeature
from requests.exceptions import RequestException
from soco import SoCo, events_asyncio, zonegroupstate
from soco import config as soco_config
from soco.discovery import discover

from music_assistant.constants import CONF_ENTRY_MANUAL_DISCOVERY_IPS, VERBOSE_LOG_LEVEL
from music_assistant.models.player_provider import PlayerProvider

from .constants import CONF_HOUSEHOLD_ID, CONF_NETWORK_SCAN, SUBSCRIPTION_TIMEOUT
from .player import SonosPlayer


class SonosPlayerProvider(PlayerProvider):
    """Sonos S1 Player Provider for legacy Sonos speakers."""

    _discovery_running: bool = False
    _discovery_reschedule_timer: asyncio.TimerHandle | None = None

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialize the provider."""
        super().__init__(*args, **kwargs)

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        # Configure SoCo to use async event system
        soco_config.EVENTS_MODULE = events_asyncio
        zonegroupstate.EVENT_CACHE_TIMEOUT = SUBSCRIPTION_TIMEOUT
        self.topology_condition = asyncio.Condition()

        # Set up SoCo logging
        if self.logger.isEnabledFor(VERBOSE_LOG_LEVEL):
            logging.getLogger("soco").setLevel(logging.DEBUG)
        else:
            logging.getLogger("soco").setLevel(self.logger.level + 10)

        # Disable SoCo cache to prevent stale data
        soco_config.CACHE_ENABLED = False

        # Start discovery
        await self.discover_players()

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/close of the provider."""
        if self._discovery_reschedule_timer:
            self._discovery_reschedule_timer.cancel()
            self._discovery_reschedule_timer = None
        # await any in-progress discovery
        while self._discovery_running:
            await asyncio.sleep(0.5)
        # Clean up subscriptions and connections
        for sonos_player in self.mass.players.all(provider_filter=self.instance_id):
            sonos_player = cast("SonosPlayer", sonos_player)
            await sonos_player.offline()
        # Stop the async event listener
        if events_asyncio.event_listener:
            await events_asyncio.event_listener.async_stop()

    async def discover_players(self) -> None:
        """Discover Sonos players on the network."""
        if self._discovery_running:
            return

        # Handle config option for manual IP's
        manual_ip_config = cast(
            "list[str]", self.config.get_value(CONF_ENTRY_MANUAL_DISCOVERY_IPS.key)
        )
        for ip_address in manual_ip_config:
            try:
                player = SoCo(ip_address)
                await self._setup_player(player)
            except RequestException as err:
                # player is offline
                self.logger.debug("Failed to add SonosPlayer %s: %s", player, err)
            except Exception as err:
                self.logger.warning(
                    "Failed to add SonosPlayer %s: %s",
                    player,
                    err,
                    exc_info=err if self.logger.isEnabledFor(10) else None,
                )

        allow_network_scan = self.config.get_value(CONF_NETWORK_SCAN)
        if not (household_id := self.config.get_value(CONF_HOUSEHOLD_ID)):
            household_id = "Sonos"

        def do_discover() -> None:
            """Run discovery and add players in executor thread."""
            self._discovery_running = True
            try:
                self.logger.debug("Sonos discovery started...")
                discovered_devices: set[SoCo] = (
                    discover(
                        timeout=30, household_id=household_id, allow_network_scan=allow_network_scan
                    )
                    or set()
                )

                # process new players
                for soco in discovered_devices:
                    try:
                        asyncio.run_coroutine_threadsafe(
                            self._setup_player(soco), self.mass.loop
                        ).result()
                    except RequestException as err:
                        # player is offline
                        self.logger.debug("Failed to add SonosPlayer %s: %s", soco, err)
                    except Exception as err:
                        self.logger.warning(
                            "Failed to add SonosPlayer %s: %s",
                            soco,
                            err,
                            exc_info=err if self.logger.isEnabledFor(10) else None,
                        )
            finally:
                self._discovery_running = False

        await asyncio.to_thread(do_discover)

        def reschedule() -> None:
            self._discovery_reschedule_timer = None
            self.mass.create_task(self.discover_players())

        # reschedule self once finished
        self._discovery_reschedule_timer = self.mass.loop.call_later(1800, reschedule)

    async def _setup_player(self, soco: SoCo) -> None:
        """Set up a discovered Sonos player."""
        player_id = soco.uid

        if existing := cast("SonosPlayer", self.mass.players.get(player_id=player_id)):
            if existing.soco.ip_address != soco.ip_address:
                existing.update_ip(soco.ip_address)
            return
        if not soco.is_visible:
            return
        enabled = self.mass.config.get_raw_player_config_value(player_id, "enabled", True)
        if not enabled:
            self.logger.debug("Ignoring disabled player: %s", player_id)
            return
        try:
            # Ensure speaker info is available during setup
            if not soco.speaker_info:
                soco.get_speaker_info(True, timeout=7)
            sonos_player = SonosPlayer(self, soco)
            if not soco.fixed_volume:
                sonos_player._attr_supported_features = {
                    *sonos_player._attr_supported_features,
                    PlayerFeature.VOLUME_SET,
                }

            # Register with Music Assistant
            await sonos_player.setup()

        except Exception as err:
            self.logger.error("Error setting up Sonos player %s: %s", player_id, err)
