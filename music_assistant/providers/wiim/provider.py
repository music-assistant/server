"""WiiM Player Provider implementation."""

from __future__ import annotations

import asyncio
import logging
from typing import cast

from pywiim import discover_devices

from music_assistant.constants import CONF_ENTRY_MANUAL_DISCOVERY_IPS, VERBOSE_LOG_LEVEL
from music_assistant.models.player_provider import PlayerProvider
from music_assistant.providers.wiim.player import WiimPlayer


class WiimProvider(PlayerProvider):
    """
    WiiM player provider.

    This provides a WiiM player implementation for Music Assistant.
    """

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self.logger.debug("Initializing WiimProvider with config: %s", self.config)

        if self.logger.isEnabledFor(VERBOSE_LOG_LEVEL):
            logging.getLogger("pywiim").setLevel(logging.DEBUG)
            logging.getLogger("async_upnp_client").setLevel(logging.DEBUG)
        else:
            logging.getLogger("pywiim").setLevel(self.logger.level + 10)
            logging.getLogger("async_upnp_client").setLevel(self.logger.level + 10)

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""
        self.logger.info("WiimProvider loaded")

        discovered_devices = await discover_devices()

        device_ip_addresses: list[str] = [
            ip_address.strip()
            for ip_address in cast(
                "list[str]", self.config.get_value(CONF_ENTRY_MANUAL_DISCOVERY_IPS.key)
            )
            if len(ip_address.strip()) > 0
        ]

        # Remove duplicates (by IP)
        for device in discovered_devices:
            if device.ip not in device_ip_addresses:
                device_ip_addresses.append(device.ip)

        # Run the rest of each player setup in parallel, so we don't have to wait for each player
        # to be setup before starting the next one.
        setup_coroutines = [
            WiimPlayer.setup(ip_address, self) for ip_address in device_ip_addresses
        ]
        await asyncio.gather(*setup_coroutines)

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
