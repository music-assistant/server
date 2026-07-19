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
    CONF_RAOP_CREDENTIALS,
    StreamingProtocol,
)
from music_assistant.providers.airplay.helpers import (
    generate_active_remote_id,
    get_cli_binary,
    resolve_if_ip,
    serialize_txt_records,
)

if TYPE_CHECKING:
    from music_assistant_models.media_items import AudioFormat
    from music_assistant_models.player import PlayerMedia

    from music_assistant.providers.airplay.player import AirPlayPlayer
    from music_assistant.providers.airplay.provider import AirPlayProvider
    from music_assistant.providers.airplay.stream_session import AirPlayStreamSession


class AirPlayStream:
    """AirPlay audio streamer using the unified cliairplay binary."""

    _cli_proc: AsyncProcess | None
    session: AirPlayStreamSession | None = None

    def __init__(self, player: AirPlayPlayer, pcm_format: AudioFormat | None = None) -> None:
        """
        Initialize AirPlay stream.

        :param player: The player to stream to.
        :param pcm_format: The PCM format fed to the binary's stdin
            (defaults to 44.1kHz/16-bit).
        """
        self.prov = player.provider
        self.mass = player.provider.mass
        self.player = player
        self.pcm_format = pcm_format or AIRPLAY_PCM_FORMAT
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
        self._stdout_reader_task: asyncio.Task[None] | None = None
        # Device latency info reported by the binary after connect (0 = unreported)
        self.latency_lead_ms: int = 0
        self.device_min_frames: int = 0
        self.device_max_frames: int = 0

    @property
    def running(self) -> bool:
        """Return boolean if this stream is running."""
        return not self._stopped and self._cli_proc is not None and not self._cli_proc.closed

    @property
    def connected(self) -> bool:
        """Return boolean if the device connection has been established."""
        return self._connected.is_set()

    async def start(self, start_unix_ms: int) -> None:
        """
        Start cliairplay process.

        :param start_unix_ms: The instant the first sample must be audible,
            as unix epoch milliseconds. All members of a sync group must
            receive the same value.
        """
        args = await self._build_cli_args(start_unix_ms)
        self.player.logger.debug("Starting cliairplay for player %s", self.player.player_id)
        self._cli_proc = AsyncProcess(args, stdin=True, stdout=True, stderr=True, name="cliairplay")
        await self._cli_proc.start()
        self._cli_proc.attach_stderr_reader(self.mass.create_task(self._stderr_reader()))
        self._stdout_reader_task = self.mass.create_task(self._stdout_reader())

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
        # stop the stdout reader first so process close can drain the pipe
        if self._stdout_reader_task and not self._stdout_reader_task.done():
            self._stdout_reader_task.cancel()
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

    async def _build_cli_args(self, start_unix_ms: int) -> list[str]:  # noqa: PLR0915
        """Assemble the cliairplay argument list for this stream."""
        cli_binary = await get_cli_binary()
        prov = cast("AirPlayProvider", self.prov)
        # The user-configured protocol acts as an override only; the default is
        # to let the binary resolve the route from the mDNS TXT (--txt).
        protocol_override = self.player.protocol_override
        if protocol_override == StreamingProtocol.RAOP:
            protocol_arg = "raop"
        elif protocol_override == StreamingProtocol.AIRPLAY2:
            protocol_arg = "airplay2"
        else:
            protocol_arg = "auto"

        args: list[str] = [
            cli_binary,
            "--protocol",
            protocol_arg,
            "--volume",
            str(self.player.volume_level),
            "--dacp",
            prov.dacp_id,
            "--activeremote",
            self.active_remote_id,
            "--cmdpipe",
            self.commands_pipe.path,
            "--start-unix-ms",
            str(start_unix_ms),
            "--samplerate",
            str(self.pcm_format.sample_rate),
            "--bitdepth",
            str(self.pcm_format.bit_depth),
        ]

        # Playback lead/buffer: the binary's default (2000 ms, clamped to the
        # device-reported window) is used unless the user explicitly overrides it.
        if latency_override := self.player.latency_override_ms:
            args += ["--latency", str(latency_override)]

        airplay_info = self.player.airplay_discovery_info
        raop_info = self.player.raop_discovery_info
        # Connection target: the AirPlay 2 service whenever it may be used
        # (auto or forced airplay2), the RAOP service otherwise. All AirPlay 2
        # routes (native and RAOP-compat) run against the _airplay._tcp port.
        if airplay_info and protocol_arg != "raop":
            args += ["--port", str(airplay_info.port)]
            args += ["--name", self.player.display_name]
            args += ["--hostname", str(airplay_info.server)]
        elif raop_info:
            args += ["--port", str(raop_info.port)]

        # mDNS properties from the RAOP service (needed by the RAOP-based flows)
        if raop_info:
            args += ["--udn", raop_info.name]
            for prop in ("et", "md", "am", "pk", "pw", "cn"):
                if prop_value := raop_info.decoded_properties.get(prop):
                    args += [f"--{prop}", prop_value]

        # Full _airplay._tcp TXT for the binary's automatic route selection
        if airplay_info and (txt_records := serialize_txt_records(airplay_info)):
            args += ["--txt", txt_records]

        # HAP credentials (triggers native AP2 flow when present)
        if creds := self.player.config.get_value(CONF_AIRPLAY_CREDENTIALS):
            creds_str = str(creds)
            if len(creds_str) == 192:
                args += ["--auth", creds_str]
            else:
                self.player.logger.warning(
                    "Invalid credentials length: %d (expected 192)", len(creds_str)
                )

        # Legacy Apple TV RAOP pairing secret
        if raop_creds := self.player.config.get_value(CONF_RAOP_CREDENTIALS):
            # Credentials format is "client_id:auth_secret", the binary expects the secret
            creds_str = str(raop_creds)
            auth_secret = creds_str.split(":", 1)[1] if ":" in creds_str else creds_str
            args += ["--secret", auth_secret]

        # Device password
        if password := self.player.config.get_value(CONF_PASSWORD):
            args += ["--password", str(password)]

        # Shared PTP daemon clock (multi-room sync for native AP2 streams)
        if protocol_arg != "raop" and prov.ptp_daemon_running:
            args += ["--ptp-shared"]

        # Local interface binding
        if_arg: str | None = None
        target_is_ipv6 = ":" in self.player.address
        if_ip = await resolve_if_ip(self.mass, str(self.player.device_info.ip_address))
        if if_ip not in ("0.0.0.0", "::", ""):
            try:
                source_is_ipv6 = isinstance(ipaddress.ip_address(if_ip), ipaddress.IPv6Address)
                if source_is_ipv6 == target_is_ipv6:
                    if_arg = if_ip
                    args += ["--if", if_ip]
            except ValueError:
                pass

        # Address advertised inside the protocol (timing peers) for hosts where
        # the reachable address differs from the bind address (e.g. containers).
        publish_ip = str(self.mass.streams.publish_ip or "")
        if publish_ip and publish_ip != if_arg:
            try:
                publish_is_ipv6 = isinstance(
                    ipaddress.ip_address(publish_ip), ipaddress.IPv6Address
                )
                if publish_is_ipv6 == target_is_ipv6:
                    args += ["--publish-ip", publish_ip]
            except ValueError:
                pass

        # Debug level
        if self.prov.logger.isEnabledFor(VERBOSE_LOG_LEVEL):
            args += ["--debug", "10"]
        elif self.prov.logger.isEnabledFor(logging.DEBUG):
            args += ["--debug", "5"]

        # Positional args: device address + stdin for audio
        args += [self.player.address, "-"]
        return args

    async def _stdout_reader(self) -> None:
        """
        Monitor stdout for the running cliairplay process.

        After connect the binary reports the effective lead and the
        receiver-reported buffering window on stdout:
          [STATUS] latency lead_ms=<int> device_min_frames=<int> device_max_frames=<int>
        """
        if not self._cli_proc:
            return
        buffer = b""
        while chunk := await self._cli_proc.read(1024):
            buffer += chunk
            while b"\n" in buffer:
                raw_line, buffer = buffer.split(b"\n", 1)
                line = raw_line.decode("utf-8", errors="ignore").strip()
                if not line:
                    continue
                if "[STATUS] latency" in line:
                    self._parse_latency_status(line)
                self.player.logger.log(VERBOSE_LOG_LEVEL, line)

    def _parse_latency_status(self, line: str) -> None:
        """Parse and store the [STATUS] latency line reported by the binary."""
        try:
            fields = dict(part.split("=", 1) for part in line.split() if "=" in part)
            self.latency_lead_ms = int(fields.get("lead_ms", 0))
            self.device_min_frames = int(fields.get("device_min_frames", 0))
            self.device_max_frames = int(fields.get("device_max_frames", 0))
        except ValueError:
            return
        self.player.logger.debug(
            "Device latency for %s: lead=%dms, buffer window=%d-%d frames (0=unreported)",
            self.player.display_name,
            self.latency_lead_ms,
            self.device_min_frames,
            self.device_max_frames,
        )

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
            if not expected_eof:
                logger.warning(
                    "cliairplay process stopped unexpectedly for %s", player.display_name
                )
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

    def _update_elapsed(self, elapsed_time: float) -> None:
        """Update elapsed time with session offset compensation."""
        if self._elapsed_time_offset is None and self.session:
            self._elapsed_time_offset = max(0, time.time() - self.session.start_time - elapsed_time)
        if self._elapsed_time_offset:
            elapsed_time += self._elapsed_time_offset
        self.player.set_state_from_stream(elapsed_time=elapsed_time, stream=self)
