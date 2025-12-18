"""Logic for RAOP audio streaming to AirPlay devices."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, cast

from music_assistant_models.enums import PlaybackState
from music_assistant_models.errors import PlayerCommandFailed

from music_assistant.constants import VERBOSE_LOG_LEVEL
from music_assistant.helpers.process import AsyncProcess
from music_assistant.providers.airplay.constants import (
    AIRPLAY_OUTPUT_BUFFER_DURATION_MS,
    CONF_ALAC_ENCODE,
    CONF_AP_CREDENTIALS,
    CONF_ENCRYPTION,
    CONF_PASSWORD,
)
from music_assistant.providers.airplay.helpers import get_cli_binary

from ._protocol import AirPlayProtocol

if TYPE_CHECKING:
    from music_assistant.providers.airplay.provider import AirPlayProvider


class RaopStream(AirPlayProtocol):
    """
    RAOP (AirPlay 1) Audio Streamer.

    Python is not suitable for realtime audio streaming so we do the actual streaming
    of (RAOP) audio using a small executable written in C based on libraop to do
    the actual timestamped playback, which reads pcm audio from stdin
    and we can send some interactive commands using a named pipe.
    """

    supports_pairing = True

    async def start(self, start_ntp: int) -> None:
        """Initialize CLIRaop process for a player."""
        assert self.player.raop_discovery_info is not None  # for type checker
        cli_binary = await get_cli_binary(self.player.protocol)
        extra_args: list[str] = []
        player_id = self.player.player_id
        extra_args += ["-if", self.mass.streams.bind_ip]
        if self.player.config.get_value(CONF_ENCRYPTION, True):
            extra_args += ["-encrypt"]
        if self.player.config.get_value(CONF_ALAC_ENCODE, True):
            extra_args += ["-alac"]
        for prop in ("et", "md", "am", "pk", "pw"):
            if prop_value := self.player.raop_discovery_info.decoded_properties.get(prop):
                extra_args += [f"-{prop}", prop_value]
        if device_password := self.mass.config.get_raw_player_config_value(
            player_id, CONF_PASSWORD
        ):
            extra_args += ["-password", str(device_password)]
        # Add AirPlay credentials from pairing if available (for Apple devices)
        if ap_credentials := self.player.config.get_value(CONF_AP_CREDENTIALS):
            extra_args += ["-secret", str(ap_credentials)]
        if self.prov.logger.isEnabledFor(logging.DEBUG):
            extra_args += ["-debug", "5"]
        elif self.prov.logger.isEnabledFor(VERBOSE_LOG_LEVEL):
            extra_args += ["-debug", "10"]

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
            str(self.player.raop_discovery_info.port),
            "-latency",
            str(AIRPLAY_OUTPUT_BUFFER_DURATION_MS),
            "-volume",
            str(self.player.volume_level),
            *extra_args,
            "-dacp",
            cast("AirPlayProvider", self.prov).dacp_id,
            "-activeremote",
            self.active_remote_id,
            "-cmdpipe",
            self.commands_pipe.path,
            "-udn",
            self.player.raop_discovery_info.name,
            self.player.address,
            self.audio_pipe.path,
        ]
        self.player.logger.debug(
            "Starting cliraop process for player %s with args: %s",
            self.player.player_id,
            cliraop_args,
        )
        self._cli_proc = AsyncProcess(cliraop_args, stdin=True, stderr=True, name="cliraop")
        await self._cli_proc.start()

        # read up to first 50 lines of stderr to get the initial status
        for _ in range(50):
            line = (await self._cli_proc.read_stderr()).decode("utf-8", errors="ignore")
            self.player.logger.debug(line)
            if "connected to " in line:
                self.player.logger.info("AirPlay device connected. Starting playback.")
                break
            if "Cannot connect to AirPlay device" in line:
                raise PlayerCommandFailed("Cannot connect to AirPlay device")

        # start reading the stderr of the cliraop process from another task
        self._cli_proc.attach_stderr_reader(self.mass.create_task(self._stderr_reader()))
        # repeat sending the volume level to the player because some players seem
        # to ignore it the first time
        # https://github.com/music-assistant/support/issues/3330
        self.mass.call_later(1, self.send_cli_command(f"VOLUME={self.player.volume_level}\n"))

    async def start_pairing(self) -> None:
        """Start pairing process for this protocol (if supported)."""
        assert self.player.raop_discovery_info is not None  # for type checker
        cli_binary = await get_cli_binary(self.player.protocol)

        cliraop_args = [
            cli_binary,
            "-pair",
            "-if",
            self.mass.streams.bind_ip,
            "-port",
            str(self.player.raop_discovery_info.port),
            "-udn",
            self.player.raop_discovery_info.name,
            self.player.address,
        ]
        self.player.logger.debug(
            "Starting PAIRING with cliraop process for player %s with args: %s",
            self.player.player_id,
            cliraop_args,
        )
        self._cli_proc = AsyncProcess(cliraop_args, stdin=True, stderr=True, name="cliraop")
        await self._cli_proc.start()
        # read up to first 10 lines of stderr to get the initial status
        for _ in range(10):
            line = (await self._cli_proc.read_stderr()).decode("utf-8", errors="ignore")
            self.player.logger.debug(line)
            if "enter PIN code displayed on " in line:
                self.is_pairing = True
                return
        await self._cli_proc.close()
        raise PlayerCommandFailed("Pairing failed")

    async def finish_pairing(self, pin: str) -> str:
        """Finish pairing process with given PIN (if supported)."""
        if not self.is_pairing:
            await self.start_pairing()
        if not self._cli_proc or self._cli_proc.closed:
            raise PlayerCommandFailed("Pairing process not started")

        self.is_pairing = False
        _, _stderr = await self._cli_proc.communicate(input=f"{pin}\n".encode(), timeout=10)
        for line in _stderr.decode().splitlines():
            self.player.logger.debug(line)
            for error in ("device did not respond", "can't authentify", "pin failed"):
                if error in line.lower():
                    raise PlayerCommandFailed(f"Pairing failed: {error}")
            if "secret is " in line:
                return line.split("secret is ")[1].strip()
        raise PlayerCommandFailed(f"Pairing failed: {_stderr.decode().strip()}")

    async def _stderr_reader(self) -> None:
        """Monitor stderr for the running CLIRaop process."""
        player = self.player
        logger = player.logger
        lost_packets = 0
        if not self._cli_proc:
            return
        async for line in self._cli_proc.iter_stderr():
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
            logger.log(VERBOSE_LOG_LEVEL, line)
            await asyncio.sleep(0)  # Yield to event loop

        logger.debug("CLIRaop stderr reader ended")
        if not self._stopped:
            self._stopped = True
            self.player.set_state_from_stream(state=PlaybackState.IDLE, elapsed_time=0)
