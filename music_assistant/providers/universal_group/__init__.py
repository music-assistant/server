"""Group player provider: enables grouping of all Players."""

import asyncio
import logging
from typing import List

from music_assistant.helpers.typing import MusicAssistant
from music_assistant.helpers.util import create_task
from music_assistant.models.config_entry import ConfigEntry, ConfigEntryType
from music_assistant.models.player import DeviceInfo, Player, PlayerState
from music_assistant.models.provider import PlayerProvider

PROV_ID = "universal_group"
PROV_NAME = "Universal Group player"
LOGGER = logging.getLogger(PROV_ID)

CONF_PLAYER_COUNT = "group_player_count"
CONF_PLAYERS = "group_player_players"
CONF_MASTER = "group_player_master"

CONFIG_ENTRIES = [
    ConfigEntry(
        entry_key=CONF_PLAYER_COUNT,
        entry_type=ConfigEntryType.INT,
        description=CONF_PLAYER_COUNT,
        default_value=1,
        range=(0, 10),
    )
]


async def setup(mass):
    """Perform async setup of this Plugin/Provider."""
    prov = GroupPlayerProvider()
    await mass.register_provider(prov)


class GroupPlayerProvider(PlayerProvider):
    """PlayerProvider which allows users to group players."""

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
        """Handle initialization of the provider based on config."""
        conf = self.mass.config.player_providers[PROV_ID]
        for index in range(conf[CONF_PLAYER_COUNT]):
            player = GroupPlayer(self.mass, index)
            await self.mass.players.add_player(player)
        return True

    async def on_stop(self):
        """Handle correct close/cleanup of the provider on exit. Called on shutdown."""
        for player in self.players:
            await player.cmd_stop()


class GroupPlayer(Player):
    """Model for a group player."""

    def __init__(self, mass: MusicAssistant, player_index: int):
        """Initialize."""
        super().__init__()
        self.mass = mass
        self._player_index = player_index
        self._player_id = f"{PROV_ID}_{player_index}"
        self._provider_id = PROV_ID
        self._name = f"{PROV_NAME} {player_index}"
        self._powered = False
        self._state = PlayerState.IDLE
        self._available = True
        self._current_uri = ""
        self._volume_level = 0
        self._muted = False
        self.connected_clients = {}
        self.stream_task = None
        self.sync_task = None
        self._config_entries = self.__get_config_entries()
        self._group_childs = self.__get_group_childs()

    @property
    def player_id(self) -> str:
        """Return player id of this player."""
        return self._player_id

    @property
    def provider_id(self) -> str:
        """Return provider id of this player."""
        return self._provider_id

    @property
    def name(self) -> str:
        """Return name of the player."""
        return self._name

    @property
    def powered(self) -> bool:
        """Return current power state of player."""
        return self._powered

    @property
    def state(self) -> PlayerState:
        """Return current PlayerState of player."""
        return self._state

    @property
    def available(self) -> bool:
        """Return current availablity of player."""
        return True

    @property
    def current_uri(self) -> str:
        """Return currently loaded uri of player (if any)."""
        return self._current_uri

    @property
    def volume_level(self) -> int:
        """Return current volume level of player (scale 0..100)."""
        return self._volume_level

    @property
    def muted(self) -> bool:
        """Return current mute state of player."""
        return self._muted

    @property
    def elapsed_time(self):
        """Return elapsed time for first child player."""
        if self.state in [PlayerState.PLAYING, PlayerState.PAUSED]:
            for player_id in self.group_childs:
                player = self.mass.players.get_player(player_id)
                if player:
                    return player.elapsed_time
        return 0

    @property
    def should_poll(self):
        """Return True if this player should be polled for state."""
        return True

    @property
    def is_group_player(self) -> bool:
        """Return True if this player is a group player."""
        return True

    @property
    def group_childs(self):
        """Return group childs of this group player."""
        return self._group_childs

    @property
    def device_info(self) -> DeviceInfo:
        """Return deviceinfo."""
        return DeviceInfo(
            model="Group Player",
            manufacturer=PROV_ID,
        )

    @property
    def config_entries(self):
        """Return config entries for this group player."""
        return self._config_entries

    async def on_poll(self) -> None:
        """Call when player is periodically polled by the player manager (should_poll=True)."""
        self._config_entries = self.__get_config_entries()
        self._group_childs = self.__get_group_childs()
        self.update_state()

    def __get_group_childs(self):
        """Return group childs of this group player."""
        player_conf = self.mass.config.get_player_config(self.player_id)
        if player_conf and player_conf.get(CONF_PLAYERS):
            return player_conf[CONF_PLAYERS]
        return []

    def __get_config_entries(self):
        """Return config entries for this group player."""
        all_players = [
            {"text": item.name, "value": item.player_id}
            for item in self.mass.players
            if item.player_id is not self._player_id
        ]
        selected_players_ids = self.mass.config.get_player_config(self.player_id).get(
            CONF_PLAYERS, []
        )
        # selected_players_ids = []
        selected_players = []
        for player_id in selected_players_ids:
            player = self.mass.players.get_player(player_id)
            if player:
                selected_players.append(
                    {"text": player.name, "value": player.player_id}
                )
        default_master = ""
        if selected_players:
            default_master = selected_players[0]["value"]
        return [
            ConfigEntry(
                entry_key=CONF_PLAYERS,
                entry_type=ConfigEntryType.STRING,
                default_value=[],
                values=all_players,
                label=CONF_PLAYERS,
                description="group_player_players_desc",
                multi_value=True,
            ),
            ConfigEntry(
                entry_key=CONF_MASTER,
                entry_type=ConfigEntryType.STRING,
                default_value=default_master,
                values=selected_players,
                label=CONF_MASTER,
                description="group_player_master_desc",
                multi_value=False,
                depends_on=CONF_PLAYERS,
            ),
        ]

    # SERVICE CALLS / PLAYER COMMANDS

    async def cmd_play_uri(self, uri: str):
        """Play the specified uri/url on the player."""
        await self.cmd_stop()
        self._current_uri = uri
        self._state = PlayerState.PLAYING
        self._powered = True
        # forward this command to each child player
        # TODO: Only start playing on powered players ?
        # Monitor if a child turns on and join it to the sync ?
        for child_player_id in self.group_childs:
            child_player = self.mass.players.get_player(child_player_id)
            if child_player:
                queue_stream_uri = f"{self.mass.web.stream_url}/group/{self.player_id}?player_id={child_player_id}"
                await child_player.cmd_play_uri(queue_stream_uri)
        self.update_state()
        self.stream_task = create_task(self.queue_stream_task())

    async def cmd_stop(self) -> None:
        """Send STOP command to player."""
        self._state = PlayerState.IDLE
        if self.stream_task:
            # cancel existing stream task if any
            self.stream_task.cancel()
            self.connected_clients = {}
            await asyncio.sleep(0.5)
        if self.sync_task:
            self.sync_task.cancel()
        # forward this command to each child player
        # TODO: Only forward to powered child players
        for child_player_id in self.group_childs:
            child_player = self.mass.players.get_player(child_player_id)
            if child_player:
                await child_player.cmd_stop()
        self.update_state()

    async def cmd_play(self) -> None:
        """Send PLAY command to player."""
        if not self.state == PlayerState.PAUSED:
            return
        # forward this command to each child player
        for child_player_id in self.group_childs:
            child_player = self.mass.players.get_player(child_player_id)
            if child_player:
                await child_player.cmd_play()
        self._state = PlayerState.PLAYING
        self.update_state()

    async def cmd_pause(self):
        """Send PAUSE command to player."""
        # forward this command to each child player
        for child_player_id in self.group_childs:
            child_player = self.mass.players.get_player(child_player_id)
            if child_player:
                await child_player.cmd_pause()
        self._state = PlayerState.PAUSED
        self.update_state()

    async def cmd_power_on(self) -> None:
        """Send POWER ON command to player."""
        self._powered = True
        self.update_state()

    async def cmd_power_off(self) -> None:
        """Send POWER OFF command to player."""
        self._powered = False
        self.update_state()

    async def cmd_volume_set(self, volume_level: int) -> None:
        """
        Send volume level command to player.

            :param volume_level: volume level to set (0..100).
        """
        # this is already handled by the player manager

    async def cmd_volume_mute(self, is_muted=False):
        """
        Send volume MUTE command to given player.

            :param is_muted: bool with new mute state.
        """
        for child_player_id in self.group_childs:
            self.mass.players.cmd_volume_mute(child_player_id)
        self.muted = is_muted

    async def subscribe_stream_client(self, child_player_id):
        """Handle streaming to all players of a group. Highly experimental."""

        # each connected client gets its own Queue to which audio chunks (flac) are sent
        try:
            # report this client as connected
            queue = asyncio.Queue()
            self.connected_clients[child_player_id] = queue
            LOGGER.debug(
                "[%s] child player connected: %s",
                self.player_id,
                child_player_id,
            )
            # yield flac chunks from stdout to the http streamresponse
            while True:
                chunk = await queue.get()
                yield chunk
                queue.task_done()
                if not chunk:
                    break
        except (GeneratorExit, Exception):  # pylint: disable=broad-except
            LOGGER.warning(
                "[%s] child player aborted stream: %s", self.player_id, child_player_id
            )
            self.connected_clients.pop(child_player_id, None)
        else:
            self.connected_clients.pop(child_player_id, None)
            LOGGER.debug(
                "[%s] child player completed streaming: %s",
                self.player_id,
                child_player_id,
            )

    async def queue_stream_task(self):
        """Handle streaming queue to connected child players."""
        ticks = 0
        while ticks < 60 and (len(self.connected_clients) != len(self.group_childs)):
            # TODO: Support situation where not all clients of the group are powered
            await asyncio.sleep(0.1)
            ticks += 1
        if not self.connected_clients:
            LOGGER.warning("no clients!")
            return
        LOGGER.debug(
            "start queue stream with %s connected clients", len(self.connected_clients)
        )
        self.sync_task = create_task(self.__synchronize_players())

        async for audio_chunk in self.mass.streams.queue_stream_flac(self.player_id):

            # make sure we still have clients connected
            if not self.connected_clients:
                LOGGER.warning("no more clients!")
                return

            # send the audio chunk to all connected players
            tasks = []
            for _queue in self.connected_clients.values():
                tasks.append(create_task(_queue.put(audio_chunk)))
            # wait for clients to consume the data
            await asyncio.wait(tasks)

            if not self.connected_clients:
                LOGGER.warning("no more clients!")
                return
        self.sync_task.cancel()

    async def __synchronize_players(self):
        """Handle drifting/lagging by monitoring progress and compare to master player."""

        master_player_id = self.mass.config.player_settings[self.player_id].get(
            CONF_MASTER
        )
        master_player = self.mass.players.get_player(master_player_id)
        if not master_player:
            LOGGER.warning("Synchronization of playback aborted: no master player.")
            return
        LOGGER.debug(
            "Synchronize playback of group using master player %s", master_player.name
        )

        # wait until master is playing
        while master_player.state != PlayerState.PLAYING:
            await asyncio.sleep(0.1)
        await asyncio.sleep(0.5)

        prev_lags = {}
        prev_drifts = {}

        while self.connected_clients:

            # check every 0.5 seconds for player sync
            await asyncio.sleep(0.5)

            for child_player_id in self.connected_clients:

                if child_player_id == master_player_id:
                    continue
                child_player = self.mass.players.get_player(child_player_id)

                if (
                    not child_player
                    or child_player.state != PlayerState.PLAYING
                    or child_player.elapsed_milliseconds is None
                ):
                    continue

                if child_player_id not in prev_lags:
                    prev_lags[child_player_id] = []
                if child_player_id not in prev_drifts:
                    prev_drifts[child_player_id] = []

                # calculate lag (player is too slow in relation to the master)
                lag = (
                    master_player.elapsed_milliseconds
                    - child_player.elapsed_milliseconds
                )
                prev_lags[child_player_id].append(lag)
                if len(prev_lags[child_player_id]) == 5:
                    # if we have 5 samples calclate the average lag
                    avg_lag = sum(prev_lags[child_player_id]) / len(
                        prev_lags[child_player_id]
                    )
                    prev_lags[child_player_id] = []
                    if avg_lag > 25:
                        LOGGER.debug(
                            "child player %s is lagging behind with %s milliseconds",
                            child_player_id,
                            avg_lag,
                        )
                        # we correct the lag by pausing the master player for a very short time
                        await master_player.cmd_pause()
                        # sending the command takes some time, account for that too
                        if avg_lag > 20:
                            sleep_time = avg_lag - 20
                            await asyncio.sleep(sleep_time / 1000)
                        create_task(master_player.cmd_play())
                        break  # no more processing this round if we've just corrected a lag

                # calculate drift (player is going faster in relation to the master)
                drift = (
                    child_player.elapsed_milliseconds
                    - master_player.elapsed_milliseconds
                )
                prev_drifts[child_player_id].append(drift)
                if len(prev_drifts[child_player_id]) == 5:
                    # if we have 5 samples calculate the average drift
                    avg_drift = sum(prev_drifts[child_player_id]) / len(
                        prev_drifts[child_player_id]
                    )
                    prev_drifts[child_player_id] = []

                    if avg_drift > 25:
                        LOGGER.debug(
                            "child player %s is drifting ahead with %s milliseconds",
                            child_player_id,
                            avg_drift,
                        )
                        # we correct the drift by pausing the player for a very short time
                        # this is not the best approach but works with all Players
                        # temporary solution until I find something better like sending more/less pcm chunks
                        await child_player.cmd_pause()
                        # sending the command takes some time, account for that too
                        if avg_drift > 20:
                            sleep_time = drift - 20
                            await asyncio.sleep(sleep_time / 1000)
                        await child_player.cmd_play()
                        break  # no more processing this round if we've just corrected a lag
