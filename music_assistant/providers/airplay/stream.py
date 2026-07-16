"""
AirPlay audio streaming using the cliairplay binary.

Handles both RAOP (AirPlay 1) and AirPlay 2 protocols through a single
unified binary. Audio is fed via stdin, commands via a named pipe,
status is reported on stderr in normalized [STATUS] format.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import time
from typing import TYPE_CHECKING, cast

from music_assistant_models.enums import PlaybackState

from music_assistant.constants import VERBOSE_LOG_LEVEL
from music_assistant.helpers.named_pipe import AsyncNamedPipeWriter
from music_assistant.helpers.process import AsyncProcess
from music_assistant.providers.airplay.constants import (
    AIRPLAY_PCM_FORMAT,
    CONF_AIRPLAY_CREDENTIALS,
    CONF_PASSWORD,
    StreamingProtocol,
)
from music_assistant.providers.airplay.helpers import (
    generate_active_remote_id,
    get_cli_binary,
    resolve_if_ip,
)

if TYPE_CHECKING:
    from music_assistant_models.player import PlayerMedia

    from music_assistant.providers.airplay.player import AirPlayPlayer
    from music_assistant.providers.airplay.provider import AirPlayProvider
    from music_assistant.providers.airplay.stream_session import AirPlayStreamSession


class AirPlayStream:
    """AirPlay audio streamer using the unified cliairplay binary."""

    _cli_proc: AsyncProcess | None
    session: AirPlayStreamSession | None = None

    pcm_format = AIRPLAY_PCM_FORMAT

    def __init__(self, player: AirPlayPlayer) -> None:
        """
        Initialize AirPlay stream.

        :param player: The player to stream to.
        """
        self.prov = player.provider
        self.mass = player.provider.mass
        self.player = player
        self.logger = player.provider.logger.getChild("stream")
        mac_address = self.player.device_info.mac_address or self.player.player_id
        self.active_remote_id: str = generate_active_remote_id(mac_address)
        self.prevent_playback: bool = False
        self._cli_proc: AsyncProcess | None = None
        self.commands_pipe = AsyncNamedPipeWriter(
            f"/tmp/{self.player.protocol.value}-{self.player.player_id}-{self.active_remote_id}-cmd",  # noqa: S108
        )
        self._stopped = False
        self._connected = asyncio.Event()
        self._metadata_checksum = ""
        self._last_progress_sent: int = -1
        self._elapsed_time_offset: float | None = None

    @property
    def running(self) -> bool:
        """Return boolean if this stream is running."""
        return not self._stopped and self._cli_proc is not None and not self._cli_proc.closed

    @property
    def connected(self) -> bool:
        """Return boolean if the device connection has been established."""
        return self._connected.is_set()

    async def start(self, start_ntp: int) -> None:
        """
        Start cliairplay process.

        :param start_ntp: NTP timestamp to start streaming.
        """
        cli_binary = await get_cli_binary()
        protocol = self.player.protocol
        prov = cast("AirPlayProvider", self.prov)

        args: list[str] = [
            cli_binary,
            "--protocol",
            "raop" if protocol == StreamingProtocol.RAOP else "airplay2",
            "--latency",
            str(self.player.output_buffer_duration_ms),
            "--volume",
            str(self.player.volume_level),
            "--dacp",
            prov.dacp_id,
            "--activeremote",
            self.active_remote_id,
            "--cmdpipe",
            self.commands_pipe.path,
            "--ntpstart",
            str(start_ntp),
            "--samplerate",
            str(self.pcm_format.sample_rate),
            "--bitdepth",
            str(self.pcm_format.bit_depth),
        ]

        # Device port (from AP2 or RAOP discovery)
        if protocol == StreamingProtocol.AIRPLAY2 and self.player.airplay_discovery_info:
            args += ["--port", str(self.player.airplay_discovery_info.port)]
            args += ["--name", self.player.display_name]
            args += ["--hostname", str(self.player.airplay_discovery_info.server)]
        elif self.player.raop_discovery_info:
            args += ["--port", str(self.player.raop_discovery_info.port)]
            args += ["--udn", self.player.raop_discovery_info.name]

        # mDNS properties (needed by both flows)
        if self.player.raop_discovery_info:
            for prop in ("et", "md", "am", "pk", "pw"):
                if prop_value := self.player.raop_discovery_info.decoded_properties.get(prop):
                    args += [f"--{prop}", prop_value]

        # HAP credentials (triggers native AP2 flow when present)
        if creds := self.player.config.get_value(CONF_AIRPLAY_CREDENTIALS):
            creds_str = str(creds)
            if len(creds_str) == 192:
                args += ["--auth", creds_str]
            else:
                self.player.logger.warning(
                    "Invalid credentials length: %d (expected 192)", len(creds_str)
                )

        # Device password
        if password := self.player.config.get_value(CONF_PASSWORD):
            args += ["--password", str(password)]

        # Local interface binding
        if_ip = await resolve_if_ip(self.mass, str(self.player.device_info.ip_address))
        if if_ip not in ("0.0.0.0", "::", ""):
            try:
                source_is_ipv6 = isinstance(ipaddress.ip_address(if_ip), ipaddress.IPv6Address)
                target_is_ipv6 = ":" in self.player.address
                if source_is_ipv6 == target_is_ipv6:
                    args += ["--if", if_ip]
            except ValueError:
                pass

        # Debug level
        if self.prov.logger.isEnabledFor(VERBOSE_LOG_LEVEL):
            args += ["--debug", "10"]
        elif self.prov.logger.isEnabledFor(logging.DEBUG):
            args += ["--debug", "5"]

        # Positional args: device address + stdin for audio
        args += [self.player.address, "-"]

        self.player.logger.debug(
            "Starting cliairplay (%s) for player %s",
            "RAOP" if protocol == StreamingProtocol.RAOP else "AP2",
            self.player.player_id,
        )
        self._cli_proc = AsyncProcess(args, stdin=True, stderr=True, name="cliairplay")
        await self._cli_proc.start()
        self._cli_proc.attach_stderr_reader(self.mass.create_task(self._stderr_reader()))

    async def wait_for_connection(self) -> None:
        """Wait for device connection to be established."""
        if not self._cli_proc:
            return
        await asyncio.wait_for(self._connected.wait(), timeout=10)
        volume = 0 if self.player.volume_muted else self.player.volume_level
        self.mass.call_later(2, self.send_cli_command(f"VOLUME={volume}"))
        self._metadata_checksum = ""
        self.mass.call_later(2, self.player._on_player_media_updated)

    async def stop(self, force: bool = False) -> None:
        """
        Stop playback and cleanup.

        :param force: If True, immediately kill the process without graceful shutdown.
        """
        await self.send_cli_command("ACTION=STOP")
        self._stopped = True
        await self.commands_pipe.remove()
        if force:
            if self._cli_proc and not self._cli_proc.closed:
                await self._cli_proc.kill()
        else:
            if self._cli_proc:
                await self._cli_proc.write_eof()
            if self._cli_proc and not self._cli_proc.closed:
                await self._cli_proc.close()
        self.player.set_state_from_stream(state=PlaybackState.IDLE, elapsed_time=0)

    async def write_audio(self, data: bytes) -> None:
        """
        Write raw audio data to the CLI process stdin.

        :param data: Raw audio bytes to send to the streaming process.
        """
        if self._stopped or not self._cli_proc or self._cli_proc.closed:
            return
        await self._cli_proc.write(data)

    async def write_audio_eof(self) -> None:
        """Signal end-of-stream to the CLI process stdin."""
        if self._stopped or not self._cli_proc or self._cli_proc.closed:
            return
        await self._cli_proc.write_eof()

    async def send_cli_command(self, command: str) -> None:
        """Send an interactive command to the running CLI binary."""
        if self._stopped or not self._cli_proc or self._cli_proc.closed:
            return
        if not self.commands_pipe:
            return
        self.player.last_command_sent = time.time()
        if not command.endswith("\n"):
            command += "\n"
        await self.commands_pipe.write(command.encode("utf-8"))

    async def send_metadata(self, progress: int | None, metadata: PlayerMedia | None) -> None:
        """Send metadata to player."""
        if self._stopped:
            return
        if metadata:
            duration = min(metadata.duration or 0, 3600)
            title = metadata.title or ""
            artist = metadata.artist or ""
            album = metadata.album or ""

            metadata_checksum = f"{title}|{artist}|{album}|{duration}|{metadata.image_url}"
            if metadata_checksum == self._metadata_checksum:
                return
            self._metadata_checksum = metadata_checksum

            cmd = f"TITLE={title}\nARTIST={artist}\nALBUM={album}\n"
            cmd += f"DURATION={duration}\nPROGRESS=0\nACTION=SENDMETA\n"

            await self.send_cli_command(cmd)
            self._last_progress_sent = 0
            if metadata.image_url:
                await self.send_cli_command(f"ARTWORK={metadata.image_url}")
        if progress is not None and abs(progress - self._last_progress_sent) >= 2:
            self._last_progress_sent = progress
            await self.send_cli_command(f"PROGRESS={progress}")

    async def _stderr_reader(self) -> None:
        """
        Monitor stderr for the running cliairplay process.

        The binary emits normalized [STATUS] messages:
          [STATUS] connected
          [STATUS] playing elapsed_ms=<ms>
          [STATUS] paused
          [STATUS] eof
          [ERROR] <message>
        """
        player = self.player
        logger = player.logger
        expected_eof = False
        if not self._cli_proc:
            return
        async for line in self._cli_proc.iter_stderr():
            if self._stopped:
                break

            if "[STATUS] connected" in line:
                self._connected.set()
            elif "[STATUS] playing elapsed_ms=" in line:
                try:
                    millis = int(line.split("elapsed_ms=")[1])
                    self._update_elapsed(millis / 1000)
                except ValueError, IndexError:
                    pass
            elif "[STATUS] paused" in line:
                player.set_state_from_stream(state=PlaybackState.PAUSED, stream=self)
            elif "[STATUS] eof" in line:
                logger.debug("End of stream reached")
                expected_eof = True
                break
            elif "[ERROR]" in line:
                logger.error("cliairplay: %s", line.strip())

            logger.log(VERBOSE_LOG_LEVEL, line)
            await asyncio.sleep(0)

        logger.debug("cliairplay stderr reader ended")
        if not self._stopped:
            self._stopped = True
            player.set_state_from_stream(state=PlaybackState.IDLE, elapsed_time=0, stream=self)
            if not expected_eof:
                logger.warning(
                    "cliairplay process stopped unexpectedly for %s", player.display_name
                )
                self.mass.create_task(self.mass.players.cmd_ungroup(player.player_id))

    def _update_elapsed(self, elapsed_time: float) -> None:
        """Update elapsed time with session offset compensation."""
        if self._elapsed_time_offset is None and self.session:
            self._elapsed_time_offset = max(0, time.time() - self.session.start_time - elapsed_time)
        if self._elapsed_time_offset:
            elapsed_time += self._elapsed_time_offset
        self.player.set_state_from_stream(elapsed_time=elapsed_time, stream=self)
