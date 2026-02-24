"""Logic for AirPlay 2 audio streaming to AirPlay devices."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, cast

from music_assistant_models.enums import PlaybackState

from music_assistant.constants import CONF_SYNC_ADJUST, VERBOSE_LOG_LEVEL
from music_assistant.helpers.process import AsyncProcess
from music_assistant.providers.airplay.constants import (
    AIRPLAY2_MIN_LOG_LEVEL,
    AIRPLAY_OUTPUT_BUFFER_MIN_DURATION_MS,
    CONF_AIRPLAY_CREDENTIALS,
    CONF_AIRPLAY_LATENCY,
)
from music_assistant.providers.airplay.helpers import get_cli_binary

from ._protocol import AirPlayProtocol

if TYPE_CHECKING:
    from music_assistant.providers.airplay.provider import AirPlayProvider


class AirPlay2Stream(AirPlayProtocol):
    """
    AirPlay 2 Audio Streamer.

    Uses cliap2 (C executable based on owntone) for timestamped playback.
    Audio is fed via stdin, commands via a named pipe.
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

    async def start(self, start_ntp: int) -> None:
        """Start cliap2 process."""
        assert self.player.airplay_discovery_info is not None
        cli_binary = await get_cli_binary(self.player.protocol)
        player_id = self.player.player_id
        sync_adjust = self.player.config.get_value(CONF_SYNC_ADJUST)
        assert isinstance(sync_adjust, int)
        latency = self.player.config.get_value(
            CONF_AIRPLAY_LATENCY, AIRPLAY_OUTPUT_BUFFER_MIN_DURATION_MS
        )
        assert isinstance(latency, int)

        txt_kv: str = ""
        for key, value in self.player.airplay_discovery_info.decoded_properties.items():
            txt_kv += f'"{key}={value}" '

        # cliap2 is the binary that handles the actual streaming to the player
        # this binary leverages from the AirPlay2 support in OwnTone
        # https://github.com/music-assistant/cliairplay

        # Get AirPlay credentials if available (for Apple devices that require pairing)
        airplay_credentials: str | None = None
        if creds := self.player.config.get_value(CONF_AIRPLAY_CREDENTIALS):
            airplay_credentials = str(creds)

        # Get the provider's DACP ID for remote control callbacks
        prov = cast("AirPlayProvider", self.prov)

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
            "--dacp_id",
            prov.dacp_id,
            "--pipe",
            "-",  # Use stdin for audio input
            "--command_pipe",
            self.commands_pipe.path,
            "--latency",
            str(latency),
        ]

        # Add credentials for authenticated AirPlay devices (Apple TV, HomePod, etc.)
        # Native HAP pairing format: 192 hex chars = client_private_key(128) + server_public_key(64)
        if airplay_credentials:
            if len(airplay_credentials) == 192:
                cli_args += ["--auth", airplay_credentials]
            else:
                self.player.logger.warning(
                    "Invalid credentials length: %d (expected 192)",
                    len(airplay_credentials),
                )

        self.player.logger.debug(
            "Starting cliap2 process for player %s with args: %s",
            player_id,
            cli_args,
        )
        self._cli_proc = AsyncProcess(cli_args, stdin=True, stderr=True, name="cliap2")
        await self._cli_proc.start()
        # start reading the stderr of the cliap2 process from another task
        self._cli_proc.attach_stderr_reader(self.mass.create_task(self._stderr_reader()))

    async def _stderr_reader(self) -> None:
        """Monitor stderr for the running CLIap2 process."""
        player = self.player
        logger = player.logger
        if not self._cli_proc:
            return
        async for line in self._cli_proc.iter_stderr():
            if self._stopped:
                break
            if "player: event_play_start()" in line:
                # successfully connected
                self._connected.set()
            if "Pause at" in line:
                player.set_state_from_stream(state=PlaybackState.PAUSED, stream=self)
            elif "Restarted at" in line:
                player.set_state_from_stream(state=PlaybackState.PLAYING, stream=self)
            elif "Starting at" in line:
                # streaming has started
                player.set_state_from_stream(
                    state=PlaybackState.PLAYING, elapsed_time=0, stream=self
                )
            if "put delay detected" in line:
                if "resetting all outputs" in line:
                    logger.error(
                        "Repeated output buffer low level detected, restarting playback..."
                    )
                    logger.info(
                        "Recommended to increase 'Milliseconds of data to buffer' in player "
                        "advanced settings"
                    )
                    self.mass.create_task(self.mass.players.cmd_resume(self.player.player_id))
                else:
                    logger.warning("Output buffer low level detected!")
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
            elif "[DEBUG]" in line and "mass_timer_cb" in line:
                # mass_timer_cb is very spammy, reduce it to verbose
                logger.log(VERBOSE_LOG_LEVEL, line)
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
            self.player.set_state_from_stream(state=PlaybackState.IDLE, elapsed_time=0, stream=self)
