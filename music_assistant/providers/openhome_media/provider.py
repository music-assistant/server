"""OpenHome/Linn Player Provider implementation."""
from __future__ import annotations

import asyncio
import logging
from ipaddress import IPv4Address
from typing import TYPE_CHECKING

from async_upnp_client.aiohttp import AiohttpSessionRequester
from async_upnp_client.client import UpnpRequester
from async_upnp_client.client_factory import UpnpFactory

# from async_upnp_client.const import AddressTupleVXType
from async_upnp_client.search import async_search

# from async_upnp_client.ssdp import SSDP_IP_V4, SSDP_IP_V6, SSDP_PORT, SSDP_ST_ALL
from async_upnp_client.utils import CaseInsensitiveDict

# from music_assistant_models.enums import (
#     ConfigEntryType,
#     EventType,
#     PlaybackState,
#     PlayerFeature,
#     PlayerType,
#     RepeatMode,
# )
from music_assistant_models.player import DeviceInfo
from zeroconf import ServiceStateChange

from music_assistant.constants import CONF_PLAYERS, VERBOSE_LOG_LEVEL
from music_assistant.helpers.util import (
    TaskManager,
    get_primary_ip_address_from_zeroconf,
)
from music_assistant.models.player_provider import PlayerProvider

from .constants import CONF_NETWORK_SCAN
from .helpers import OpenHomeNotifyServer
from .player import OpenHomePlayer

if TYPE_CHECKING:
    from zeroconf.asyncio import AsyncServiceInfo


class OpenHomePlayerProvider(PlayerProvider):
    """
    Linn/OpenHome Media Player provider.

    Note that this is always subclassed from PlayerProvider,
    which in turn is a subclass of the generic Provider model.

    The base implementation already takes care of some convenience methods,
    such as the mass object and the logger. Take a look at the base class
    for more information on what is available.

    Just like with any other subclass, make sure that if you override
    any of the default methods (such as __init__), you call the super() method.
    In most cases it's not needed to override any of the builtin methods, and you only
    implement the abc methods with your actual implementation.
    """

    openhome_players: dict[str, OpenHomePlayer] = {}
    _discovery_running: bool = False

    lock: asyncio.Lock
    requester: UpnpRequester
    upnp_factory: UpnpFactory
    notify_server: OpenHomeNotifyServer

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        # OPTIONAL
        # this is an optional method that you can implement if
        # relevant or leave out completely if not needed.
        # it will be called when the provider is initialized in Music Assistant.
        # you can use this to do any async initialization of the provider,
        # such as loading configuration, setting up connections, etc.
        self.logger.info(
            "Initializing OpenHomePlayerProvider with config: %s", self.config
        )
        self.lock = asyncio.Lock()
        if self.logger.isEnabledFor(VERBOSE_LOG_LEVEL):
            logging.getLogger("async_upnp_client").setLevel(logging.INFO)
        else:
            logging.getLogger("async_upnp_client").setLevel(self.logger.level + 10)
        self.logger.info(
            "Initializing OpenHomePlayerProvider with config: %s", self.config
        )
        self.requester = AiohttpSessionRequester(
            self.mass.http_session, with_sleep=True
        )
        self.upnp_factory = UpnpFactory(self.requester, non_strict=True)

        # use Music Assistant dynamic routes to receive subscribed messages
        # self.notify_server = OpenHomeNotifyServer(self.requester, self.mass)

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
        self.mass.streams.unregister_dynamic_route("/notify", "NOTIFY")
        async with TaskManager(self.mass) as tg:
            for openhome_player in self.openhome_players.values():
                tg.create_task(self._device_disconnect(openhome_player))

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
        super().on_player_enabled(player_id)

    def on_player_disabled(self, player_id: str) -> None:
        """Call (by config manager) when a player gets disabled."""
        # OPTIONAL
        # this is an optional method that you can implement if
        # you want to do something special when a player is disabled.
        # e.g. you can stop polling the player or disconnect from it.
        super().on_player_disabled(player_id)

    async def remove_player(self, player_id: str) -> None:
        """Remove a player from this provider."""
        # OPTIONAL - required only if you specified ProviderFeature.REMOVE_PLAYER
        # this is used to actually remove a player.

    async def on_mdns_service_state_change(
            self, name: str, state_change: ServiceStateChange, info: AsyncServiceInfo | None
    ) -> None:
        """Handle MDNS service state callback."""
        # MANDATORY IF YOU WANT TO USE MDNS DISCOVERY
        # OPTIONAL if you don't use mdns for discovery of players
        # If you specify a mdns service type in the manifest.json, this method will be called
        # automatically on mdns changes for the specified service type.

        # If no mdns service type is specified, this method is omitted so
        # you can completely remove it from your provider implementation.

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
        # handle update for existing device
        # (state change is either updated or added)
        # check if we have an existing player in the player manager
        # note that you can use this point to update the player connection info
        # if that changed (e.g. ip address)
        cur_address = get_primary_ip_address_from_zeroconf(info)
        if mass_player := self.mass.players.get(player_id):
            # existing player found in the player manager,
            # this is an existing player that has been updated/reconnected
            # or simply a re-announcement on mdns.
            if cur_address and cur_address != mass_player.device_info.ip_address:
                self.logger.debug(
                    "Address updated to %s for player %s",
                    cur_address,
                    mass_player.display_name,
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

    async def discover_players(self, use_multicast: bool = False) -> None:
        """Discover OpenHome players on the network."""
        # This is an optional method that you can implement if
        # you want to (manually) discover players on the network
        # and you do not use mdns discovery.
        if self._discovery_running:
            return
        discovered_devices: set[str] = set()
        try:
            self._discovery_running = True
            self.logger.debug("OpenHome discovery started...")
            allow_network_scan = self.config.get_value(CONF_NETWORK_SCAN)

            async def on_response(discovery_info: CaseInsensitiveDict) -> None:
                """Process discovered device from ssdp search."""
                ssdp_st: str = discovery_info.get("ST", discovery_info.get("NT"))
                # ST=ServiceType NT=NotificationType
                if not ssdp_st:
                    return

                ssdp_usn: str = discovery_info["USN"]
                # USN=UniqueServiceName
                # print("TESTING USN", ssdp_usn)
                if not (("urn:linn-co-uk:device:Source:1" in ssdp_usn) or
                        ("urn:av-openhome-org:device:Source:1" in ssdp_usn)):
                    # we're only interested in OpenHome compliant devices
                    return
                # print("PASSED USN", ssdp_usn)

                ssdp_udn: str | None = discovery_info.get("_udn")
                assert ssdp_udn is not None  # for type checking
                if not ssdp_udn and ssdp_usn.startswith("uuid:"):
                    ssdp_udn = ssdp_usn.split("::", maxsplit=1)[0]
                ssdp_udn = ssdp_udn.replace("uuid:", "") # don't like prefix for no benefit
                if ssdp_udn in discovered_devices:
                    # already processed this device
                    return

                # print("ADDING OH DEVICE: ", ssdp_udn)
                discovered_devices.add(ssdp_udn)
                await self._device_discovered(ssdp_udn, discovery_info["location"])

            # alternate between using a regular and multicast search (if enabled)
            if allow_network_scan and use_multicast:
                await async_search(
                    async_callback=on_response,
                    target=(str(IPv4Address("239.255.255.250")), 1900),
                    timeout = 9
                )
            else:
                await async_search(async_callback=on_response)

        finally:
            self._discovery_running = False

        # TODO implement logic to listen for state changes from the player and update the player object

        def reschedule() -> None:
            self.mass.create_task(
                self.discover_players(use_multicast=not use_multicast)  # toggle multicast
            )

        # reschedule self once finished
        self.mass.loop.call_later(300, reschedule)

        # register the player with the player manager
        # await self.mass.players.register(player)
        # once the player is registered, you can either instruct the player manager to
        # poll the player for state changes or you can implement your own logic to
        # listen for state changes from the player and update the player object accordingly.
        # if the player state needs to be updated, you can call the update method on the player:
        # player.update_state()

    async def _device_discovered(self, udn: str, description_url: str) -> None:
        """Handle discovered Open Home player."""
        self.logger.debug(f"DISCOVERED: {udn} {description_url}")
        # print("DISCOVERING PLAYER: ", udn)
        async with self.lock:
            if openhome_player := self.openhome_players.get(udn):
                # existing player
                if (
                        openhome_player.description_url == description_url
                        and openhome_player.available
                ):
                    # nothing to do, device is already connected
                    return
                # update description_url to newly discovered one
                openhome_player.description_url = description_url
                # print("UPDATING URL", openhome_player.description_url , description_url)
            else:
                # new player detected, add new OpenHomePlayer instance
                conf_key = f"{CONF_PLAYERS}/{udn}/enabled"
                enabled = self.mass.config.get(conf_key, True)
                self.logger.debug(f"New player: {udn} {conf_key} {enabled}")
                # ignore disabled players
                if not enabled:
                    self.logger.debug(f"Ignoring disabled player: {udn}")
                    return

                # add new Linn/Open Home player
                openhome_player = OpenHomePlayer(
                    provider=self,
                    player_id=udn,
                    description_url=description_url,
                )

                # will be updated later when device connects.
                openhome_player._attr_device_info = DeviceInfo(
                    model="Unknown",
                    manufacturer="Unknown",
                )
                print("ADDING OPEN HOME PLAYER: ", udn)
                self.openhome_players[udn] = openhome_player
            await openhome_player.setup()

    async def _device_disconnect(self, openhome_player: OpenHomePlayer) -> None:
        """
        Destroy connections to the device now that it's not available.

        Also call when removing this entity from MA to clean up connections.
        """
        async with openhome_player.lock:
            if not openhome_player.device:
                self.logger.debug("Disconnecting from device that's not connected")
                return

            self.logger.debug("Disconnecting from %s", openhome_player.device.name)

            openhome_player.device.on_event = None
            old_device = openhome_player.device
            openhome_player.device = None
            await old_device.async_unsubscribe_services()
