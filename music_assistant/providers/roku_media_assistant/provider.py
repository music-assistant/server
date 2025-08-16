"""Media Assistant Provider implementation."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, cast

from async_upnp_client.search import async_search
from music_assistant_models.player import DeviceInfo
from rokuecp import Roku

from music_assistant.constants import CONF_ENTRY_MANUAL_DISCOVERY_IPS, VERBOSE_LOG_LEVEL
from music_assistant.helpers.util import TaskManager
from music_assistant.models.player_provider import PlayerProvider

from .constants import CONF_AUTO_DISCOVER
from .player import MediaAssistantPlayer

if TYPE_CHECKING:
    from async_upnp_client.utils import CaseInsensitiveDict
    from music_assistant_models.enums import ProviderFeature

SUPPORTED_FEATURES: set[ProviderFeature] = set()


class MediaAssistantprovider(PlayerProvider):
    """Media Assistant Player provider."""

    roku_players: dict[str, MediaAssistantPlayer] = {}
    _discovery_running: bool = False
    lock: asyncio.Lock

    @property
    def supported_features(self) -> set[ProviderFeature]:
        """Return the features supported by this Provider."""
        return SUPPORTED_FEATURES

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self.lock = asyncio.Lock()
        # silence the async_upnp_client logger
        if self.logger.isEnabledFor(VERBOSE_LOG_LEVEL):
            logging.getLogger("async_upnp_client").setLevel(logging.DEBUG)
        else:
            logging.getLogger("async_upnp_client").setLevel(self.logger.level + 10)
        # silence the rokuecp logger
        if self.logger.isEnabledFor(VERBOSE_LOG_LEVEL):
            logging.getLogger("rokuecp").setLevel(logging.DEBUG)
        else:
            logging.getLogger("rokuecp").setLevel(self.logger.level + 10)

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""
        manual_ip_config = cast(
            "list[str]", self.config.get_value(CONF_ENTRY_MANUAL_DISCOVERY_IPS.key)
        )

        for ip in manual_ip_config:
            await self._device_discovered(ip)

        self.logger.info("MediaAssistantProvider loaded")
        await self.discover_players()

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/close of the provider."""
        if self.roku_players is None:
            return  # type: ignore[unreachable]
        async with TaskManager(self.mass) as tg:
            for roku_player in self.roku_players.values():
                tg.create_task(self._device_disconnect(roku_player))

    async def discover_players(self) -> None:
        """Discover Roku players on the network."""
        if self.config.get_value(CONF_AUTO_DISCOVER):
            if self._discovery_running:
                return
            try:
                self._discovery_running = True
                self.logger.debug("Roku discovery started...")
                discovered_devices: set[str] = set()

                async def on_response(discovery_info: CaseInsensitiveDict) -> None:
                    """Process discovered device from ssdp search."""
                    ssdp_st: str | None = discovery_info.get("st")
                    if not ssdp_st:
                        return

                    if "roku:ecp" not in ssdp_st:
                        # we're only interested in Roku devices
                        return

                    ssdp_usn: str = discovery_info["usn"]
                    ssdp_udn: str | None = discovery_info.get("_udn")
                    if not ssdp_udn and ssdp_usn.startswith("uuid:"):
                        ssdp_udn = "ROKU_" + ssdp_usn.split(":")[-1]
                    elif ssdp_udn:
                        ssdp_udn = "ROKU_" + ssdp_udn.split(":")[-1]
                    else:
                        return

                    if ssdp_udn in discovered_devices:
                        # already processed this device
                        return

                    discovered_devices.add(ssdp_udn)

                    await self._device_discovered(discovery_info["_host"])

                await async_search(on_response, search_target="roku:ecp")

            finally:
                self._discovery_running = False

        def reschedule() -> None:
            self.mass.create_task(self.discover_players())

        # reschedule self once finished
        self.mass.loop.call_later(300, reschedule)

    async def _device_disconnect(self, roku_player: MediaAssistantPlayer) -> None:
        """Destroy connections to the device."""
        async with roku_player.lock:
            if not roku_player.roku:
                self.logger.debug("Disconnecting from device that's not connected")
                return

            self.logger.debug("Disconnecting from %s", roku_player.name)

            old_device = roku_player.roku
            self.roku_players.pop(roku_player.player_id)
            await old_device.close_session()

    async def _device_discovered(self, ip: str) -> None:
        """Handle discovered Roku."""
        async with self.lock:
            # connecting to Roku to retrieve device Info
            roku = Roku(ip)
            try:
                device = await roku.update()
                await roku.close_session()
            except Exception:
                self.logger.error("Failed to retrieve device info from Roku at: %s", ip)
                await roku.close_session()
                return

            if device.info.serial_number is None:
                return

            player_id = "ROKU_" + device.info.serial_number

            if roku_player := self.roku_players.get(player_id):
                # existing player
                if roku_player.device_info.ip_address == ip and roku_player.available:
                    # nothing to do, device is already connected
                    return
                # update description url to newly discovered one
                roku_player.device_info.ip_address = ip
            else:
                roku_player = MediaAssistantPlayer(
                    provider=self,
                    player_id=player_id,
                    roku_name=device.info.name if device.info.name is not None else "",
                    roku=Roku(ip),
                )

                roku_player._attr_device_info = DeviceInfo(
                    model=device.info.model_name if device.info.model_name is not None else "",
                    model_id=device.info.model_number,
                    manufacturer=device.info.brand,
                    ip_address=ip,
                )

                self.roku_players[player_id] = roku_player
            await roku_player.setup()
