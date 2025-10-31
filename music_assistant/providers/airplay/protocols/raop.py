"""Logic for RAOP audio streaming to AirPlay devices."""

from __future__ import annotations

import asyncio
import logging

from music_assistant_models.enums import PlaybackState
from music_assistant_models.errors import PlayerCommandFailed

from music_assistant.constants import CONF_SYNC_ADJUST, VERBOSE_LOG_LEVEL
from music_assistant.helpers.process import AsyncProcess, check_output
from music_assistant.providers.airplay.constants import (
    CONF_ALAC_ENCODE,
    CONF_ENCRYPTION,
    CONF_PASSWORD,
    CONF_READ_AHEAD_BUFFER,
)
from music_assistant.providers.airplay.helpers import get_cli_binary

from ._protocol import AirPlayProtocol


class RaopStream(AirPlayProtocol):
    """
    RAOP (AirPlay 1) Audio Streamer.

    Python is not suitable for realtime audio streaming so we do the actual streaming
    of (RAOP) audio using a small executable written in C based on libraop to do
    the actual timestamped playback, which reads pcm audio from stdin
    and we can send some interactive commands using a named pipe.
    """

    _stderr_reader_task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        """Return boolean if this stream is running."""
        return (
            not self._stopped
            and self._started.is_set()
            and self._cli_proc is not None
            and not self._cli_proc.closed
        )

    async def get_ntp(self) -> int:
        """Get current NTP timestamp from the CLI binary."""
        cli_binary = await get_cli_binary(self.player.protocol)
        # TODO: we can potentially also just generate this ourselves?
        self.prov.logger.debug("Getting NTP timestamp from %s CLI binary", self.player.protocol)
        _, stdout = await check_output(cli_binary, "-ntp")
        self.prov.logger.debug(f"Output from ntp check: {stdout.decode().strip()}")
        return int(stdout.strip())

    async def start(self, start_ntp: int, skip: int = 0) -> None:
        """Initialize CLIRaop process for a player."""
        assert self.player.discovery_info is not None  # for type checker
        cli_binary = await get_cli_binary(self.player.protocol)

        extra_args: list[str] = []
        player_id = self.player.player_id
        extra_args += ["-if", self.mass.streams.bind_ip]
        if self.player.config.get_value(CONF_ENCRYPTION, True):
            extra_args += ["-encrypt"]
        if self.player.config.get_value(CONF_ALAC_ENCODE, True):
            extra_args += ["-alac"]
        for prop in ("et", "md", "am", "pk", "pw"):
            if prop_value := self.player.discovery_info.decoded_properties.get(prop):
                extra_args += [f"-{prop}", prop_value]
        if skip > 0:
            extra_args += ["-skip", str(skip)]
        sync_adjust = self.player.config.get_value(CONF_SYNC_ADJUST, 0)
        assert isinstance(sync_adjust, int)
        if device_password := self.mass.config.get_raw_player_config_value(
            player_id, CONF_PASSWORD, None
        ):
            extra_args += ["-password", str(device_password)]
        # Add AirPlay credentials from pyatv pairing if available (for Apple devices)
        # if raop_credentials := self.player.config.get_value(CONF_AP_CREDENTIALS):
        #     # pyatv AirPlay credentials are in format "identifier:secret_key:other:data"
        #     # cliraop expects just the secret_key (2nd part, 64-char hex string) for -secret
        #     parts = str(raop_credentials).split(":")
        #     if len(parts) >= 2:
        #         # Take the second part (index 1) as the secret key
        #         secret_key = parts[1]
        #         self.prov.logger.debug(
        #             "Using AirPlay credentials for %s: id=%s, secret_len=%d, parts=%d",
        #             self.player.player_id,
        #             parts[0],
        #             len(secret_key),
        #             len(parts),
        #         )
        #         extra_args += ["-secret", secret_key]
        #     else:
        #         # Fallback: assume it's already just the key
        #         self.prov.logger.debug(
        #             "Using AirPlay credentials for %s: single value, length=%d",
        #             self.player.player_id,
        #             len(str(raop_credentials)),
        #         )
        #         extra_args += ["-secret", str(raop_credentials)]
        if self.prov.logger.isEnabledFor(logging.DEBUG):
            extra_args += ["-debug", "5"]
        elif self.prov.logger.isEnabledFor(VERBOSE_LOG_LEVEL):
            extra_args += ["-debug", "10"]
        read_ahead = await self.mass.config.get_player_config_value(
            player_id, CONF_READ_AHEAD_BUFFER
        )
        self.player.logger.info("Starting cliraop with latency buffer: %dms", read_ahead)

        # cliraop is the binary that handles the actual raop streaming to the player
        # this is a slightly modified version of philippe44's libraop
        # https://github.com/music-assistant/libraop
        # we use this intermediate binary to do the actual streaming because attempts to do
        # so using pure python (e.g. pyatv) were not successful due to the realtime nature
        cliraop_args = [
            cli_binary,
            "-ntpstart",
            str(start_ntp),
            "-port",
            str(self.player.discovery_info.port),
            "-latency",
            str(read_ahead),
            "-volume",
            str(self.player.volume_level),
            *extra_args,
            "-dacp",
            self.prov.dacp_id,
            "-activeremote",
            self.active_remote_id,
            "-cmdpipe",
            self.commands_named_pipe,
            "-udn",
            self.player.discovery_info.name,
            self.player.address,
            self.audio_named_pipe,
        ]
        self.player.logger.debug(
            "Starting cliraop process for player %s with args: %s",
            self.player.player_id,
            cliraop_args,
        )
        self._cli_proc = AsyncProcess(cliraop_args, stdin=False, stderr=True, name="cliraop")
        await self._cli_proc.start()
        # read up to first 50 lines of stderr to get the initial status
        for _ in range(50):
            line = (await self._cli_proc.read_stderr()).decode("utf-8", errors="ignore")
            self.player.logger.debug(line)
            if "connected to " in line:
                self.player.logger.info("AirPlay device connected. Starting playback.")
                self._started.set()
                # Open pipes now that cliraop is ready
                await self._open_pipes()
                break
            if "Cannot connect to AirPlay device" in line:
                raise PlayerCommandFailed("Cannot connect to AirPlay device")
        # repeat sending the volume level to the player because some players seem
        # to ignore it the first time
        # https://github.com/music-assistant/support/issues/3330
        await self.send_cli_command(f"VOLUME={self.player.volume_level}\n")
        # start reading the stderr of the cliraop process from another task
        self._stderr_reader_task = self.mass.create_task(self._stderr_reader())

    async def _stderr_reader(self) -> None:
        """Monitor stderr for the running CLIRaop process."""
        player = self.player
        logger = player.logger
        lost_packets = 0
        if not self._cli_proc:
            return
        async for line in self._cli_proc.iter_stderr():
            if "elapsed milliseconds:" in line:
                # this is received more or less every second while playing
                # millis = int(line.split("elapsed milliseconds: ")[1])
                # self.player.elapsed_time = (millis / 1000) - self.elapsed_time_correction
                # self.player.elapsed_time_last_updated = time.time()
                logger.log(VERBOSE_LOG_LEVEL, line)
                continue
            if "set pause" in line or "Pause at" in line:
                player.set_state_from_stream(state=PlaybackState.PAUSED)
            if "Restarted at" in line or "restarting w/ pause" in line:
                player.set_state_from_stream(state=PlaybackState.PLAYING)
            if "restarting w/o pause" in line:
                # streaming has started
                player.set_state_from_stream(state=PlaybackState.PLAYING, elapsed_time=0)
            if "lost packet out of backlog" in line:
                lost_packets += 1
                if lost_packets == 100:
                    logger.error("High packet loss detected, restarting playback...")
                    self.mass.create_task(self.mass.players.cmd_resume(self.player.player_id))
                else:
                    logger.warning("Packet loss detected!")
            if "end of stream reached" in line:
                logger.debug("End of stream reached")
                break
            logger.debug(line)

        # ensure we're cleaned up afterwards (this also logs the returncode)
        logger.debug("CLIRaop stderr reader ended")
        await self.stop()
