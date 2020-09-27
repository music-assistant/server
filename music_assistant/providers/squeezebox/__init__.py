"""Squeezebox emulated player provider."""

import asyncio
import logging
from typing import List

from music_assistant.models.config_entry import ConfigEntry
from music_assistant.models.playerprovider import PlayerProvider

from .constants import PROV_ID, PROV_NAME
from .discovery import DiscoveryProtocol
from .socket_client import SqueezeSocketClient

CONF_LAST_POWER = "last_power"
CONF_LAST_VOLUME = "last_volume"

LOGGER = logging.getLogger(PROV_ID)

CONFIG_ENTRIES = []  # we don't have any provider config entries (for now)


async def async_setup(mass):
    """Perform async setup of this Plugin/Provider."""
    prov = PySqueezeProvider()
    await mass.async_register_provider(prov)


class PySqueezeProvider(PlayerProvider):
    """Python implementation of SlimProto server."""

    _tasks = []

    @property
    def id(self) -> str:
        """Return provider ID for this provider."""
        return PROV_ID

    @property
    def name(self) -> str:
        """Return provider Name for this provider."""
        return PROV_NAME

    @property
    def config_entries(self) -> List[ConfigEntry]:
        """Return Config Entries for this provider."""
        return CONFIG_ENTRIES

    async def async_on_start(self) -> bool:
        """Handle initialization of the provider. Called on startup."""
        # start slimproto server
        self._tasks.append(
            self.mass.add_job(
                asyncio.start_server(self.__async_client_connected, "0.0.0.0", 3483)
            )
        )
        # setup discovery
        self._tasks.append(self.mass.add_job(self.async_start_discovery()))

    async def async_on_stop(self):
        """Handle correct close/cleanup of the provider on exit."""
        for task in self._tasks:
            task.cancel()

    async def async_start_discovery(self):
        """Start discovery for players."""
        transport, _ = await self.mass.loop.create_datagram_endpoint(
            lambda: DiscoveryProtocol(self.mass.web.http_port),
            local_addr=("0.0.0.0", 3483),
        )
        try:
            while True:
                await asyncio.sleep(60)  # serve forever
        finally:
            transport.close()

    async def __async_client_connected(self, reader, writer):
        """Handle a client connection on the socket."""
        addr = writer.get_extra_info("peername")
        LOGGER.debug("New socket client connected: %s", addr)
        socket_client = SqueezeSocketClient(reader, writer)
        socket_client.mass = self.mass
