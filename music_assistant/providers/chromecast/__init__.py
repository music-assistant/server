"""ChromeCast playerprovider."""

import logging
import uuid
from typing import List

import pychromecast
from music_assistant.models.config_entry import ConfigEntry
from music_assistant.models.player_queue import QueueItem
from music_assistant.models.playerprovider import PlayerProvider
from pychromecast.controllers.multizone import MultizoneManager

from .const import PROV_ID, PROV_NAME, PROVIDER_CONFIG_ENTRIES
from .models import ChromecastInfo
from .player import ChromecastPlayer

LOGGER = logging.getLogger(PROV_ID)


async def async_setup(mass):
    """Perform async setup of this Plugin/Provider."""
    logging.getLogger("pychromecast").setLevel(logging.WARNING)
    prov = ChromecastProvider()
    await mass.async_register_provider(prov)


class ChromecastProvider(PlayerProvider):
    """Support for ChromeCast Audio PlayerProvider."""

    # pylint: disable=abstract-method

    def __init__(self, *args, **kwargs):
        """Initialize."""
        self.mz_mgr = MultizoneManager()
        self._players = {}
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

    async def async_on_start(self) -> bool:
        """Handle initialization of the provider based on config."""
        self._listener = pychromecast.CastListener(
            self.__chromecast_add_update_callback,
            self.__chromecast_remove_callback,
            self.__chromecast_add_update_callback,
        )
        self._browser = pychromecast.discovery.start_discovery(
            self._listener, self.mass.zeroconf
        )
        return True

    async def async_on_stop(self):
        """Handle correct close/cleanup of the provider on exit."""
        if not self._browser:
            return
        # stop discovery
        pychromecast.stop_discovery(self._browser)
        # stop cast socket clients
        for player in self._players.values():
            player.disconnect()

    async def async_cmd_play_uri(self, player_id: str, uri: str):
        """
        Play the specified uri/url on the goven player.

            :param player_id: player_id of the player to handle the command.
        """
        self.mass.add_job(self._players[player_id].play_uri, uri)

    async def async_cmd_stop(self, player_id: str) -> None:
        """
        Send STOP command to given player.

            :param player_id: player_id of the player to handle the command.
        """
        self.mass.add_job(self._players[player_id].stop)

    async def async_cmd_play(self, player_id: str) -> None:
        """
        Send STOP command to given player.

            :param player_id: player_id of the player to handle the command.
        """
        self.mass.add_job(self._players[player_id].play)

    async def async_cmd_pause(self, player_id: str):
        """
        Send PAUSE command to given player.

            :param player_id: player_id of the player to handle the command.
        """
        self.mass.add_job(self._players[player_id].pause)

    async def async_cmd_next(self, player_id: str):
        """
        Send NEXT TRACK command to given player.

            :param player_id: player_id of the player to handle the command.
        """
        self.mass.add_job(self._players[player_id].next)

    async def async_cmd_previous(self, player_id: str):
        """
        Send PREVIOUS TRACK command to given player.

            :param player_id: player_id of the player to handle the command.
        """
        self.mass.add_job(self._players[player_id].previous)

    async def async_cmd_power_on(self, player_id: str) -> None:
        """
        Send POWER ON command to given player.

            :param player_id: player_id of the player to handle the command.
        """
        self.mass.add_job(self._players[player_id].power_on)

    async def async_cmd_power_off(self, player_id: str) -> None:
        """
        Send POWER OFF command to given player.

            :param player_id: player_id of the player to handle the command.
        """
        self.mass.add_job(self._players[player_id].power_off)

    async def async_cmd_volume_set(self, player_id: str, volume_level: int) -> None:
        """
        Send volume level command to given player.

            :param player_id: player_id of the player to handle the command.
            :param volume_level: volume level to set (0..100).
        """
        self.mass.add_job(self._players[player_id].volume_set, volume_level / 100)

    async def async_cmd_volume_mute(self, player_id: str, is_muted=False):
        """
        Send volume MUTE command to given player.

            :param player_id: player_id of the player to handle the command.
            :param is_muted: bool with new mute state.
        """
        self.mass.add_job(self._players[player_id].volume_mute, is_muted)

    async def async_cmd_queue_load(self, player_id: str, queue_items: List[QueueItem]):
        """
        Load/overwrite given items in the player's queue implementation.

            :param player_id: player_id of the player to handle the command.
            :param queue_items: a list of QueueItems
        """
        self.mass.add_job(self._players[player_id].queue_load, queue_items)

    async def async_cmd_queue_append(
        self, player_id: str, queue_items: List[QueueItem]
    ):
        """
        Append new items at the end of the queue.

            :param player_id: player_id of the player to handle the command.
            :param queue_items: a list of QueueItems
        """
        self.mass.add_job(self._players[player_id].queue_append, queue_items)

    def __chromecast_add_update_callback(self, cast_uuid, cast_service_name):
        """Handle zeroconf discovery of a new or updated chromecast."""
        # pylint: disable=unused-argument
        if uuid is None:
            return  # Discovered chromecast without uuid
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
        if player_id in self._players:
            # player already added, the player will take care of reconnects itself.
            LOGGER.debug(
                "Player is already added:  %s (%s)", cast_info.friendly_name, player_id
            )
        else:
            player = ChromecastPlayer(self.mass, cast_info)
            self._players[player_id] = player
            self.mass.add_job(self.mass.player_manager.async_add_player(player))
        self.mass.add_job(self._players[player_id].set_cast_info, cast_info)

    def __chromecast_remove_callback(self, cast_uuid, cast_service_name, cast_service):
        """Handle a Chromecast removed event."""
        # pylint: disable=unused-argument
        player_id = str(cast_service[1])
        friendly_name = cast_service[3]
        LOGGER.debug("Chromecast removed: %s - %s", friendly_name, player_id)
        self.mass.add_job(self.mass.player_manager.async_remove_player(player_id))
