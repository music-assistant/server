"""Player provider for Sonos speakers."""

import asyncio
import logging
import time
from typing import List

import soco
from music_assistant.helpers.util import create_task
from music_assistant.models.config_entry import ConfigEntry
from music_assistant.models.player import DeviceInfo, Player, PlayerFeature, PlayerState
from music_assistant.models.player_queue import QueueItem
from music_assistant.models.provider import PlayerProvider

PROV_ID = "sonos"
PROV_NAME = "Sonos"
LOGGER = logging.getLogger(PROV_ID)

CONFIG_ENTRIES = []  # we don't have any provider config entries (for now)
PLAYER_FEATURES = [PlayerFeature.QUEUE, PlayerFeature.CROSSFADE, PlayerFeature.GAPLESS]
PLAYER_CONFIG_ENTRIES = []  # we don't have any player config entries (for now)


class SonosProvider(PlayerProvider):
    """Support for Sonos speakers."""

    # pylint: disable=abstract-method

    _discovery_running = False
    _tasks = []
    _players = {}
    _report_progress_tasks = []

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
        """Handle initialization of the provider."""
        self.mass.tasks.add("Run Sonos discovery", self.__run_discovery, periodic=1800)

    async def on_stop(self):
        """Handle correct close/cleanup of the provider on exit."""
        for task in self._tasks:
            task.cancel()

    async def cmd_play_uri(self, player_id: str, uri: str):
        """
        Play the specified uri/url on the goven player.

            :param player_id: player_id of the player to handle the command.
        """
        player = self._players.get(player_id)
        if player:
            create_task(player.soco.play_uri, uri)
        else:
            LOGGER.warning("Received command for unavailable player: %s", player_id)

    async def cmd_stop(self, player_id: str) -> None:
        """
        Send STOP command to given player.

            :param player_id: player_id of the player to handle the command.
        """
        player = self._players.get(player_id)
        if player:
            create_task(player.soco.stop)
        else:
            LOGGER.warning("Received command for unavailable player: %s", player_id)

    async def cmd_play(self, player_id: str) -> None:
        """
        Send STOP command to given player.

            :param player_id: player_id of the player to handle the command.
        """
        player = self._players.get(player_id)
        if player:
            create_task(player.soco.play)
        else:
            LOGGER.warning("Received command for unavailable player: %s", player_id)

    async def cmd_pause(self, player_id: str):
        """
        Send PAUSE command to given player.

            :param player_id: player_id of the player to handle the command.
        """
        player = self._players.get(player_id)
        if player:
            create_task(player.soco.pause)
        else:
            LOGGER.warning("Received command for unavailable player: %s", player_id)

    async def cmd_next(self, player_id: str):
        """
        Send NEXT TRACK command to given player.

            :param player_id: player_id of the player to handle the command.
        """
        player = self._players.get(player_id)
        if player:
            create_task(player.soco.next)
        else:
            LOGGER.warning("Received command for unavailable player: %s", player_id)

    async def cmd_previous(self, player_id: str):
        """
        Send PREVIOUS TRACK command to given player.

            :param player_id: player_id of the player to handle the command.
        """
        player = self._players.get(player_id)
        if player:
            create_task(player.soco.previous)
        else:
            LOGGER.warning("Received command for unavailable player: %s", player_id)

    async def cmd_power_on(self, player_id: str) -> None:
        """
        Send POWER ON command to given player.

            :param player_id: player_id of the player to handle the command.
        """
        player = self._players.get(player_id)
        if player:
            # power is not supported so abuse mute instead
            player.soco.mute = False
            player.powered = True
        else:
            LOGGER.warning("Received command for unavailable player: %s", player_id)

    async def cmd_power_off(self, player_id: str) -> None:
        """
        Send POWER OFF command to given player.

            :param player_id: player_id of the player to handle the command.
        """
        player = self._players.get(player_id)
        if player:
            # power is not supported so abuse mute instead
            player.soco.mute = True
            player.powered = False
        else:
            LOGGER.warning("Received command for unavailable player: %s", player_id)

    async def cmd_volume_set(self, player_id: str, volume_level: int) -> None:
        """
        Send volume level command to given player.

            :param player_id: player_id of the player to handle the command.
            :param volume_level: volume level to set (0..100).
        """
        player = self._players.get(player_id)
        if player:
            player.soco.volume = volume_level
        else:
            LOGGER.warning("Received command for unavailable player: %s", player_id)

    async def cmd_volume_mute(self, player_id: str, is_muted=False):
        """
        Send volume MUTE command to given player.

            :param player_id: player_id of the player to handle the command.
            :param is_muted: bool with new mute state.
        """
        player = self._players.get(player_id)
        if player:
            player.soco.mute = is_muted
        else:
            LOGGER.warning("Received command for unavailable player: %s", player_id)

    async def cmd_queue_play_index(self, player_id: str, index: int):
        """
        Play item at index X on player's queue.

            :param player_id: player_id of the player to handle the command.
            :param index: (int) index of the queue item that should start playing
        """
        player = self._players.get(player_id)
        if player:
            create_task(player.soco.play_from_queue, index)
        else:
            LOGGER.warning("Received command for unavailable player: %s", player_id)

    async def cmd_queue_load(self, player_id: str, queue_items: List[QueueItem]):
        """
        Load/overwrite given items in the player's queue implementation.

            :param player_id: player_id of the player to handle the command.
            :param queue_items: a list of QueueItems
        """
        player = self._players.get(player_id)
        if player:
            create_task(player.soco.clear_queue)
            for pos, item in enumerate(queue_items):
                create_task(player.soco.add_uri_to_queue, item.stream_url, pos)
        else:
            LOGGER.warning("Received command for unavailable player: %s", player_id)

    async def cmd_queue_insert(
        self, player_id: str, queue_items: List[QueueItem], insert_at_index: int
    ):
        """
        Insert new items at position X into existing queue.

        If insert_at_index 0 or None, will start playing newly added item(s)
            :param player_id: player_id of the player to handle the command.
            :param queue_items: a list of QueueItems
            :param insert_at_index: queue position to insert new items
        """
        player = self._players.get(player_id)
        if player:
            for pos, item in enumerate(queue_items):
                create_task(
                    player.soco.add_uri_to_queue, item.stream_url, insert_at_index + pos
                )
        else:
            LOGGER.warning("Received command for unavailable player: %s", player_id)

    async def cmd_queue_append(self, player_id: str, queue_items: List[QueueItem]):
        """
        Append new items at the end of the queue.

            :param player_id: player_id of the player to handle the command.
            :param queue_items: a list of QueueItems
        """
        player_queue = self.mass.players.get_player_queue(player_id)
        if player_queue:
            return await self.cmd_queue_insert(
                player_id, queue_items, len(player_queue.items)
            )
        LOGGER.warning("Received command for unavailable player: %s", player_id)

    async def cmd_queue_clear(self, player_id: str):
        """
        Clear the player's queue.

            :param player_id: player_id of the player to handle the command.
        """
        player = self._players.get(player_id)
        if player:
            create_task(player.soco.clear_queue)
        else:
            LOGGER.warning("Received command for unavailable player: %s", player_id)

    def __run_discovery(self):
        """Background Sonos discovery and handler, runs in executor thread."""
        if self._discovery_running:
            return
        self._discovery_running = True
        LOGGER.debug("Sonos discovery started...")
        discovered_devices = soco.discover()
        if discovered_devices is None:
            discovered_devices = []
        new_device_ids = [item.uid for item in discovered_devices]
        cur_player_ids = [item.player_id for item in self._players.values()]
        # remove any disconnected players...
        for player in list(self._players.values()):
            if not player.is_group and player.soco.uid not in new_device_ids:
                create_task(self.mass.players.remove_player(player.player_id))
                for sub in player.subscriptions:
                    sub.unsubscribe()
                self._players.pop(player, None)
        # process new players
        for device in discovered_devices:
            if device.uid not in cur_player_ids and device.is_visible:
                self.__device_discovered(device)
        # handle groups
        if len(discovered_devices) > 0:
            self.__process_groups(discovered_devices[0].all_groups)
        else:
            self.__process_groups([])

    def __device_discovered(self, soco_device: soco.SoCo):
        """Handle discovered Sonos player."""
        speaker_info = soco_device.get_speaker_info(True)
        player = Player(
            player_id=soco_device.uid,
            provider_id=PROV_ID,
            name=soco_device.player_name,
            features=PLAYER_FEATURES,
            config_entries=PLAYER_CONFIG_ENTRIES,
            device_info=DeviceInfo(
                model=speaker_info["model_name"],
                address=speaker_info["mac_address"],
                manufacturer=PROV_NAME,
            ),
        )
        # store soco object on player
        player.soco = soco_device
        player.media_position_updated_at = 0
        # handle subscriptions to events
        player.subscriptions = []

        def subscribe(service, _callback):
            queue = ProcessSonosEventQueue(soco_device.uid, _callback)
            sub = service.subscribe(auto_renew=True, event_queue=queue)
            player.subscriptions.append(sub)

        subscribe(soco_device.avTransport, self.__player_event)
        subscribe(soco_device.renderingControl, self.__player_event)
        subscribe(soco_device.zoneGroupTopology, self.__topology_changed)
        create_task(self.mass.players.add_player(player))
        return player

    def __player_event(self, player_id: str, event):
        """Handle a SoCo player event."""
        player = self._players[player_id]
        if event:
            variables = event.variables
            if "volume" in variables:
                player.volume_level = int(variables["volume"]["Master"])
            if "mute" in variables:
                player.muted = variables["mute"]["Master"] == "1"
        else:
            player.volume_level = player.soco.volume
            player.muted = player.soco.mute
        transport_info = player.soco.get_current_transport_info()
        current_transport_state = transport_info.get("current_transport_state")
        if current_transport_state == "TRANSITIONING":
            return
        if player.soco.is_playing_tv or player.soco.is_playing_line_in:
            player.powered = False
        else:
            new_state = __convert_state(current_transport_state)
            player.state = new_state
            track_info = player.soco.get_current_track_info()
            player.current_uri = track_info["uri"]
            position_info = player.soco.avTransport.GetPositionInfo(
                [("InstanceID", 0), ("Channel", "Master")]
            )
            rel_time = __timespan_secs(position_info.get("RelTime"))
            player.elapsed_time = rel_time
            if player.state == PlayerState.PLAYING:
                create_task(self._report_progress(player_id))
        player.update_state()

    def __process_groups(self, sonos_groups):
        """Process all sonos groups."""
        all_group_ids = []
        for group in sonos_groups:
            all_group_ids.append(group.uid)
            if group.uid not in self._players:
                # new group player
                group_player = self.__device_discovered(group.coordinator)
            else:
                group_player = self._players[group.uid]
            # check members
            group_player.is_group_player = True
            group_player.name = group.label
            group_player.group_childs = [item.uid for item in group.members]
            create_task(self.mass.players.update_player(group_player))

    async def __topology_changed(self, player_id, event=None):
        """Received topology changed event from one of the sonos players."""
        # pylint: disable=unused-argument
        # Schedule discovery to work out the changes.
        create_task(self.__run_discovery)

    async def _report_progress(self, player_id: str):
        """Report current progress while playing."""
        if player_id in self._report_progress_tasks:
            return  # already running
        # sonos does not send instant updates of the player's progress (elapsed time)
        # so we need to send it in periodically
        player = self._players[player_id]
        player.should_poll = True
        while player and player.state == PlayerState.PLAYING:
            time_diff = time.time() - player.media_position_updated_at
            adjusted_current_time = player.elapsed_time + time_diff
            player.elapsed_time = adjusted_current_time
            await asyncio.sleep(1)
        player.should_poll = False
        self._report_progress_tasks.pop(player_id, None)


def __convert_state(sonos_state: str) -> PlayerState:
    """Convert Sonos state to PlayerState."""
    if sonos_state == "PLAYING":
        return PlayerState.PLAYING
    if sonos_state == "TRANSITIONING":
        return PlayerState.PLAYING
    if sonos_state == "PAUSED_PLAYBACK":
        return PlayerState.PAUSED
    return PlayerState.IDLE


def __timespan_secs(timespan):
    """Parse a time-span into number of seconds."""
    if timespan in ("", "NOT_IMPLEMENTED", None):
        return None
    return sum(60 ** x[0] * int(x[1]) for x in enumerate(reversed(timespan.split(":"))))


class ProcessSonosEventQueue:
    """Queue like object for dispatching sonos events."""

    def __init__(self, player_id, callback_handler):
        """Initialize Sonos event queue."""
        self._callback_handler = callback_handler
        self._player_id = player_id

    def put(self, item, block=True, timeout=None):
        """Process event."""
        # pylint: disable=unused-argument
        self._callback_handler(self._player_id, item)
