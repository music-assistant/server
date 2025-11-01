"""Logic for AirPlay 2 audio streaming to AirPlay devices."""

from __future__ import annotations

import asyncio
import logging
import os
import platform

from music_assistant_models.enums import PlaybackState
from music_assistant_models.errors import PlayerCommandFailed

from music_assistant.constants import CONF_SYNC_ADJUST, VERBOSE_LOG_LEVEL
from music_assistant.helpers.process import AsyncProcess
from music_assistant.providers.airplay.constants import (
    AIRPLAY2_MIN_LOG_LEVEL,
    CONF_READ_AHEAD_BUFFER,
)
from music_assistant.providers.airplay.helpers import get_cli_binary, get_ntp_timestamp

from ._protocol import AirPlayProtocol


class AirPlay2Stream(AirPlayProtocol):
    """
    AirPlay 2 Audio Streamer.

    Python is not suitable for realtime audio streaming so we do the actual streaming
    of audio using a small executable written in C based on owntones to do
    the actual timestamped playback. It reads pcm audio from a named pipe
    and we can send some interactive commands using another named pipe.
    """

    _stderr_reader_task: asyncio.Task[None] | None = None

    async def get_ntp(self) -> int:
        """Get current NTP timestamp."""
        # this can probably be removed now that we already get the ntp
        # in python (within the stream session start)
        return get_ntp_timestamp()

    @property
    def _cli_loglevel(self) -> int:
        """
        Return a cliap2 aligned loglevel.

        Ensures that minimum level required for required cliap2 stderr output is respected.
        """
        force_verbose: bool = False  # just for now
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
        if force_verbose:
            mass_level = 5  # always use max log level for now to capture all stderr output
        return max(mass_level, AIRPLAY2_MIN_LOG_LEVEL)

    async def start(self, start_ntp: int, skip: int = 0) -> None:
        """Initialize CLI process for a player."""
        cli_binary = await get_cli_binary(self.player.protocol)
        assert self.player.discovery_info is not None

        player_id = self.player.player_id
        sync_adjust = self.mass.config.get_raw_player_config_value(player_id, CONF_SYNC_ADJUST, 0)
        assert isinstance(sync_adjust, int)
        read_ahead = await self.mass.config.get_player_config_value(
            player_id, CONF_READ_AHEAD_BUFFER
        )

        txt_kv: str = ""
        for key, value in self.player.discovery_info.decoded_properties.items():
            txt_kv += f'"{key}={value}" '

        # Note: skip parameter is accepted for API compatibility with base class
        # but is not currently used by the cliap2 binary (AirPlay2 handles late joiners differently)

        # cliap2 is the binary that handles the actual streaming to the player
        # this binary leverages from the AirPlay2 support in owntones
        # https://github.com/music-assistant/cliairplay
        cli_args = [
            cli_binary,
            "--config",
            os.path.join(os.path.dirname(__file__), "bin", "cliap2.conf"),
            "--name",
            self.player.display_name,
            "--hostname",
            str(self.player.discovery_info.server),
            "--address",
            str(self.player.address),
            "--port",
            str(self.player.discovery_info.port),
            "--txt",
            txt_kv,
            "--ntpstart",
            str(start_ntp),
            "--latency",
            str(read_ahead),
            "--volume",
            str(self.player.volume_level),
            "--loglevel",
            str(self._cli_loglevel),
            "--pipe",
            self.audio_named_pipe,
        ]
        self.player.logger.debug(
            "Starting cliap2 process for player %s with args: %s",
            player_id,
            cli_args,
        )
        self._cli_proc = AsyncProcess(cli_args, stdin=True, stderr=True, name="cliap2")
        if platform.system() == "Darwin":
            os.environ["DYLD_LIBRARY_PATH"] = "/usr/local/lib"
        await self._cli_proc.start()
        # read up to first num_lines lines of stderr to get the initial status
        num_lines: int = 50
        if self.prov.logger.level > logging.INFO:
            num_lines *= 10
        for _ in range(num_lines):
            line = (await self._cli_proc.read_stderr()).decode("utf-8", errors="ignore")
            self.player.logger.debug(line)
            if f"airplay: Adding AirPlay device '{self.player.display_name}'" in line:
                self.player.logger.info("AirPlay device connected. Starting playback.")
                self._started.set()
                # Open pipes now that cliraop is ready
                await self._open_pipes()
                break
            if f"The AirPlay 2 device '{self.player.display_name}' failed" in line:
                raise PlayerCommandFailed("Cannot connect to AirPlay device")
        # start reading the stderr of the cliap2 process from another task
        self._stderr_reader_task = self.mass.create_task(self._stderr_reader())

    async def _stderr_reader(self) -> None:
        """Monitor stderr for the running CLIap2 process."""
        player = self.player
        queue = self.mass.players.get_active_queue(player)
        logger = player.logger
        lost_packets = 0
        if not self._cli_proc:
            return
        async for line in self._cli_proc.iter_stderr():
            # TODO @bradkeifer make cliap2 work this way
            if "elapsed milliseconds:" in line:
                # this is received more or less every second while playing
                # millis = int(line.split("elapsed milliseconds: ")[1])
                # self.player.elapsed_time = (millis / 1000) - self.elapsed_time_correction
                # self.player.elapsed_time_last_updated = time.time()
                # NOTE: Metadata is now handled at the session level
                pass
            if "set pause" in line or "Pause at" in line:
                player.set_state_from_stream(state=PlaybackState.PAUSED)
            if "Restarted at" in line or "restarting w/ pause" in line:
                player.set_state_from_stream(state=PlaybackState.PLAYING)
            if "restarting w/o pause" in line:
                # streaming has started
                player.set_state_from_stream(state=PlaybackState.PLAYING, elapsed_time=0)
            if "lost packet out of backlog" in line:
                lost_packets += 1
                if lost_packets == 100 and queue:
                    logger.error("High packet loss detected, restarting playback...")
                    self.mass.create_task(self.mass.player_queues.resume(queue.queue_id, False))
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

        # ensure we're cleaned up afterwards (this also logs the returncode)
        await self.stop()
