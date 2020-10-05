"""Local player provider."""
import asyncio
import logging
import signal
import subprocess
from typing import List

from music_assistant.models.config_entry import ConfigEntry
from music_assistant.models.player import DeviceInfo, PlaybackState, Player
from music_assistant.models.provider import PlayerProvider

PROV_ID = "builtin_player"
PROV_NAME = "Built-in (local) player"
LOGGER = logging.getLogger(PROV_ID)


async def async_setup(mass):
    """Perform async setup of this Plugin/Provider."""
    prov = BuiltinPlayerProvider()
    await mass.async_register_provider(prov)


class BuiltinPlayerProvider(PlayerProvider):
    """Demo PlayerProvider which provides a single local player."""

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
        player = BuiltinPlayer("local_player", "Built-in player on the server")
        self.mass.add_job(self.mass.players.async_add_player(player))
        return True

    async def async_on_stop(self):
        """Handle correct close/cleanup of the provider on exit."""
        for player in self.players:
            await player.async_cmd_stop()


class BuiltinPlayer(Player):
    """Representation of a BuiltinPlayer."""

    def __init__(self, player_id: str, name: str) -> None:
        """Initialize the built-in player."""
        self._player_id = player_id
        self._name = name
        self._powered = False
        self._elapsed_time = 0
        self._state = PlaybackState.Stopped
        self._current_uri = ""
        self._volume_level = 100
        self._muted = False
        self._sox = None
        self._progress_task = None

    @property
    def player_id(self) -> str:
        """Return player id of this player."""
        return self._player_id

    @property
    def provider_id(self) -> str:
        """Return provider id of this player."""
        return PROV_ID

    @property
    def name(self) -> str:
        """Return name of the player."""
        return self._name

    @property
    def powered(self) -> bool:
        """Return current power state of player."""
        return self._powered

    @property
    def elapsed_time(self) -> float:
        """Return elapsed_time of current playing uri in seconds."""
        return self._elapsed_time

    @property
    def state(self) -> PlaybackState:
        """Return current PlaybackState of player."""
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
    def is_group_player(self) -> bool:
        """Return True if this player is a group player."""
        return False

    @property
    def device_info(self) -> DeviceInfo:
        """Return the device info for this player."""
        return DeviceInfo(
            model="Demo", address="http://demo:12345", manufacturer=PROV_NAME
        )

    # SERVICE CALLS / PLAYER COMMANDS

    async def async_cmd_play_uri(self, uri: str):
        """Play the specified uri/url on the player."""
        if self._sox:
            await self.async_cmd_stop()
        self._current_uri = uri
        self._sox = subprocess.Popen(["play", "-t", "flac", "-q", uri])
        self._state = PlaybackState.Playing
        self._powered = True
        self.update_state()

        async def report_progress():
            """Report fake progress while sox is playing."""
            LOGGER.info("Playback started on player %s", self.name)
            self._elapsed_time = 0
            while self._sox and not self._sox.poll():
                await asyncio.sleep(1)
                self._elapsed_time += 1
                self.update_state()
            LOGGER.info("Playback stopped on player %s", self.name)
            self._elapsed_time = 0
            self._state = PlaybackState.Stopped
            self.update_state()

        if self._progress_task:
            self._progress_task.cancel()
        self._progress_task = self.mass.add_job(report_progress)

    async def async_cmd_stop(self) -> None:
        """Send STOP command to player."""
        if self._sox:
            self._sox.terminate()
            self._sox = None
        self._state = PlaybackState.Stopped
        self.update_state()

    async def async_cmd_play(self) -> None:
        """Send PLAY command to player."""
        if self._sox:
            self._sox.send_signal(signal.SIGCONT)
            self._state = PlaybackState.Playing
            self.update_state()

    async def async_cmd_pause(self):
        """Send PAUSE command to given player."""
        if self._sox:
            self._sox.send_signal(signal.SIGSTOP)
        self._state = PlaybackState.Paused
        self.update_state()

    async def async_cmd_power_on(self) -> None:
        """Send POWER ON command to player."""
        self._powered = True
        self.update_state()

    async def async_cmd_power_off(self) -> None:
        """Send POWER OFF command to player."""
        await self.async_cmd_stop()
        self._powered = False
        self.update_state()

    async def async_cmd_volume_set(self, volume_level: int) -> None:
        """
        Send volume level command to given player.

            :param volume_level: volume level to set (0..100).
        """
        self._volume_level = volume_level
        self.update_state()

    async def async_cmd_volume_mute(self, is_muted=False):
        """
        Send volume MUTE command to given player.

            :param is_muted: bool with new mute state.
        """
        self._muted = is_muted
        self.update_state()
