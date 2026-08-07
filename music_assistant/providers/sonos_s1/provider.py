"""Sonos S1 Player Provider implementation."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, cast

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType, PlayerFeature
from requests.exceptions import RequestException
from soco import SoCo, events_asyncio, zonegroupstate
from soco import config as soco_config
from soco.discovery import discover, scan_network

from music_assistant.constants import CONF_ENTRY_MANUAL_DISCOVERY_IPS, VERBOSE_LOG_LEVEL
from music_assistant.helpers.util import format_ip_for_url
from music_assistant.models.player_provider import PlayerProvider

from .constants import (
    CONF_HOUSEHOLD_ID,
    CONF_NETWORK_SCAN,
    DISCOVERY_INTERVAL,
    SUBSCRIPTION_TIMEOUT,
)
from .player import SonosPlayer


class SonosPlayerProvider(PlayerProvider):
    """Sonos S1 Player Provider for legacy Sonos speakers."""

    _discovery_running: bool = False

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Initialize the provider."""
        super().__init__(*args, **kwargs)
        self._discovery_task_id: str = f"sonos_s1_discovery_{self.instance_id}"
        self._unloaded: bool = False

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return Config entries to setup this provider."""
        household_ids = await self._discover_household_ids()
        return (
            CONF_ENTRY_MANUAL_DISCOVERY_IPS,
            ConfigEntry(
                key=CONF_NETWORK_SCAN,
                type=ConfigEntryType.BOOLEAN,
                default_value=False,
            ),
            ConfigEntry(
                key=CONF_HOUSEHOLD_ID,
                type=ConfigEntryType.STRING,
                default_value=household_ids[0] if household_ids else None,
                advanced=True,
                required=False,
            ),
        )

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
        # a discovery already running in its worker thread cannot be interrupted, so the
        # flag is what stops it from arming a new reschedule once it resumes
        self._unloaded = True
        # a reschedule that already fired lives on as a task under the same id,
        # so both are needed to cover the pending and the running case
        self.mass.cancel_timer(self._discovery_task_id)
        self.mass.cancel_task(self._discovery_task_id)
        # await any in-progress discovery
        while self._discovery_running:
            await asyncio.sleep(0.5)
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

        if self._unloaded:
            return
        # reschedule self once finished, replacing any reschedule already armed
        self.mass.call_later(
            DISCOVERY_INTERVAL, self.discover_players, task_id=self._discovery_task_id
        )

    async def _setup_player(self, soco: SoCo) -> None:
        """Set up a discovered Sonos player."""
        player_id = soco.uid

        if existing := cast("SonosPlayer", self.mass.players.get_player(player_id=player_id)):
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

    async def _discover_household_ids(self, prefer_s1: bool = True) -> list[str]:
        """Discover the HouseHold ID of S1 speaker(s) the network."""
        if cache := await self.mass.cache.get("sonos_household_ids"):
            return cast("list[str]", cache)
        household_ids: list[str] = []

        def get_all_sonos_ips() -> set[SoCo]:
            """Run full network discovery and return IP's of all devices found on the network."""
            discovered_zones: set[SoCo] | None
            if discovered_zones := scan_network(multi_household=True):
                return {zone.ip_address for zone in discovered_zones}
            return set()

        all_sonos_ips = await asyncio.to_thread(get_all_sonos_ips)
        for ip_address in all_sonos_ips:
            async with self.mass.http_session.get(
                f"http://{format_ip_for_url(ip_address)}:1400/status/zp"
            ) as resp:
                if resp.status == 200:
                    data = await resp.text()
                    if prefer_s1 and "<SWGen>2</SWGen>" in data:
                        continue
                    if "HouseholdControlID" in data:
                        household_id = data.split("<HouseholdControlID>")[1].split(
                            "</HouseholdControlID>"
                        )[0]
                        household_ids.append(household_id)
        await self.mass.cache.set("sonos_household_ids", household_ids, 3600)
        return household_ids
