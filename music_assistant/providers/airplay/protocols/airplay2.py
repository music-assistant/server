"""Logic for AirPlay 2 audio streaming to AirPlay devices."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, cast

from music_assistant_models.enums import PlaybackState

from music_assistant.constants import VERBOSE_LOG_LEVEL
from music_assistant.helpers.process import AsyncProcess
from music_assistant.providers.airplay.constants import (
    AIRPLAY2_MIN_LOG_LEVEL,
    AIRPLAY_SESSION_ESTABLISHMENT_LATENCY_MAX_MS,
    AIRPLAY_SESSION_ESTABLISHMENT_LATENCY_MIN_MS,
    CONF_AIRPLAY_CREDENTIALS,
    CONF_AP2PASSWORD,
)
from music_assistant.providers.airplay.helpers import get_cli_binary

from ._protocol import AirPlayProtocol

if TYPE_CHECKING:
    from music_assistant.providers.airplay.provider import AirPlayProvider


class AirPlay2Stream(AirPlayProtocol):
    """
    AirPlay 2 Audio Streamer.

    Uses cliap2 (C executable based on OwnTone) for timestamped playback.
    Audio is fed via stdin, commands via a named pipe.
    """

    async def start(self, start_ntp: int) -> None:
        """Start cliap2 process."""
        assert self.player.airplay_discovery_info is not None  # for type checker
        cli_binary = await get_cli_binary(self.player.protocol)
        player_id = self.player.player_id

        txt_kv: str = ""
        for key, value in self.player.airplay_discovery_info.decoded_properties.items():
            txt_kv += f'"{key}={value}" '

        # cliap2 is the binary that handles the actual streaming to the player
        # this binary leverages from the AirPlay2 support in OwnTone
        # https://github.com/music-assistant/cliairplay

        # Get AirPlay credentials if available (for Apple devices that require pairing)
        airplay_credentials: str | None = None
        airplay_password: str | None = None
        if creds := self.player.config.get_value(CONF_AIRPLAY_CREDENTIALS):
            airplay_credentials = str(creds)
            if ap2_password := self.player.config.get_value(CONF_AP2PASSWORD):
                airplay_password = str(ap2_password)

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
            str(self.player.output_buffer_duration_ms),
            "--session_establishment_latency",
            str(self.player.session_establishment_latency_ms),
        ]

        # Add credentials for authenticated AirPlay devices (Apple TV, HomePod, etc.)
        # Native HAP pairing format: 192 hex chars = client_private_key(128) + server_public_key(64)
        if airplay_credentials:
            if len(airplay_credentials) == 192:
                cli_args += ["--auth", airplay_credentials]
                if airplay_password:
                    cli_args += ["--password", airplay_password]
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

    @property
    def _session_establishment_ms(self) -> int | None:
        """
        Return the actual number of milliseconds taken to establish a streaming session with the device.

        Will return None if session establishment has not yet happened.
        """
        if self._cli_start_ts is None:
            return None
        if self._connected_ts is None:
            return None
        return int((self._connected_ts - self._cli_start_ts) * 1000)

    @property
    def _recommended_session_establishment_latency(self) -> tuple[int, int] | None:
        """
        Return the suggested value window for session establishment latency configuration.

        Will return None if a recommended window cannot be determined.
        Empirical testing has found that a value in the range of no less than 700ms
        less than and no more than 100ms more than the actual session establishment time
        provides a reliable window. The exact reasoning for this is not yet understood and
        it seems counter intuitive to have a value less than the actual. A more robust
        solution for ensuring synchronised playback needs to be implemented into the AirPlay2
        binary and once done, there will be no need for the complexity of a session establishment
        latency concept to exist in MA.
        """
        if self._session_establishment_ms is None:
            return None
        high = min(
            ((self._session_establishment_ms + 99) // 100) * 100,
            AIRPLAY_SESSION_ESTABLISHMENT_LATENCY_MAX_MS,
        )
        # 600 used to provide protection against hitting the 700ms boundary mentioned in docstring above.
        low = max(high - 600, AIRPLAY_SESSION_ESTABLISHMENT_LATENCY_MIN_MS)
        return (low, high)

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

    def _session_established(self) -> None:
        """
        Streaming session is now established.

        Log the actual session establishment duration and check against configured value.
        """
        if self._connected_ts is not None:
            """We have previously connected, so we ignore this call"""
            return
        self._connected_ts = time.time()
        assert self._session_establishment_ms is not None  # for type checker
        self.logger.debug(
            f"Streaming session established in {self._session_establishment_ms}ms for player {self.player.name}"
        )
        window = self._recommended_session_establishment_latency
        if window is None:  # guard
            return
        low, high = window
        if (self.player.session_establishment_latency_ms < low) or (
            self.player.session_establishment_latency_ms > high
        ):
            self.logger.warning(
                f"Configured value of {self.player.session_establishment_latency_ms}ms for session establishment latency "
                f"is outside the recommended range for player {self.player.name}. The recommended range is between "
                f"{low} and {high} ms. Please adjust this advanced configuration value accordingly."
            )

    @staticmethod
    def _log_cli_output(logger: logging.Logger, line: str) -> None:
        """
        Route a cliap2 stderr line to the appropriate log level.

        Strips the leading timestamp and log level info to eliminate duplication
        of MA logging output and improve readability.
        """
        if "[FATAL]" in line:
            logger.critical(line[31:])
        elif "[  LOG]" in line:
            logger.error(line[31:])
        elif "[ INFO]" in line:
            logger.info(line[31:])
        elif "[ WARN]" in line:
            logger.warning(line[31:])
        elif "[DEBUG]" in line and "player: Player status: playing" in line:
            logger.log(VERBOSE_LOG_LEVEL, line[31:])
        elif "[DEBUG]" in line:
            logger.debug(line[31:])
        elif "[ SPAM]" in line:
            logger.log(VERBOSE_LOG_LEVEL, line[31:])
        else:
            logger.error(line)

    async def _stderr_reader(self) -> None:
        """Monitor stderr for the running CLIap2 process."""
        player = self.player
        logger = player.logger
        expected_eof = False
        if not self._cli_proc:
            return
        async for line in self._cli_proc.iter_stderr():
            if self._stopped:
                break
            if "main: DACP ID set to:" in line:
                self._cli_start_ts = time.time()
            elif (
                f"player: Callback from AirPlay 2 device {self.player.display_name} to device_activate_cb (status 2)"
                in line
            ):
                # successfully connected
                self._connected.set()
                self._session_established()
            elif "Pause at" in line:
                player.set_state_from_stream(state=PlaybackState.PAUSED, stream=self)
            elif "Restarted at" in line:
                player.set_state_from_stream(state=PlaybackState.PLAYING, stream=self)
            elif "Starting at" in line:
                # streaming has started - compute a fixed offset between the
                # session start_time and this process start to handle dynamic
                # leader switching where a new process starts mid-session.
                if self._elapsed_time_offset is None and self.session:
                    self._elapsed_time_offset = max(0, time.time() - self.session.start_time)
                elapsed_time = self._elapsed_time_offset or 0
                player.set_state_from_stream(
                    state=PlaybackState.PLAYING, elapsed_time=elapsed_time, stream=self
                )
            elif "put delay detected" in line:
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
            elif "end of stream reached" in line:
                logger.debug("End of stream reached")
                expected_eof = True
                break

            self._log_cli_output(logger, line)
            await asyncio.sleep(0)  # Yield to event loop

        # ensure we're cleaned up afterwards (this also logs the returncode)
        if not self._stopped:
            self._stopped = True
            if not expected_eof:
                # CLI process died without reaching EOF — unsolicited stop.
                logger.warning("CLIap2 process stopped unexpectedly for %s", player.display_name)
                # Hand off to the player controller so it drops just this member, or
                # transfers leadership to a healthy member, instead of dissolving the
                # whole group over a single dead transport. A sync leader is left in
                # its current state here on purpose: the controller only transfers
                # leadership while the queue still looks active, and transfer_queue or
                # dissolve sets the final state.
                self.mass.create_task(self.mass.players.cmd_ungroup(player.player_id))
                if player.group_members:
                    return
            player.set_state_from_stream(state=PlaybackState.IDLE, elapsed_time=0, stream=self)
