"""ChromeCast playerprovider."""

import logging
from typing import List

import pychromecast
from music_assistant.models.config_entry import ConfigEntry
from music_assistant.models.provider import PlayerProvider
from pychromecast.controllers.multizone import MultizoneManager

from .const import PROV_ID, PROV_NAME, PROVIDER_CONFIG_ENTRIES
from .models import ChromecastInfo
from .player import ChromecastPlayer

LOGGER = logging.getLogger(PROV_ID)


async def setup(mass):
    """Perform async setup of this Plugin/Provider."""
    logging.getLogger("pychromecast").setLevel(logging.WARNING)
    prov = ChromecastProvider()
    await mass.register_provider(prov)


class ChromecastProvider(PlayerProvider):
    """Support for ChromeCast Audio PlayerProvider."""

    def __init__(self, *args, **kwargs):
        """Initialize."""
        self.mz_mgr = MultizoneManager()
        self._listener = None
        self._browser = None
        super().__init__(*args, **kwargs)

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
        return PROVIDER_CONFIG_ENTRIES

    async def on_start(self) -> bool:
        """Handle initialization of the provider based on config."""
        self._listener = pychromecast.CastListener(
            self.__chromecast_add_update_callback,
            self.__chromecast_remove_callback,
            self.__chromecast_add_update_callback,
        )

        def start_discovery():
            self._browser = pychromecast.discovery.start_discovery(
                self._listener, self.mass.zeroconf
            )

        self.mass.add_job(start_discovery)
        return True

    async def on_stop(self):
        """Handle correct close/cleanup of the provider on exit."""
        if not self._browser:
            return
        # stop discovery
        self.mass.add_job(pychromecast.stop_discovery, self._browser)

    def __chromecast_add_update_callback(self, cast_uuid, cast_service_name):
        """Handle zeroconf discovery of a new or updated chromecast."""
        # pylint: disable=unused-argument
        if cast_uuid is None:
            return  # Discovered chromecast without uuid?

        service = self._listener.services[cast_uuid]
        cast_info = ChromecastInfo(
            services=service[0],
            uuid=service[1],
            model_name=service[2],
            friendly_name=service[3],
            host=service[4],
            port=service[5],
        )
        player_id = cast_info.uuid
        LOGGER.debug(
            "Chromecast discovered: %s (%s)", cast_info.friendly_name, player_id
        )
        player = self.mass.players.get_player(player_id)
        if not player:
            player = ChromecastPlayer(self.mass, cast_info)
        # if player was already added, the player will take care of reconnects itself.
        player.set_cast_info(cast_info)
        self.mass.add_job(self.mass.players.add_player(player))

    @staticmethod
    def __chromecast_remove_callback(cast_uuid, cast_service_name, cast_service):
        """Handle a Chromecast removed event."""
        # pylint: disable=unused-argument
        player_id = str(cast_service[1])
        friendly_name = cast_service[3]
        LOGGER.debug("Chromecast removed: %s - %s", friendly_name, player_id)
        # we ignore this event completely as the Chromecast socket client handles this itself
