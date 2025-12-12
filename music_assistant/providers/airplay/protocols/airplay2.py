"""Logic for AirPlay 2 audio streaming to AirPlay devices."""

from __future__ import annotations

import asyncio
import logging

from music_assistant_models.enums import PlaybackState
from music_assistant_models.errors import PlayerCommandFailed

from music_assistant.constants import CONF_SYNC_ADJUST, VERBOSE_LOG_LEVEL
from music_assistant.helpers.process import AsyncProcess
from music_assistant.providers.airplay.constants import (
    AIRPLAY2_MIN_LOG_LEVEL,
)
from music_assistant.providers.airplay.helpers import get_cli_binary

from ._protocol import AirPlayProtocol


class AirPlay2Stream(AirPlayProtocol):
    """
    AirPlay 2 Audio Streamer.

    Python is not suitable for realtime audio streaming so we do the actual streaming
    of audio using a small executable written in C based on owntones to do
    the actual timestamped playback. It reads pcm audio from a named pipe
    and we can send some interactive commands using another named pipe.
    """

    @property
    def _cli_loglevel(self) -> int:
        """
        Return a cliap2 aligned loglevel.

        Ensures that minimum level required for required cliap2 stderr output is respected.
        """
        mass_level: int = 0
        match self.prov.logger.level:
            case logging.CRITICAL:
                mass_level = 0
            case logging.ERROR:
                mass_level = 1
            case logging.WARNING:
                mass_level = 2
            case logging.INFO:
                mass_level = 3
            case logging.DEBUG:
                mass_level = 4
        if self.prov.logger.isEnabledFor(VERBOSE_LOG_LEVEL):
            mass_level = 5
        return max(mass_level, AIRPLAY2_MIN_LOG_LEVEL)

    async def start(self, start_ntp: int, skip: int = 0) -> None:
        """Initialize CLI process for a player."""
        cli_binary = await get_cli_binary(self.player.protocol)
        assert self.player.airplay_discovery_info is not None

        player_id = self.player.player_id
        sync_adjust = self.mass.config.get_raw_player_config_value(player_id, CONF_SYNC_ADJUST, 0)
        assert isinstance(sync_adjust, int)

        txt_kv: str = ""
        for key, value in self.player.airplay_discovery_info.decoded_properties.items():
            txt_kv += f'"{key}={value}" '

        # Note: skip parameter is accepted for API compatibility with base class
        # but is not currently used by the cliap2 binary (AirPlay2 handles late joiners differently)

        # cliap2 is the binary that handles the actual streaming to the player
        # this binary leverages from the AirPlay2 support in owntones
        # https://github.com/music-assistant/cliairplay

        cli_args = [
            cli_binary,
            "--name",
            self.player.display_name,
            "--hostname",
            str(self.player.airplay_discovery_info.server),
            "--address",
            str(self.player.address),
            "--port",
            str(self.player.airplay_discovery_info.port),
            "--txt",
            txt_kv,
            "--ntpstart",
            str(start_ntp),
            "--volume",
            str(self.player.volume_level),
            "--loglevel",
            str(self._cli_loglevel),
            "--pipe",
            self.audio_pipe.path,
            "--command_pipe",
            self.commands_pipe.path,
        ]

        self.player.logger.debug(
            "Starting cliap2 process for player %s with args: %s",
            player_id,
            cli_args,
        )
        self._cli_proc = AsyncProcess(cli_args, stdin=False, stderr=True, name="cliap2")
        await self._cli_proc.start()
        # read up to first num_lines lines of stderr to get the initial status
        num_lines: int = 50
        if self.prov.logger.level > logging.INFO:
            num_lines *= 10
        for _ in range(num_lines):
            line = (await self._cli_proc.read_stderr()).decode("utf-8", errors="ignore").strip()
            self.player.logger.debug(line)
            if f"airplay: Adding AirPlay device '{self.player.display_name}'" in line:
                self.player.logger.info("AirPlay device connected. Starting playback.")
                break
            if f"The AirPlay 2 device '{self.player.display_name}' failed" in line:
                raise PlayerCommandFailed("Cannot connect to AirPlay device")
        # start reading the stderr of the cliap2 process from another task
        self._cli_proc.attach_stderr_reader(self.mass.create_task(self._stderr_reader()))

    async def _stderr_reader(self) -> None:
        """Monitor stderr for the running CLIap2 process."""
        player = self.player
        logger = player.logger
        if not self._cli_proc:
            return
        async for line in self._cli_proc.iter_stderr():
            if "Pause at" in line:
                player.set_state_from_stream(state=PlaybackState.PAUSED)
            if "Restarted at" in line:
                player.set_state_from_stream(state=PlaybackState.PLAYING)
            if "Starting at" in line:
                # streaming has started
                player.set_state_from_stream(state=PlaybackState.PLAYING, elapsed_time=0)
            if "put delay detected" in line:
                if "resetting all outputs" in line:
                    logger.error("High packet loss detected, restarting playback...")
                    self.mass.create_task(self.mass.players.cmd_resume(self.player.player_id))
                else:
                    logger.warning("Packet loss detected!")
            if "end of stream reached" in line:
                logger.debug("End of stream reached")
                break

            # log cli stderr output in alignment with mass logging level
            if "[FATAL]" in line:
                logger.critical(line)
            elif "[  LOG]" in line:
                logger.error(line)
            elif "[ INFO]" in line:
                logger.info(line)
            elif "[ WARN]" in line:
                logger.warning(line)
            elif "[DEBUG]" in line:
                logger.debug(line)
            elif "[ SPAM]" in line:
                logger.log(VERBOSE_LOG_LEVEL, line)
            else:  # for now, log unknown lines as error
                logger.error(line)
            await asyncio.sleep(0)  # Yield to event loop

        # ensure we're cleaned up afterwards (this also logs the returncode)
        if not self._stopped:
            self._stopped = True
            self.player.set_state_from_stream(state=PlaybackState.IDLE, elapsed_time=0)
