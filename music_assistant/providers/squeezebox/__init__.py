"""Squeezebox emulated player provider."""

import asyncio
import logging
from typing import List

from music_assistant.constants import CONF_CROSSFADE_DURATION
from music_assistant.helpers.typing import MusicAssistant
from music_assistant.helpers.util import callback, create_task
from music_assistant.models.config_entry import ConfigEntry
from music_assistant.models.player import DeviceInfo, Player, PlayerFeature, PlayerState
from music_assistant.models.player_queue import QueueItem
from music_assistant.models.provider import PlayerProvider

from .constants import PROV_ID, PROV_NAME
from .discovery import DiscoveryProtocol
from .socket_client import SqueezeEvent, SqueezeSocketClient

CONF_LAST_POWER = "last_power"
CONF_LAST_VOLUME = "last_volume"

LOGGER = logging.getLogger(PROV_ID)

CONFIG_ENTRIES = []  # we don't have any provider config entries (for now)
PLAYER_FEATURES = [PlayerFeature.QUEUE, PlayerFeature.CROSSFADE, PlayerFeature.GAPLESS]
PLAYER_CONFIG_ENTRIES = []  # we don't have any player config entries (for now)


async def setup(mass):
    """Perform async setup of this Plugin/Provider."""
    prov = PySqueezeProvider()
    await mass.register_provider(prov)


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

    async def on_start(self) -> bool:
        """Handle initialization of the provider. Called on startup."""
        # start slimproto server
        create_task(asyncio.start_server(self._client_connected, "0.0.0.0", 3483))

        # setup discovery
        create_task(self.start_discovery())

    async def start_discovery(self):
        """Start discovery for players."""
        transport, _ = await self.mass.loop.create_datagram_endpoint(
            lambda: DiscoveryProtocol(self.mass.web.port),
            local_addr=("0.0.0.0", 3483),
        )
        try:
            while True:
                await asyncio.sleep(60)  # serve forever
        finally:
            transport.close()

    async def _client_connected(self, reader, writer):
        """Handle a client connection on the socket."""
        addr = writer.get_extra_info("peername")
        LOGGER.debug("Socket client connected: %s", addr)
        socket_client = SqueezeSocketClient(self.mass, reader, writer)

        def handle_event(event: SqueezeEvent, socket_client: SqueezeSocketClient):
            player_id = socket_client.player_id
            if not player_id:
                return
            # always check if we already have this player as it might be reconnected
            player = self.mass.players.get_player(player_id)
            if not player:
                player = SqueezePlayer(self.mass, socket_client)
            player.set_socket_client(socket_client)
            # just update, the playermanager will take care of adding it if it's a new player
            player.handle_socket_client_event(event)

        socket_client.register_callback(handle_event)


class SqueezePlayer(Player):
    """Squeezebox player."""

    def __init__(self, mass: MusicAssistant, socket_client: SqueezeSocketClient):
        """Initialize."""
        super().__init__()
        self.mass = mass
        self._socket_client = socket_client

    @property
    def available(self) -> bool:
        """Return current availablity of player."""
        return self._socket_client.connected

    @property
    def should_poll(self) -> bool:
        """Return True if this player should be polled for state updates."""
        return False

    @property
    def socket_client(self):
        """Return the uinderluing socket client for the player."""
        return self._socket_client

    def set_socket_client(self, socket_client: SqueezeSocketClient):
        """Set a (new) socket client to this player."""
        self._socket_client = socket_client

    async def on_remove(self) -> None:
        """Call when player is removed from the player manager."""
        self.socket_client.disconnect()

    @property
    def player_id(self) -> str:
        """Return player id (=mac address) of the player."""
        return self.socket_client.player_id

    @property
    def provider_id(self) -> str:
        """Return provider id of this player."""
        return PROV_ID

    @property
    def name(self) -> str:
        """Return name of the player."""
        return self.socket_client.name

    @property
    def volume_level(self):
        """Return current volume level of player."""
        return self.socket_client.volume_level

    @property
    def powered(self):
        """Return current power state of player."""
        return self.socket_client.powered

    @property
    def muted(self):
        """Return current mute state of player."""
        return self.socket_client.muted

    @property
    def state(self):
        """Return current state of player."""
        return PlayerState(self.socket_client.state)

    @property
    def elapsed_time(self):
        """Return elapsed_time of current playing track in (fractions of) seconds."""
        return self.socket_client.elapsed_seconds

    @property
    def elapsed_milliseconds(self) -> int:
        """Return (realtime) elapsed time of current playing media in milliseconds."""
        return self.socket_client.elapsed_milliseconds

    @property
    def current_uri(self):
        """Return uri of currently loaded track."""
        return self.socket_client.current_uri

    @property
    def features(self) -> List[PlayerFeature]:
        """Return list of features this player supports."""
        return PLAYER_FEATURES

    @property
    def config_entries(self) -> List[ConfigEntry]:
        """Return player specific config entries (if any)."""
        return PLAYER_CONFIG_ENTRIES

    @property
    def device_info(self) -> DeviceInfo:
        """Return the device info for this player."""
        return DeviceInfo(
            model=self.socket_client.device_type,
            address=self.socket_client.device_address,
        )

    async def cmd_stop(self):
        """Send stop command to player."""
        return await self.socket_client.cmd_stop()

    async def cmd_play(self):
        """Send play (unpause) command to player."""
        return await self.socket_client.cmd_play()

    async def cmd_pause(self):
        """Send pause command to player."""
        return await self.socket_client.cmd_pause()

    async def cmd_power_on(self) -> None:
        """Send POWER ON command to player."""
        # save power and volume state in cache
        cache_str = f"squeezebox_player_{self.player_id}"
        await self.mass.cache.set(cache_str, (True, self.volume_level))
        return await self.socket_client.cmd_power(True)

    async def cmd_power_off(self) -> None:
        """Send POWER OFF command to player."""
        # save power and volume state in cache
        cache_str = f"squeezebox_player_{self.player_id}"
        await self.mass.cache.set(cache_str, (False, self.volume_level))
        return await self.socket_client.cmd_power(False)

    async def cmd_volume_set(self, volume_level: int):
        """Send new volume level command to player."""
        return await self.socket_client.cmd_volume_set(volume_level)

    async def cmd_mute(self, muted: bool = False):
        """Send mute command to player."""
        return await self.socket_client.cmd_mute(muted)

    async def cmd_play_uri(self, uri: str):
        """Request player to start playing a single uri."""
        crossfade = self.mass.config.player_settings[self.player_id][
            CONF_CROSSFADE_DURATION
        ]
        return await self.socket_client.play_uri(uri, crossfade_duration=crossfade)

    async def cmd_next(self):
        """Send NEXT TRACK command to player."""
        queue = self.mass.players.get_player_queue(self.player_id)
        if queue:
            new_track = queue.get_item(queue.cur_index + 1)
            if new_track:
                return await self.cmd_play_uri(new_track.stream_url)

    async def cmd_previous(self):
        """Send PREVIOUS TRACK command to player."""
        queue = self.mass.players.get_player_queue(self.player_id)
        if queue:
            new_track = queue.get_item(queue.cur_index - 1)
            if new_track:
                return await self.cmd_play_uri(new_track.stream_url)

    async def cmd_queue_play_index(self, index: int):
        """
        Play item at index X on player's queue.

            :param index: (int) index of the queue item that should start playing
        """
        queue = self.mass.players.get_player_queue(self.player_id)
        if queue:
            new_track = queue.get_item(index)
            if new_track:
                return await self.cmd_play_uri(new_track.stream_url)

    async def cmd_queue_load(self, queue_items: List[QueueItem]):
        """
        Load/overwrite given items in the player's queue implementation.

            :param queue_items: a list of QueueItems
        """
        if queue_items:
            await self.cmd_play_uri(queue_items[0].stream_url)
            return await self.cmd_play_uri(queue_items[0].stream_url)

    async def cmd_queue_insert(
        self, queue_items: List[QueueItem], insert_at_index: int
    ):
        """
        Insert new items at position X into existing queue.

        If insert_at_index 0 or None, will start playing newly added item(s)
            :param queue_items: a list of QueueItems
            :param insert_at_index: queue position to insert new items
        """
        # queue handled by built-in queue controller
        # we only check the start index
        queue = self.mass.players.get_player_queue(self.player_id)
        if queue and insert_at_index == queue.cur_index:
            return await self.cmd_queue_play_index(insert_at_index)

    async def cmd_queue_append(self, queue_items: List[QueueItem]):
        """
        Append new items at the end of the queue.

            :param queue_items: a list of QueueItems
        """
        # automagically handled by built-in queue controller

    async def cmd_queue_update(self, queue_items: List[QueueItem]):
        """
        Overwrite the existing items in the queue, used for reordering.

            :param queue_items: a list of QueueItems
        """
        # automagically handled by built-in queue controller

    async def cmd_queue_clear(self):
        """Clear the player's queue."""
        # queue is handled by built-in queue controller but send stop
        return await self.cmd_stop()

    async def restore_states(self):
        """Restore power/volume states."""
        cache_str = f"squeezebox_player_{self.player_id}"
        cache_data = await self.mass.cache.get(cache_str)
        last_power, last_volume = cache_data if cache_data else (False, 40)
        await self.socket_client.cmd_volume_set(last_volume)
        await self.socket_client.cmd_power(last_power)

    @callback
    def handle_socket_client_event(self, event: SqueezeEvent):
        """Process incoming event from the socket client."""
        if event == SqueezeEvent.CONNECTED:
            # restore previous power/volume
            create_task(self.restore_states())
        elif event == SqueezeEvent.DECODER_READY:
            # tell player to load next queue track
            queue = self.mass.players.get_player_queue(self.player_id)
            if queue:
                next_item = queue.next_item
                if next_item:
                    crossfade = self.mass.config.player_settings[self.player_id][
                        CONF_CROSSFADE_DURATION
                    ]
                    create_task(
                        self.socket_client.play_uri(
                            next_item.stream_url,
                            send_flush=False,
                            crossfade_duration=crossfade,
                        )
                    )
        self.update_state()
