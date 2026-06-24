"""Logic for RAOP audio streaming to AirPlay devices."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import time
from typing import TYPE_CHECKING, cast

from music_assistant_models.enums import PlaybackState

from music_assistant.constants import VERBOSE_LOG_LEVEL
from music_assistant.helpers.process import AsyncProcess
from music_assistant.providers.airplay.constants import (
    CONF_ALAC_ENCODE,
    CONF_ENCRYPTION,
    CONF_PASSWORD,
    CONF_RAOP_CREDENTIALS,
)
from music_assistant.providers.airplay.helpers import get_cli_binary, resolve_if_ip

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

    async def start(self, start_ntp: int) -> None:
        """Start CLIRaop process."""
        assert self.player.raop_discovery_info is not None  # for type checker
        cli_binary = await get_cli_binary(self.player.protocol)
        extra_args: list[str] = []
        if_ip = await resolve_if_ip(self.mass, str(self.player.device_info.ip_address))
        # Only pass -if when source and target use the same address family.
        # cliraop cannot use IPv6 source for IPv4 target (or vice versa).
        if if_ip not in ("0.0.0.0", "::", ""):
            try:
                source_is_ipv6 = isinstance(ipaddress.ip_address(if_ip), ipaddress.IPv6Address)
                target_is_ipv6 = ":" in self.player.address
                if source_is_ipv6 == target_is_ipv6:
                    extra_args += ["-if", if_ip]
            except ValueError:
                self.player.logger.debug("Skipping invalid interface value for -if: %s", if_ip)
        if self.player.config.get_value(CONF_ENCRYPTION, True):
            extra_args += ["-encrypt"]
        if self.player.config.get_value(CONF_ALAC_ENCODE, True):
            extra_args += ["-alac"]
        for prop in ("et", "md", "am", "pk", "pw"):
            if prop_value := self.player.raop_discovery_info.decoded_properties.get(prop):
                extra_args += [f"-{prop}", prop_value]
        if device_password := self.player.config.get_value(CONF_PASSWORD):
            extra_args += ["-password", str(device_password)]
        # Add RAOP credentials from pairing if available (for Apple devices)
        if raop_credentials := self.player.config.get_value(CONF_RAOP_CREDENTIALS):
            # Credentials format is "client_id:auth_secret", cliraop expects just auth_secret
            creds_str = str(raop_credentials)
            auth_secret = creds_str.split(":", 1)[1] if ":" in creds_str else creds_str
            extra_args += ["-secret", auth_secret]
        if self.prov.logger.isEnabledFor(logging.DEBUG):
            extra_args += ["-debug", "5"]
        elif self.prov.logger.isEnabledFor(VERBOSE_LOG_LEVEL):
            extra_args += ["-debug", "10"]

        cliraop_args = [
            cli_binary,
            "-ntpstart",
            str(start_ntp),
            "-port",
            str(self.player.raop_discovery_info.port),
            "-latency",
            str(self.player.output_buffer_duration_ms),
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
            "-",  # Use stdin for audio input
        ]
        self.player.logger.debug(
            "Starting cliraop process for player %s with args: %s",
            self.player.player_id,
            cliraop_args,
        )
        self._cli_proc = AsyncProcess(cliraop_args, stdin=True, stderr=True, name="cliraop")
        await self._cli_proc.start()
        # start reading the stderr of the cliap2 process from another task
        self._cli_proc.attach_stderr_reader(self.mass.create_task(self._stderr_reader()))

    async def _stderr_reader(self) -> None:
        """Monitor stderr for the running CLIRaop process."""
        player = self.player
        logger = player.logger
        lost_packets = 0
        expected_eof = False
        if not self._cli_proc:
            return
        async for line in self._cli_proc.iter_stderr():
            if self._stopped:
                break
            if "connected to " in line:
                self._connected.set()
                # successfully connected - playback will/can start
            if "set pause" in line or "Pause at" in line:
                player.set_state_from_stream(state=PlaybackState.PAUSED, stream=self)
            elif "Restarted at" in line or "restarting w/ pause" in line:
                player.set_state_from_stream(state=PlaybackState.PLAYING, stream=self)
            elif "restarting w/o pause" in line:
                # streaming has started
                player.set_state_from_stream(
                    state=PlaybackState.PLAYING, elapsed_time=0, stream=self
                )
            elif "elapsed milliseconds:" in line:
                # this is received more or less every second while playing
                millis = int(line.split("elapsed milliseconds: ")[1])
                elapsed_time = millis / 1000
                # on the first elapsed time report, compute a fixed offset between
                # the session start_time and this cliraop process start.
                # this handles dynamic leader switching where a new cliraop process
                # reports from 0 while the flow stream started much earlier.
                if self._elapsed_time_offset is None and self.session:
                    self._elapsed_time_offset = max(
                        0, time.time() - self.session.start_time - elapsed_time
                    )
                if self._elapsed_time_offset:
                    elapsed_time += self._elapsed_time_offset
                player.set_state_from_stream(elapsed_time=elapsed_time, stream=self)
            elif "Password required, but none supplied." in line:
                logger.error(
                    f"Player {self.player.name} requires a password. "
                    f"Please add one in Player Settings"
                )
                break
            if "lost packet out of backlog" in line:
                lost_packets += 1
                if lost_packets == 100:
                    logger.error("High packet loss detected, restarting playback...")
                    self.mass.create_task(self.mass.players.cmd_resume(self.player.player_id))
                else:
                    logger.warning("Packet loss detected!")
            if "end of stream reached" in line:
                logger.debug("End of stream reached")
                expected_eof = True
                break
            logger.log(VERBOSE_LOG_LEVEL, line)
            await asyncio.sleep(0)  # Yield to event loop

        logger.debug("CLIRaop stderr reader ended")
        if not self._stopped:
            self._stopped = True
            if not expected_eof:
                logger.warning("CLIRaop process stopped unexpectedly for %s", player.display_name)
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
