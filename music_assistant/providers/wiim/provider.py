"""WiiM Player Provider implementation."""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiohttp import ClientSession, TCPConnector
from wiim import WiimApiEndpoint, WiimController, WiimDevice
from wiim.discovery import verify_wiim_device
from zeroconf import ServiceStateChange

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
        # OPTIONAL
        # this is an optional method that you can implement if
        # relevant or leave out completely if not needed.
        # it will be called when the provider is initialized in Music Assistant.
        # you can use this to do any async initialization of the provider,
        # such as loading configuration, setting up connections, etc.
        self.logger.info("Initializing WiimProvider with config: %s", self.config)

        self._attr_session = ClientSession(connector=TCPConnector(ssl=False))
        self.wiim_controller = WiimController(self._attr_session)

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""
        # OPTIONAL
        # this is an optional method that you can implement if
        # relevant or leave out completely if not needed.
        # it will be called after the provider has been fully loaded into Music Assistant.
        # you can use this for instance to trigger custom (non-mdns) discovery of players
        # or any other logic that needs to run after the provider is fully loaded.
        self.logger.info("WiimProvider loaded")

    async def unload(self, is_removed: bool = False) -> None:
        """
        Handle unload/close of the provider.

        Called when provider is deregistered (e.g. MA exiting or config reloading).
        is_removed will be set to True when the provider is removed from the configuration.
        """
        # OPTIONAL
        # this is an optional method that you can implement if
        # relevant or leave out completely if not needed.
        # it will be called when the provider is unloaded from Music Assistant.
        # this means also when the provider is getting reloaded
        for player in self.players:
            # if you have any cleanup logic for the players, you can do that here.
            # e.g. disconnecting from the player, closing connections, etc.
            self.logger.debug("Unloading player %s", player.name)
            await self.mass.players.unregister(player.player_id)

    def on_player_enabled(self, player_id: str) -> None:
        """Call (by config manager) when a player gets enabled."""
        # OPTIONAL
        # this is an optional method that you can implement if
        # you want to do something special when a player is enabled.

    def on_player_disabled(self, player_id: str) -> None:
        """Call (by config manager) when a player gets disabled."""
        # OPTIONAL
        # this is an optional method that you can implement if
        # you want to do something special when a player is disabled.
        # e.g. you can stop polling the player or disconnect from it.

    async def remove_player(self, player_id: str) -> None:
        """Remove a player from this provider."""
        # OPTIONAL - required only if you specified ProviderFeature.REMOVE_PLAYER
        # this is used to actually remove a player.

    async def on_mdns_service_state_change(
        self, name: str, state_change: ServiceStateChange, info: AsyncServiceInfo | None
    ) -> None:
        """Handle MDNS service state callback."""
        # MANDATORY IF YOU WANT TO USE MDNS DISCOVERY
        # OPTIONAL if you dont use mdns for discovery of players
        # If you specify a mdns service type in the manifest.json, this method will be called
        # automatically on mdns changes for the specified service type.

        # If no mdns service type is specified, this method is omitted and you
        # can completely remove it from your provider implementation.

        if not info:
            return  # guard

        # NOTE: If you do not use mdns for discovery of players on the network,
        # you must implement your own discovery mechanism and logic to add new players
        # and update them on state changes when needed.
        # Below is a bit of example implementation but we advise to look at existing
        # player providers for more inspiration.
        name = name.split("@", 1)[1] if "@" in name else name
        player_id = info.decoded_properties["uuid"]  # this is just an example!
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

        cur_address = get_primary_ip_address_from_zeroconf(info)

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

        potential_locations = [
            f"http://{cur_address}:{get_port_from_zeroconf(info)}/description.xml",
            f"http://{cur_address}/description.xml",
            f"http://{cur_address}:49152/description.xml",
        ]

        session = self._attr_session

        if cur_address is None:
            return

        upnp_device = None
        for location in potential_locations:
            # Use the verify_wiim_device function from discovery.py to check
            # if this is a WiiM device
            upnp_device = await verify_wiim_device(location, session)
            if upnp_device:
                break

        if not upnp_device:
            return

        http_api = WiimApiEndpoint(
            protocol="https", port=443, endpoint=cur_address, session=session
        )

        wiim_dev = WiimDevice(
            upnp_device,
            session=session,
            http_api_endpoint=http_api,
            ha_host_ip="192.168.1.124",
            polling_interval=60,
        )

        await self.wiim_controller.add_device(wiim_dev)

        player = WiimPlayer(provider=self, player_id=player_id, device=wiim_dev)

        init_success = await wiim_dev.async_init_services_and_subscribe()

        if not init_success:
            self.logger.warning("Failed to initialize WiiM device %s", name)

        await self.mass.players.register(player)
