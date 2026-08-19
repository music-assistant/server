"""OpenHome/Linn Player Provider implementation."""
from __future__ import annotations

import asyncio
import logging
from ipaddress import IPv4Address
from typing import TYPE_CHECKING

from async_upnp_client.aiohttp import AiohttpSessionRequester
from async_upnp_client.client import UpnpRequester
from async_upnp_client.client_factory import UpnpFactory
from async_upnp_client.search import async_search
from async_upnp_client.utils import CaseInsensitiveDict
from music_assistant_models.player import DeviceInfo
from zeroconf import ServiceStateChange

from music_assistant.constants import CONF_PLAYERS, VERBOSE_LOG_LEVEL
from music_assistant.helpers.util import (
    TaskManager,
    get_primary_ip_address_from_zeroconf,
)
from music_assistant.models.player_provider import PlayerProvider

from .constants import CONF_NETWORK_SCAN, CALLBACK_URL
from .helpers import OpenHomeNotifyServer
from .player import OpenHomePlayer

if TYPE_CHECKING:
    from zeroconf.asyncio import AsyncServiceInfo


class OpenHomePlayerProvider(PlayerProvider):
    """Linn/OpenHome Media Player provider."""

    openhome_players: dict[str, OpenHomePlayer] = {}
    _discovery_running: bool = False

    lock: asyncio.Lock
    requester: UpnpRequester
    upnp_factory: UpnpFactory
    notify_server: OpenHomeNotifyServer

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""

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
        self.notify_server = OpenHomeNotifyServer(self.requester, self.mass)

    async def unload(self, is_removed: bool = False) -> None:
        """Handle unload/close of the provider."""

        self.mass.streams.unregister_dynamic_route(path = CALLBACK_URL, method = "NOTIFY")
        async with TaskManager(self.mass) as tg:
            for openhome_player in self.openhome_players.values():
                tg.create_task(self._device_disconnect(openhome_player))

        for player in self.players:
            self.logger.debug("Unloading player %s", player.name)
            await self.mass.players.unregister(player.player_id)


    async def on_mdns_service_state_change(
            self, name: str, state_change: ServiceStateChange, info: AsyncServiceInfo | None
    ) -> None:
        """Handle MDNS service state callback."""

        if not info:
            return  # guard

        name = name.split("@", 1)[1] if "@" in name else name
        player_id = info.decoded_properties["uuid"]  # this is just an example!
        if not player_id:
            return  # guard, we need a player_id to work with


        if state_change == ServiceStateChange.Removed:
            # check if the player manager has an existing entry for this player
            if mass_player := self.mass.players.get(player_id):
                # the player has become unavailable
                self.logger.debug("Player offline: %s", mass_player.display_name)
                await self.mass.players.unregister(player_id)
            return

        cur_address = get_primary_ip_address_from_zeroconf(info)
        if mass_player := self.mass.players.get(player_id):
            if cur_address and cur_address != mass_player.device_info.ip_address:
                self.logger.debug(
                    "Address updated to %s for player %s",
                    cur_address,
                    mass_player.display_name,
                )
            mass_player.update_state()
            return
        self.logger.debug("Discovered device %s on %s", name, cur_address)

    async def discover_players(self, use_multicast: bool = False) -> None:
        """Discover OpenHome players on the network."""

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
                if not ssdp_st:
                    return

                ssdp_usn: str = discovery_info["USN"]
                if not (("urn:linn-co-uk:device:Source:1" in ssdp_usn) or
                        ("urn:av-openhome-org:device:Source:1" in ssdp_usn)):
                    return

                ssdp_udn: str | None = discovery_info.get("_udn")
                assert ssdp_udn is not None
                if not ssdp_udn and ssdp_usn.startswith("uuid:"):
                    ssdp_udn = ssdp_usn.split("::", maxsplit=1)[0]
                ssdp_udn = ssdp_udn.replace("uuid:", "")
                if ssdp_udn in discovered_devices:
                    # already processed this device
                    return

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

        def reschedule() -> None:
            self.mass.create_task(
                self.discover_players(use_multicast=not use_multicast)
            )

        self.mass.loop.call_later(300, reschedule)

    async def _device_discovered(self, udn: str, description_url: str) -> None:
        """Handle discovered Open Home player."""
        self.logger.debug(f"DISCOVERED: {udn} {description_url}")
        async with self.lock:
            if openhome_player := self.openhome_players.get(udn):
                if (
                        openhome_player.description_url == description_url
                        and openhome_player.available
                ):
                    return
                openhome_player.description_url = description_url
            else:
                conf_key = f"{CONF_PLAYERS}/{udn}/enabled"
                enabled = self.mass.config.get(conf_key, True)
                self.logger.debug(f"New player: {udn} {conf_key} {enabled}")
                if not enabled:
                    self.logger.debug(f"Ignoring disabled player: {udn}")
                    return

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
