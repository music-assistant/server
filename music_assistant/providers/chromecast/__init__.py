"""ChromeCast playerprovider."""

import logging
from typing import List, Optional

import pychromecast
from music_assistant.helpers.util import create_task
from music_assistant.models.config_entry import ConfigEntry
from music_assistant.models.provider import PlayerProvider
from pychromecast.controllers.multizone import MultizoneManager

from .const import PROV_ID, PROV_NAME, PROVIDER_CONFIG_ENTRIES
from .helpers import DEFAULT_PORT, ChromecastInfo
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
        self._browser: Optional[pychromecast.discovery.CastBrowser] = None
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
        self._browser = pychromecast.discovery.CastBrowser(
            pychromecast.discovery.SimpleCastListener(
                add_callback=self._discover_chromecast,
                remove_callback=self._remove_chromecast,
                update_callback=self._discover_chromecast,
            ),
            self.mass.zeroconf,
        )
        # start discovery in executor
        create_task(self._browser.start_discovery)
        return True

    async def on_stop(self):
        """Handle correct close/cleanup of the provider on exit."""
        if not self._browser:
            return
        # stop discovery
        create_task(self._browser.stop_discovery)

    def _discover_chromecast(self, uuid, _):
        """Discover a Chromecast."""
        device_info = self._browser.devices[uuid]

        info = ChromecastInfo(
            services=device_info.services,
            uuid=device_info.uuid,
            model_name=device_info.model_name,
            friendly_name=device_info.friendly_name,
            is_audio_group=device_info.port != DEFAULT_PORT,
        )

        if info.uuid is None:
            LOGGER.error("Discovered chromecast without uuid %s", info)
            return

        info = info.fill_out_missing_chromecast_info(self.mass.zeroconf)
        if info.is_dynamic_group:
            LOGGER.warning("Discovered dynamic cast group which will be ignored.")
            return

        LOGGER.debug("Discovered new or updated chromecast %s", info)
        player_id = str(info.uuid)
        player = self.mass.players.get_player(player_id)
        if not player:
            player = ChromecastPlayer(self.mass, info)
        # if player was already added, the player will take care of reconnects itself.
        player.set_cast_info(info)
        create_task(self.mass.players.add_player(player))

    def _remove_chromecast(self, uuid, service, cast_info):
        """Handle zeroconf discovery of a removed chromecast."""
        # pylint: disable=unused-argument
        player_id = str(service[1])
        friendly_name = service[3]
        LOGGER.debug("Chromecast removed: %s - %s", friendly_name, player_id)
        # we ignore this event completely as the Chromecast socket client handles this itself
