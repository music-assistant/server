"""WiiM Player Provider implementation."""

from __future__ import annotations

from typing import cast

from pywiim import WiiMClient
from pywiim.upnp.client import UpnpClient

from music_assistant.constants import CONF_ENTRY_MANUAL_DISCOVERY_IPS
from music_assistant.models.player_provider import PlayerProvider
from music_assistant.providers.wiim.player import WiimPlayer


class WiimProvider(PlayerProvider):
    """
    WiiM player provider.

    This provides a WiiM player implementation for Music Assistant.
    """

    async def handle_async_init(self) -> None:
        """Handle async initialization of the provider."""
        self.logger.info("Initializing WiimProvider with config: %s", self.config)

    async def loaded_in_mass(self) -> None:
        """Call after the provider has been loaded."""
        self.logger.info("WiimProvider loaded")

        # asdf = await discover_devices()

        # for d in asdf:
        #     self.logger.info("Found one %s", d)

        manual_ip_config: list[str] = cast(
            "list[str]", self.config.get_value(CONF_ENTRY_MANUAL_DISCOVERY_IPS.key)
        )

        for ip_address in manual_ip_config:
            stripped_ip_address = ip_address.strip()

            client = WiiMClient(stripped_ip_address, session=self.mass.http_session)

            # Get device info for UUID
            device_info = await client.get_device_info_model()

            if device_info.uuid is None or device_info.name is None:
                continue

            # Create UPnP client (required for events and queue management)
            description_url = f"http://{stripped_ip_address}:49152/description.xml"
            upnp_client = await UpnpClient.create(stripped_ip_address, description_url)

            player = WiimPlayer(
                provider=self,
                player_id=device_info.uuid,
                name=device_info.name,
                client=client,
                upnp_client=upnp_client,
            )

            await player.setup()

            await self.mass.players.register(player)

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
