"""Demo/test providers."""
import asyncio
import logging
import signal
import subprocess
from typing import List

from music_assistant.models.config_entry import ConfigEntry
from music_assistant.models.player import DeviceInfo, Player, PlayerState
from music_assistant.models.playerprovider import PlayerProvider

PROV_ID = "demo_player"
PROV_NAME = "Demo/Test players"
LOGGER = logging.getLogger(PROV_ID)


class DemoPlayerProvider(PlayerProvider):
    """Demo PlayerProvider which provides fake players."""

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
        return []

    async def async_on_start(self) -> bool:
        """Handle initialization of the provider based on config."""
        # create fake/test regular player 1
        player = Player(
            player_id="demo_player_1",
            provider_id=PROV_ID,
            name="Demo player 1",
            device_info=DeviceInfo(
                model="Demo/Test Player",
                address="http://demo_player1:12345",
                manufacturer=PROV_ID,
            ),
        )
        player.sox = None
        self._players[player.player_id] = player
        self.mass.add_job(self.mass.player_manager.async_add_player(player))
        # create fake/test regular player 2
        player = Player(
            player_id="demo_player_2",
            provider_id=PROV_ID,
            name="Demo player 2",
            device_info=DeviceInfo(
                model="Demo/Test Player",
                address="http://demo_player2:12345",
                manufacturer=PROV_ID,
            ),
        )
        player.sox = None
        self._players[player.player_id] = player
        self.mass.add_job(self.mass.player_manager.async_add_player(player))
        # create fake/test group player
        group_player = Player(
            player_id="demo_group_player",
            is_group_player=True,
            group_childs=["demo_player_1", "demo_player_2"],
            provider_id=PROV_ID,
            name="Demo Group Player",
            device_info=DeviceInfo(
                model="Demo/Test Group player",
                address="http://demo_group_player:12345",
                manufacturer=PROV_ID,
            ),
        )
        group_player.sox = None
        self._players[group_player.player_id] = group_player
        self.mass.add_job(self.mass.player_manager.async_add_player(group_player))

        return True

    async def async_on_stop(self):
        """Handle correct close/cleanup of the provider on exit."""
        for player in self._players.values():
            if player.sox:
                player.sox.terminate()

    # SERVICE CALLS / PLAYER COMMANDS

    async def async_cmd_play_uri(self, player_id: str, uri: str):
        """
        Play the specified uri/url on the given player.

            :param player_id: player_id of the player to handle the command.
        """
        player = self._players[player_id]
        if player.sox:
            await self.async_cmd_stop(player_id)
        player.current_uri = uri
        player.sox = subprocess.Popen(["play", "-q", uri])
        player.state = PlayerState.Playing
        player.powered = True
        self.mass.add_job(self.mass.player_manager.async_update_player(player))

        async def report_progress():
            """Report fake progress while sox is playing."""
            LOGGER.info("Playback started on player %s", player_id)
            player.elapsed_time = 0
            while player.sox and not player.sox.poll():
                await asyncio.sleep(1)
                player.elapsed_time += 1
                self.mass.add_job(self.mass.player_manager.async_update_player(player))
            LOGGER.info("Playback stopped on player %s", player_id)
            player.elapsed_time = 0
            player.state = PlayerState.Stopped
            self.mass.add_job(self.mass.player_manager.async_update_player(player))

        if self._progress_tasks.get(player_id):
            self._progress_tasks[player_id].cancel()
        self._progress_tasks[player_id] = self.mass.add_job(report_progress)

    async def async_cmd_stop(self, player_id: str) -> None:
        """
        Send STOP command to given player.

            :param player_id: player_id of the player to handle the command.
        """
        player = self._players[player_id]
        if player.sox:
            player.sox.terminate()
            player.sox = None
        player.state = PlayerState.Stopped
        self.mass.add_job(self.mass.player_manager.async_update_player(player))

    async def async_cmd_play(self, player_id: str) -> None:
        """
        Send PLAY command to given player.

            :param player_id: player_id of the player to handle the command.
        """
        player = self._players[player_id]
        if player.sox:
            player.sox.send_signal(signal.SIGCONT)
            player.state = PlayerState.Playing
            self.mass.add_job(self.mass.player_manager.async_update_player(player))

    async def async_cmd_pause(self, player_id: str):
        """
        Send PAUSE command to given player.

            :param player_id: player_id of the player to handle the command.
        """
        player = self._players[player_id]
        if player.sox:
            player.sox.send_signal(signal.SIGSTOP)
        player.state = PlayerState.Paused
        self.mass.add_job(self.mass.player_manager.async_update_player(player))

    async def async_cmd_next(self, player_id: str):
        """
        Send NEXT TRACK command to given player.

            :param player_id: player_id of the player to handle the command.
        """
        # this code should never be reached as the player doesn't report queue support
        # throw NotImplementedError just in case we've missed a spot
        raise NotImplementedError

    async def async_cmd_previous(self, player_id: str):
        """
        Send PREVIOUS TRACK command to given player.

            :param player_id: player_id of the player to handle the command.
        """
        # this code should never be reached as the player doesn't report queue support
        # throw NotImplementedError just in case we've missed a spot
        raise NotImplementedError

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
