"""WiiM Player Provider implementation."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, cast

from aiohttp import ClientSession, TCPConnector
from wiim import WiimController
from wiim.discovery import async_create_wiim_device, verify_wiim_device
from wiim.exceptions import WiimDeviceException, WiimRequestException
from zeroconf import ServiceStateChange

from music_assistant.constants import CONF_ENTRY_MANUAL_DISCOVERY_IPS, VERBOSE_LOG_LEVEL
from music_assistant.helpers.util import (
    get_port_from_zeroconf,
    get_primary_ip_address_from_zeroconf,
)
from music_assistant.models.player_provider import PlayerProvider

from .player import WiimPlayer

if TYPE_CHECKING:
    from zeroconf.asyncio import AsyncServiceInfo


class WiimProvider(PlayerProvider):
    """
    WiiM player provider.

    This provides a WiiM player implementation for Music Assistant.
    """

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self.logger.info("Initializing WiimProvider with config: %s", self.config)

        if self.logger.isEnabledFor(VERBOSE_LOG_LEVEL):
            logging.getLogger("wiim").setLevel(logging.DEBUG)
            logging.getLogger("async_upnp_client").setLevel(logging.DEBUG)
        else:
            logging.getLogger("wiim").setLevel(self.logger.level + 10)
            logging.getLogger("async_upnp_client").setLevel(self.logger.level + 10)

        self.wiim_session = ClientSession(connector=TCPConnector(ssl=False))
        self.wiim_controller = WiimController(self.wiim_session)

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""
        self.logger.info("WiimProvider loaded")

        manual_ip_config: list[str] = cast(
            "list[str]", self.config.get_value(CONF_ENTRY_MANUAL_DISCOVERY_IPS.key)
        )

        for ip_address in manual_ip_config:
            stripped_ip_address = ip_address.strip()
            potential_locations = (
                f"http://{stripped_ip_address}:49152/description.xml",
                f"http://{stripped_ip_address}/description.xml",
            )

            matched_location = None
            upnp_device = None
            for location in potential_locations:
                upnp_device = await verify_wiim_device(location, self.wiim_session)
                if upnp_device:
                    matched_location = location
                    break

            if not upnp_device or not matched_location:
                continue

            player_id = upnp_device.udn

            if not player_id:
                continue

            await self.try_add_player(player_id, stripped_ip_address, "Unknown", matched_location)

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/close of the provider."""
        for player in self.players:
            self.logger.debug("Unloading player %s", player.name)
            if isinstance(player, WiimPlayer):
                await self.wiim_controller.remove_device(player.device.udn)
                await player.device.disconnect()
            await self.mass.players.unregister(player.player_id)
        await self.wiim_session.close()

    async def on_mdns_service_state_change(
        self, name: str, state_change: ServiceStateChange, info: AsyncServiceInfo | None
    ) -> None:
        """Handle MDNS service state callback."""
        if not info:
            return  # guard

        cur_address = get_primary_ip_address_from_zeroconf(info)

        if cur_address is None:
            return

        potential_locations = (
            f"http://{cur_address}:{get_port_from_zeroconf(info)}/description.xml",
            f"http://{cur_address}/description.xml",
            f"http://{cur_address}:49152/description.xml",
        )

        matched_location = None
        upnp_device = None
        for location in potential_locations:
            upnp_device = await verify_wiim_device(location, self.wiim_session)
            if upnp_device:
                matched_location = location
                break

        if not upnp_device or not matched_location:
            return

        player_id = upnp_device.udn

        if not player_id:
            return

        # handle removed player
        if state_change == ServiceStateChange.Removed:
            if mass_player := self.mass.players.get_player(player_id):
                self.logger.debug("Player offline: %s", mass_player.display_name)
                await self.mass.players.unregister(player_id)
            return

        # handle update for existing device
        if mass_player := self.mass.players.get_player(player_id):
            if cur_address and cur_address != mass_player.device_info.ip_address:
                self.logger.debug(
                    "Address updated to %s for player %s", cur_address, mass_player.display_name
                )
            mass_player.update_state()
            return

        # handle new player
        self.logger.debug("Discovered device %s on %s", name, cur_address)
        await self.try_add_player(player_id, cur_address, name, matched_location)

    async def try_add_player(
        self, player_id: str, ip_address: str, name: str, upnp_location: str
    ) -> None:
        """Try to add a WiiM device as a player."""
        try:
            wiim_dev = await async_create_wiim_device(
                upnp_location,
                self.wiim_session,
                host=ip_address,
                local_host=self.mass.webserver.publish_ip,
                polling_interval=60,
            )
        except (WiimRequestException, WiimDeviceException) as err:
            self.logger.warning("Failed to initialize WiiM device at %s: %s", ip_address, err)
            return

        await self.wiim_controller.add_device(wiim_dev)

        player = WiimPlayer(provider=self, player_id=wiim_dev.udn, device=wiim_dev)
        await player.setup()
        await self.mass.players.register(player)
