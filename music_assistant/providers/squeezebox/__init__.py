"""Squeezebox emulated player provider."""

import asyncio
import logging
from typing import List

from music_assistant.constants import CONF_CROSSFADE_DURATION
from music_assistant.models.config_entry import ConfigEntry
from music_assistant.models.player import DeviceInfo, PlayerFeature
from music_assistant.models.player_queue import QueueItem
from music_assistant.models.playerprovider import PlayerProvider

from .constants import PROV_ID, PROV_NAME
from .discovery import DiscoveryProtocol
from .socket_client import Event, SqueezeSocketClient

CONF_LAST_POWER = "last_power"
CONF_LAST_VOLUME = "last_volume"

LOGGER = logging.getLogger(PROV_ID)

CONFIG_ENTRIES = []  # we don't have any provider config entries (for now)
PLAYER_FEATURES = [PlayerFeature.QUEUE, PlayerFeature.CROSSFADE, PlayerFeature.GAPLESS]
PLAYER_CONFIG_ENTRIES = []  # we don't have any player config entries (for now)


async def async_setup(mass):
    """Perform async setup of this Plugin/Provider."""
    prov = PySqueezeProvider()
    await mass.async_register_provider(prov)


class PySqueezeProvider(PlayerProvider):
    """Python implementation of SlimProto server."""

    _socket_clients = {}
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
        for client in self._socket_clients.values():
            client.close()

    async def async_cmd_play_uri(self, player_id: str, uri: str):
        """
        Play the specified uri/url on the given player.

            :param player_id: player_id of the player to handle the command.
        """
        socket_client = self._socket_clients.get(player_id)
        if socket_client:
            crossfade = self.mass.config.player_settings[player_id][
                CONF_CROSSFADE_DURATION
            ]
            await socket_client.async_cmd_play_uri(
                uri, send_flush=True, crossfade_duration=crossfade
            )
        else:
            LOGGER.warning("Received command for unavailable player: %s", player_id)

    async def async_cmd_stop(self, player_id: str) -> None:
        """
        Send STOP command to given player.

            :param player_id: player_id of the player to handle the command.
        """
        socket_client = self._socket_clients.get(player_id)
        if socket_client:
            await socket_client.async_cmd_stop()
        else:
            LOGGER.warning("Received command for unavailable player: %s", player_id)

    async def async_cmd_play(self, player_id: str) -> None:
        """
        Send PLAY command to given player.

            :param player_id: player_id of the player to handle the command.
        """
        socket_client = self._socket_clients.get(player_id)
        if socket_client:
            await socket_client.async_cmd_play()
        else:
            LOGGER.warning("Received command for unavailable player: %s", player_id)

    async def async_cmd_pause(self, player_id: str):
        """
        Send PAUSE command to given player.

            :param player_id: player_id of the player to handle the command.
        """
        socket_client = self._socket_clients.get(player_id)
        if socket_client:
            await socket_client.async_cmd_pause()
        else:
            LOGGER.warning("Received command for unavailable player: %s", player_id)

    async def async_cmd_next(self, player_id: str):
        """
        Send NEXT TRACK command to given player.

            :param player_id: player_id of the player to handle the command.
        """
        queue = self.mass.player_manager.get_player_queue(player_id)
        if queue:
            new_track = queue.get_item(queue.cur_index + 1)
            if new_track:
                await self.async_cmd_play_uri(player_id, new_track.uri)

    async def async_cmd_previous(self, player_id: str):
        """
        Send PREVIOUS TRACK command to given player.

            :param player_id: player_id of the player to handle the command.
        """
        queue = self.mass.player_manager.get_player_queue(player_id)
        if queue:
            new_track = queue.get_item(queue.cur_index - 1)
            if new_track:
                await self.async_cmd_play_uri(player_id, new_track.uri)

    async def async_cmd_power_on(self, player_id: str) -> None:
        """
        Send POWER ON command to given player.

            :param player_id: player_id of the player to handle the command.
        """
        socket_client = self._socket_clients.get(player_id)
        if socket_client:
            await socket_client.async_cmd_power(True)
            # save power and volume state in cache
            cache_str = f"squeezebox_player_state_{player_id}"
            await self.mass.cache.async_set(
                cache_str, (True, socket_client.volume_level)
            )
        else:
            LOGGER.warning("Received command for unavailable player: %s", player_id)

    async def async_cmd_power_off(self, player_id: str) -> None:
        """
        Send POWER OFF command to given player.

            :param player_id: player_id of the player to handle the command.
        """
        socket_client = self._socket_clients.get(player_id)
        if socket_client:
            await socket_client.async_cmd_power(False)
            # store last power state as we need it when the player (re)connects
            # save power and volume state in cache
            cache_str = f"squeezebox_player_state_{player_id}"
            await self.mass.cache.async_set(
                cache_str, (False, socket_client.volume_level)
            )
        else:
            LOGGER.warning("Received command for unavailable player: %s", player_id)

    async def async_cmd_volume_set(self, player_id: str, volume_level: int) -> None:
        """
        Send volume level command to given player.

            :param player_id: player_id of the player to handle the command.
            :param volume_level: volume level to set (0..100).
        """
        socket_client = self._socket_clients.get(player_id)
        if socket_client:
            await socket_client.async_cmd_volume_set(volume_level)
            # save power and volume state in cache
            cache_str = f"squeezebox_player_state_{player_id}"
            await self.mass.cache.async_set(
                cache_str, (socket_client.powered, volume_level)
            )
        else:
            LOGGER.warning("Received command for unavailable player: %s", player_id)

    async def async_cmd_volume_mute(self, player_id: str, is_muted=False):
        """
        Send volume MUTE command to given player.

            :param player_id: player_id of the player to handle the command.
            :param is_muted: bool with new mute state.
        """
        socket_client = self._socket_clients.get(player_id)
        if socket_client:
            await socket_client.async_cmd_mute(is_muted)
        else:
            LOGGER.warning("Received command for unavailable player: %s", player_id)

    async def async_cmd_queue_play_index(self, player_id: str, index: int):
        """
        Play item at index X on player's queue.

            :param player_id: player_id of the player to handle the command.
            :param index: (int) index of the queue item that should start playing
        """
        queue = self.mass.player_manager.get_player_queue(player_id)
        if queue:
            new_track = queue.get_item(index)
            if new_track:
                await self.async_cmd_play_uri(player_id, new_track.uri)

    async def async_cmd_queue_load(self, player_id: str, queue_items: List[QueueItem]):
        """
        Load/overwrite given items in the player's queue implementation.

            :param player_id: player_id of the player to handle the command.
            :param queue_items: a list of QueueItems
        """
        if queue_items:
            await self.async_cmd_play_uri(player_id, queue_items[0].uri)

    async def async_cmd_queue_insert(
        self, player_id: str, queue_items: List[QueueItem], insert_at_index: int
    ):
        """
        Insert new items at position X into existing queue.

        If insert_at_index 0 or None, will start playing newly added item(s)
            :param player_id: player_id of the player to handle the command.
            :param queue_items: a list of QueueItems
            :param insert_at_index: queue position to insert new items
        """
        # queue handled by built-in queue controller
        # we only check the start index
        queue = self.mass.player_manager.get_player_queue(player_id)
        if queue and insert_at_index == queue.cur_index:
            return await self.async_cmd_queue_play_index(player_id, insert_at_index)

    async def async_cmd_queue_append(
        self, player_id: str, queue_items: List[QueueItem]
    ):
        """
        Append new items at the end of the queue.

            :param player_id: player_id of the player to handle the command.
            :param queue_items: a list of QueueItems
        """
        # automagically handled by built-in queue controller

    async def async_cmd_queue_update(
        self, player_id: str, queue_items: List[QueueItem]
    ):
        """
        Overwrite the existing items in the queue, used for reordering.

            :param player_id: player_id of the player to handle the command.
            :param queue_items: a list of QueueItems
        """
        # automagically handled by built-in queue controller

    async def async_cmd_queue_clear(self, player_id: str):
        """
        Clear the player's queue.

            :param player_id: player_id of the player to handle the command.
        """
        # queue is handled by built-in queue controller but send stop
        return await self.async_cmd_stop(player_id)

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
        SqueezeSocketClient(reader, writer, self.__client_event)

    async def __client_event(self, event: str, socket_client: SqueezeSocketClient):
        """Received event from a socket client."""
        if event == Event.EVENT_CONNECTED:
            # set some attributes to make the socket client compatible with mass player format
            socket_client.should_poll = False
            socket_client.provider_id = PROV_ID
            socket_client.available = True
            socket_client.is_group_player = False
            socket_client.group_childs = []
            socket_client.device_info = DeviceInfo(
                model=socket_client.device_type, address=socket_client.device_address
            )
            socket_client.features = PLAYER_FEATURES
            socket_client.config_entries = PLAYER_CONFIG_ENTRIES
            # restore power/volume states
            cache_str = f"squeezebox_player_state_{socket_client.player_id}"
            cache_data = await self.mass.cache.async_get(cache_str)
            last_power, last_volume = cache_data if cache_data else (False, 40)
            await socket_client.async_cmd_volume_set(last_volume)
            await socket_client.async_cmd_power(last_power)
            await self.mass.player_manager.async_add_player(socket_client)
            self._socket_clients[socket_client.player_id] = socket_client
        elif event == Event.EVENT_UPDATED:
            await self.mass.player_manager.async_update_player(socket_client)
        elif event == Event.EVENT_DISCONNECTED:
            await self.mass.player_manager.async_remove_player(socket_client.player_id)
            self._socket_clients.pop(socket_client.player_id, None)
            del socket_client
        elif event == Event.EVENT_DECODER_READY:
            # player is ready for the next track (if any)
            player_id = socket_client.player_id
            queue = self.mass.player_manager.get_player_queue(socket_client.player_id)
            if queue:
                next_item = queue.next_item
                if next_item:
                    crossfade = self.mass.config.player_settings[player_id][
                        CONF_CROSSFADE_DURATION
                    ]
                    await self._socket_clients[player_id].async_cmd_play_uri(
                        next_item.uri, send_flush=False, crossfade_duration=crossfade
                    )
