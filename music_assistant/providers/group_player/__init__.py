"""Group player provider: enables grouping of all playertypes."""

import asyncio
import logging
from typing import List

from music_assistant.constants import CONF_GROUP_DELAY
from music_assistant.helpers.typing import MusicAssistantType
from music_assistant.models.config_entry import ConfigEntry, ConfigEntryType
from music_assistant.models.player import DeviceInfo, PlayerState
from music_assistant.models.playerprovider import PlayerProvider
from music_assistant.utils import create_wave_header

PROV_ID = "group_player"
PROV_NAME = "Group player creator"
LOGGER = logging.getLogger(PROV_ID)

CONF_PLAYER_COUNT = "groupplayer_player_count"
CONF_PLAYERS = "groupplayer_players"

CONFIG_ENTRIES = [
    ConfigEntry(
        entry_key=CONF_PLAYER_COUNT,
        entry_type=ConfigEntryType.INT,
        description_key=CONF_PLAYER_COUNT,
        default_value=1,
        range=(0, 10),
    )
]


async def async_setup(mass):
    """Perform async setup of this Plugin/Provider."""
    prov = GroupPlayerProvider()
    await mass.async_register_provider(prov)


class GroupPlayerProvider(PlayerProvider):
    """PlayerProvider which allows users to group players."""

    def __init__(self, *args, **kwargs):
        """Initialize."""
        self._players = {}
        self._progress_tasks = {}
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
        return CONFIG_ENTRIES

    async def async_on_start(self) -> bool:
        """Handle initialization of the provider based on config."""
        conf = self.mass.config.providers[PROV_ID]
        for index in range(conf[CONF_PLAYER_COUNT]):
            player = GroupPlayer(self.mass, index)
            self._players[player.player_id] = player
            self.mass.add_job(self.mass.player_manager.async_add_player(player))
        return True

    async def async_on_stop(self):
        """Handle correct close/cleanup of the provider on exit. Called on shutdown."""
        for player_id in self._players:
            await self.async_cmd_stop(player_id)

    # SERVICE CALLS / PLAYER COMMANDS

    async def async_cmd_play_uri(self, player_id: str, uri: str):
        """
        Play the specified uri/url on the given player.

            :param player_id: player_id of the player to handle the command.
        """

        player = self._players[player_id]
        await self.async_cmd_stop(player_id)
        player.current_uri = uri
        player.state = PlayerState.Playing
        player.powered = True
        # forward this command to each child player
        for child_player_id in player.group_childs:
            prov = self.mass.player_manager.get_player_provider(child_player_id)
            if prov:
                queue_stream_uri = f"{self.mass.web.internal_url}/stream_group/{player_id}?player_id={child_player_id}"
                await prov.async_cmd_play_uri(child_player_id, queue_stream_uri)
        self.mass.add_job(self.mass.player_manager.async_update_player(player))
        player.stream_task = self.mass.add_job(player.start_queue_stream())

    async def async_cmd_stop(self, player_id: str) -> None:
        """
        Send STOP command to given player.

            :param player_id: player_id of the player to handle the command.
        """
        player = self._players[player_id]
        player.state = PlayerState.Stopped
        if player.stream_task:
            # cancel existing stream task if any
            player.stream_task.cancel()
            player.connected_clients = {}
        # forward this command to each child player
        for child_player_id in player.group_childs:
            prov = self.mass.player_manager.get_player_provider(child_player_id)
            if prov:
                await prov.async_cmd_stop(child_player_id)
        self.mass.add_job(self.mass.player_manager.async_update_player(player))

    async def async_cmd_play(self, player_id: str) -> None:
        """
        Send PLAY command to given player.

            :param player_id: player_id of the player to handle the command.
        """
        player = self._players[player_id]
        if not player.state == PlayerState.Paused:
            return
        # forward this command to each child player
        for child_player_id in player.group_childs:
            prov = self.mass.player_manager.get_player_provider(child_player_id)
            if prov:
                await prov.async_cmd_play(child_player_id)
        player.state = PlayerState.Playing
        self.mass.add_job(self.mass.player_manager.async_update_player(player))

    async def async_cmd_pause(self, player_id: str):
        """
        Send PAUSE command to given player.

            :param player_id: player_id of the player to handle the command.
        """
        player = self._players[player_id]
        # forward this command to each child player
        for child_player_id in player.group_childs:
            prov = self.mass.player_manager.get_player_provider(child_player_id)
            if prov:
                await prov.async_cmd_pause(child_player_id)
        player.state = PlayerState.Paused
        self.mass.add_job(self.mass.player_manager.async_update_player(player))

    async def async_cmd_power_on(self, player_id: str) -> None:
        """
        Send POWER ON command to given player.

            :param player_id: player_id of the player to handle the command.
        """
        self._players[player_id].powered = True
        self.mass.add_job(
            self.mass.player_manager.async_update_player(self._players[player_id])
        )

    async def async_cmd_power_off(self, player_id: str) -> None:
        """
        Send POWER OFF command to given player.

            :param player_id: player_id of the player to handle the command.
        """
        await self.async_cmd_stop(player_id)
        self._players[player_id].powered = False
        self.mass.add_job(
            self.mass.player_manager.async_update_player(self._players[player_id])
        )

    async def async_cmd_volume_set(self, player_id: str, volume_level: int) -> None:
        """
        Send volume level command to given player.

            :param player_id: player_id of the player to handle the command.
            :param volume_level: volume level to set (0..100).
        """
        self._players[player_id].volume_level = volume_level
        self.mass.add_job(
            self.mass.player_manager.async_update_player(self._players[player_id])
        )

    async def async_cmd_volume_mute(self, player_id: str, is_muted=False):
        """
        Send volume MUTE command to given player.

            :param player_id: player_id of the player to handle the command.
            :param is_muted: bool with new mute state.
        """
        self._players[player_id].muted = is_muted
        self.mass.add_job(
            self.mass.player_manager.async_update_player(self._players[player_id])
        )

    async def subscribe_stream_client(
        self, group_player_id, child_player_id, http_handle
    ):
        """Handle streaming to all players of a group. Highly experimental."""
        try:
            group_player = self._players[group_player_id]
            group_player.connected_clients[child_player_id] = http_handle
            await http_handle.write(create_wave_header(96000, 2, 32))
            LOGGER.debug(
                "[%s] child player conected: %s",
                group_player.player_id,
                child_player_id,
            )
            while True:
                await asyncio.sleep(30)
        except Exception as exc:
            LOGGER.warning(
                "[%s] child player disconected: %s - %s",
                group_player.player_id,
                child_player_id,
                str(exc),
            )
            group_player.connected_clients.pop(child_player_id, None)


class GroupPlayer:
    """Model for a group player."""

    def __init__(self, mass: MusicAssistantType, player_index: int):
        """Initialize."""
        self.mass = mass
        self._player_index = player_index
        self.player_id = f"group_player_{player_index}"
        self.provider_id = PROV_ID
        self.name = f"Group Player {player_index}"
        self.powered = False
        self.state = PlayerState.Stopped
        self.available = True
        self.current_uri = ""
        self.volume_level = 0
        self.muted = False
        self.is_group_player = True
        self.device_info = DeviceInfo(
            model="Group Player",
            manufacturer=PROV_ID,
        )
        self.features = []
        self.connected_clients = {}
        self.stream_task = None

    @property
    def elapsed_time(self):
        """Return elapsed timefor first child player."""
        if self.state in [PlayerState.Playing, PlayerState.Paused]:
            for player_id in self.group_childs:
                player = self.mass.player_manager.get_player(player_id)
                if player:
                    return player.elapsed_time
        return 0

    @property
    def should_poll(self):
        """Return True if this player should be polled for state."""
        return self.state in [PlayerState.Playing, PlayerState.Paused]

    @property
    def group_childs(self):
        """Return group childs of this group player."""
        player_conf = self.mass.config.get_player_config(self.player_id)
        if player_conf and player_conf.get(CONF_PLAYERS):
            return player_conf[CONF_PLAYERS]
        return []

    @property
    def config_entries(self):
        """Return config entries for this group player."""
        all_players = [
            {"text": item.name, "value": item.player_id}
            for item in self.mass.player_manager.players
            if item.player_id is not self.player_id
        ]
        return [
            ConfigEntry(
                entry_key=CONF_PLAYERS,
                entry_type=ConfigEntryType.STRING,
                default_value=[],
                values=all_players,
                description_key=CONF_PLAYERS,
                multi_value=True,
            )
        ]

    async def start_queue_stream(self):
        """Start streaming audio to connected clients."""
        ticks = 0
        while ticks < 60 and len(self.connected_clients) != len(self.group_childs):
            await asyncio.sleep(0.1)
            ticks += 1
        if not self.connected_clients:
            LOGGER.warning("no clients!")
            return
        LOGGER.debug(
            "start queue stream with %s connected clients", len(self.connected_clients)
        )

        # pick a master player, by default it's the first powered player with 0 delay
        # TODO: make master player configurable ?
        master_player_id = ""
        for child_player_id in self.connected_clients:
            if self.mass.config.player_settings[child_player_id][CONF_GROUP_DELAY] == 0:
                master_player_id = child_player_id
                break
        if not master_player_id:
            master_player_id = list(self.connected_clients.keys())[0]

        # set all players in paused state and resume
        for child_player_id in self.connected_clients:
            prov = self.mass.player_manager.get_player_provider(child_player_id)
            if prov:
                await prov.async_cmd_pause(child_player_id)
        await asyncio.sleep(1)
        for child_player_id in self.connected_clients:
            prov = self.mass.player_manager.get_player_provider(child_player_id)
            if prov:
                await prov.async_cmd_play(child_player_id)

        received_milliseconds = 0
        check_drift_counter = 0
        async for audio_chunk in self.mass.stream_manager.async_stream_queue(
            self.player_id, send_wave_header=False
        ):

            received_milliseconds += 1000
            chunk_size = len(audio_chunk)

            # make sure we still have clients connected
            if not self.connected_clients:
                LOGGER.warning("no more clients!")
                return

            for child_player_id, http_client in list(self.connected_clients.items()):

                # work out startdelays
                player_delay = self.mass.config.player_settings[child_player_id][
                    CONF_GROUP_DELAY
                ]
                if player_delay:
                    if received_milliseconds <= player_delay:
                        continue  # skip this chunk
                    start_bytes = int(
                        ((player_delay - received_milliseconds) / 1000) * chunk_size
                    )
                else:
                    start_bytes = 0

                # TODO: handle drifting by continu monitoring progress and compare to master player
                if check_drift_counter == 5:
                    check_drift_counter = 0

                    master_player = self.mass.player_manager._org_players[
                        master_player_id
                    ]
                    master_player_milliseconds = getattr(
                        master_player,
                        "elapsed_time_milliseconds",
                        master_player.elapsed_time * 1000,
                    )

                    child_player = self.mass.player_manager._org_players[
                        child_player_id
                    ]
                    child_player_milliseconds = getattr(
                        child_player,
                        "elapsed_time_milliseconds",
                        child_player.elapsed_time * 1000,
                    )

                    if child_player_id != master_player.player_id:
                        drift = child_player_milliseconds - master_player_milliseconds
                        if drift > 250:
                            LOGGER.debug(
                                "child player %s is drifting ahead with %s milliseconds",
                                child_player_id,
                                drift,
                            )
                            start_bytes = int(drift / 1000 * chunk_size)
                        lag = master_player_milliseconds - child_player_milliseconds
                        if lag > 250:
                            LOGGER.debug(
                                "child player %s is lagging behind with %s milliseconds",
                                child_player_id,
                                lag,
                            )
                            await http_client.write(
                                b"\0" * int(lag / 1000 * chunk_size)
                            )
                else:
                    check_drift_counter += 1

                try:
                    await http_client.write(audio_chunk[start_bytes:])
                except (BrokenPipeError, ConnectionResetError):
                    pass

                if not self.connected_clients:
                    LOGGER.warning("no more clients!")
                    return
