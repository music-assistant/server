"""Media Assistant Provider implementation."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, ClassVar, cast

from music_assistant_models.config_entries import ConfigEntry
from music_assistant_models.enums import ConfigEntryType, IdentifierType
from music_assistant_models.player import DeviceInfo
from rokuecp import Roku

from music_assistant.constants import CONF_ENTRY_MANUAL_DISCOVERY_IPS, VERBOSE_LOG_LEVEL
from music_assistant.helpers.util import TaskManager
from music_assistant.models.player_provider import PlayerProvider

from .constants import CONF_AUTO_DISCOVER, CONF_ROKU_APP_ID
from .player import MediaAssistantPlayer

if TYPE_CHECKING:
    from async_upnp_client.utils import CaseInsensitiveDict
    from music_assistant_models.enums import ProviderFeature

SUPPORTED_FEATURES: set[ProviderFeature] = set()


class MediaAssistantprovider(PlayerProvider):
    """Media Assistant Player provider."""

    roku_players: ClassVar[dict[str, MediaAssistantPlayer]] = {}
    lock: asyncio.Lock

    @property
    def supported_features(self) -> set[ProviderFeature]:
        """Return the features supported by this Provider."""
        return SUPPORTED_FEATURES

    async def get_config_entries(self) -> tuple[ConfigEntry, ...]:
        """Return Config entries to setup this provider."""
        return (
            CONF_ENTRY_MANUAL_DISCOVERY_IPS,
            ConfigEntry(
                key=CONF_ROKU_APP_ID,
                type=ConfigEntryType.STRING,
                default_value="782875",
                required=False,
                advanced=True,
            ),
            ConfigEntry(
                key=CONF_AUTO_DISCOVER,
                type=ConfigEntryType.BOOLEAN,
                default_value=True,
                advanced=True,
            ),
        )

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self.lock = asyncio.Lock()
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

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/close of the provider."""
        if self.roku_players is None:
            return  # type: ignore[unreachable]
        async with TaskManager(self.mass) as tg:
            for roku_player in self.roku_players.values():
                tg.create_task(self._device_disconnect(roku_player))

    async def on_upnp_service_discovered(
        self, search_target: str, discovery_info: CaseInsensitiveDict
    ) -> None:
        """Handle SSDP discovery callbacks."""
        del search_target
        if not self.config.get_value(CONF_AUTO_DISCOVER):
            return
        ssdp_st: str | None = discovery_info.get("st")
        if not ssdp_st or "roku:ecp" not in ssdp_st:
            return
        if not discovery_info.get("usn"):
            return
        device_ip: str | None = discovery_info.get("_host")
        if not device_ip:
            return
        await self._device_discovered(device_ip)

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
                roku_player.device_info.add_identifier(IdentifierType.IP_ADDRESS, ip)
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
                )
                roku_player._attr_device_info.add_identifier(IdentifierType.IP_ADDRESS, ip)
                roku_player._attr_device_info.add_identifier(
                    IdentifierType.SERIAL_NUMBER, device.info.serial_number
                )
                if device.info.ethernet_mac:
                    roku_player._attr_device_info.add_identifier(
                        IdentifierType.MAC_ADDRESS, device.info.ethernet_mac
                    )
                elif device.info.wifi_mac:
                    roku_player._attr_device_info.add_identifier(
                        IdentifierType.MAC_ADDRESS, device.info.wifi_mac
                    )

                self.roku_players[player_id] = roku_player
            await roku_player.setup()
