"""WiiM Player Provider implementation."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from aiohttp import ClientSession, TCPConnector
from wiim import WiimApiEndpoint, WiimController, WiimDevice
from wiim.discovery import verify_wiim_device
from zeroconf import ServiceStateChange

from music_assistant.constants import CONF_ENTRY_MANUAL_DISCOVERY_IPS
from music_assistant.helpers.util import (
    get_port_from_zeroconf,
    get_primary_ip_address_from_zeroconf,
)
from music_assistant.models.player_provider import PlayerProvider

from .player import WiimPlayer

if TYPE_CHECKING:
    from async_upnp_client.client import UpnpDevice
    from zeroconf.asyncio import AsyncServiceInfo


class WiimProvider(PlayerProvider):
    """
    WiiM player provider.

    This provides a WiiM player implementation for Music Assistant.
    """

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self.logger.info("Initializing WiimProvider with config: %s", self.config)

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
            potential_locations = [
                f"http://{stripped_ip_address}:49152/description.xml",
                f"http://{stripped_ip_address}/description.xml",
            ]

            upnp_device = None
            for location in potential_locations:
                # Use the verify_wiim_device function from discovery.py to check
                # if this is a WiiM device
                upnp_device = await verify_wiim_device(location, self.wiim_session)
                if upnp_device:
                    break

            if not upnp_device:
                return

            player_id = upnp_device.udn

            if not player_id:
                return  # guard, we need a player_id to work with

            await self.try_add_player(player_id, stripped_ip_address, "Unknown", upnp_device)

    async def unload(self, is_removed: bool = False) -> None:
        """
        Handle unload/close of the provider.

        Called when provider is deregistered (e.g. MA exiting or config reloading).
        is_removed will be set to True when the provider is removed from the configuration.
        """
        for player in self.players:
            # if you have any cleanup logic for the players, you can do that here.
            # e.g. disconnecting from the player, closing connections, etc.
            self.logger.debug("Unloading player %s", player.name)
            await self.mass.players.unregister(player.player_id)

    async def on_mdns_service_state_change(
        self, name: str, state_change: ServiceStateChange, info: AsyncServiceInfo | None
    ) -> None:
        """Handle MDNS service state callback."""
        if not info:
            return  # guard

        cur_address = get_primary_ip_address_from_zeroconf(info)

        if cur_address is None:
            return

        potential_locations = [
            f"http://{cur_address}:{get_port_from_zeroconf(info)}/description.xml",
            f"http://{cur_address}/description.xml",
            f"http://{cur_address}:49152/description.xml",
        ]

        upnp_device = None
        for location in potential_locations:
            # Use the verify_wiim_device function from discovery.py to check
            # if this is a WiiM device
            upnp_device = await verify_wiim_device(location, self.wiim_session)
            if upnp_device:
                break

        if not upnp_device:
            return

        player_id = upnp_device.udn

        if not player_id:
            return  # guard, we need a player_id to work with

        # handle removed player
        if state_change == ServiceStateChange.Removed:
            # check if the player manager has an existing entry for this player
            if mass_player := self.mass.players.get(player_id):
                # the player has become unavailable
                self.logger.debug("Player offline: %s", mass_player.display_name)
                await self.mass.players.unregister(player_id)
            return

        # handle update for existing device
        # (state change is either updated or added)
        # check if we have an existing player in the player manager
        # note that you can use this point to update the player connection info
        # if that changed (e.g. ip address)
        if mass_player := self.mass.players.get(player_id):
            # existing player found in the player manager,
            # this is an existing player that has been updated/reconnected
            # or simply a re-announcement on mdns.

            if cur_address and cur_address != mass_player.device_info.ip_address:
                self.logger.debug(
                    "Address updated to %s for player %s", cur_address, mass_player.display_name
                )
            # inform the player manager of any changes to the player object
            # note that you would normally call this from some other callback from
            # the player's native api/library which informs you of changes in the player state.
            # as a last resort you can also choose to let the player manager
            # poll the player for state changes
            mass_player.update_state()
            return
        # handle new player
        self.logger.debug("Discovered device %s on %s", name, cur_address)
        # your own connection logic will probably be implemented here where
        # you connect to the player etc. using your device/provider specific library.

        await self.try_add_player(player_id, cur_address, name, upnp_device)

    async def try_add_player(
        self, player_id: str, ip_address: str, name: str, upnp_device: UpnpDevice
    ) -> None:
        """Try to add a device."""
        http_api = WiimApiEndpoint(
            protocol="https", port=443, endpoint=ip_address, session=self.wiim_session
        )

        wiim_dev = WiimDevice(
            upnp_device,
            session=self.wiim_session,
            http_api_endpoint=http_api,
            ha_host_ip=self.mass.webserver.publish_ip,
            polling_interval=60,
        )

        await self.wiim_controller.add_device(wiim_dev)

        player = WiimPlayer(provider=self, player_id=player_id, device=wiim_dev)

        init_success = await wiim_dev.async_init_services_and_subscribe()

        if not init_success:
            self.logger.warning("Failed to initialize WiiM device %s", name)

        await self.mass.players.register(player)
