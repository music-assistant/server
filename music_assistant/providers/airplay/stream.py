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

import aiofiles
from music_assistant_models.enums import PlaybackState

from music_assistant.constants import VERBOSE_LOG_LEVEL
from music_assistant.helpers.images import get_image_thumb
from music_assistant.helpers.named_pipe import AsyncNamedPipeWriter
from music_assistant.helpers.process import AsyncProcess
from music_assistant.providers.airplay.constants import (
    AIRPLAY_ARTWORK_SIZE,
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
        # Route the binary resolved for this stream (empty until reported),
        # e.g. "AirPlay 2 (native, PTP)" or "RAOP"
        self.active_route: str = ""
        # Local file the rendered cover art is written to for the binary to embed
        self._artwork_path = f"/tmp/cliairplay-art-{self.player.player_id}.jpg"  # noqa: S108

    @property
    def running(self) -> bool:
        """Return boolean if this stream is running."""
        return not self._stopped and self._cli_proc is not None and not self._cli_proc.closed

    @property
    def connected(self) -> bool:
        """Return boolean if the device connection has been established."""
        return self._connected.is_set()

    async def start(self, start_unix_ms: int, use_shared_ptp: bool | None = None) -> None:
        """
        Start cliairplay process.

        :param start_unix_ms: The instant the first sample must be audible,
            as unix epoch milliseconds. All members of a sync group must
            receive the same value.
        :param use_shared_ptp: Session-wide decision on whether native AirPlay 2
            members attach to the shared PTP clock daemon. The stream session
            passes the same value to every member so a group never mixes PTP and
            NTP timing. None (single-stream callers) falls back to the daemon's
            live state.
        """
        args = await self._build_cli_args(start_unix_ms, use_shared_ptp)
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
        # Send the mute-aware volume right away — audio can start within a
        # second now that metadata goes out immediately — and repeat it after
        # 2 seconds because some players ignore the first volume command
        # (https://github.com/music-assistant/support/issues/3330).
        volume = 0 if self.player.volume_muted else self.player.volume_level
        await self.send_cli_command(f"VOLUME={volume}")
        self.mass.call_later(2, self.send_cli_command(f"VOLUME={volume}"))
        self._metadata_checksum = ""
        # Push track metadata immediately on connect. Some receivers (notably
        # Sonos) hold back audio rendering until they receive track metadata
        # anchored to the stream timeline; the binary anchors that timeline the
        # instant it reports connected, so a metadata command issued now lands
        # with a valid anchor. Deferring it kept those devices silent past the
        # scheduled start, clipping the first seconds of the stream.
        self.player._on_player_media_updated()

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
            if metadata.image_url and (artwork := await self._prepare_artwork(metadata.image_url)):
                await self.send_cli_command(f"ARTWORK={artwork}")
        if progress is not None and abs(progress - self._last_progress_sent) >= 2:
            self._last_progress_sent = progress
            await self.send_cli_command(f"PROGRESS={progress}")

    async def _build_cli_args(  # noqa: PLR0915
        self, start_unix_ms: int, use_shared_ptp: bool | None = None
    ) -> list[str]:
        """
        Assemble the cliairplay argument list for this stream.

        :param start_unix_ms: The audible-start instant in unix epoch ms.
        :param use_shared_ptp: Whether a native AirPlay 2 stream attaches to the
            shared PTP clock daemon. The stream session passes an explicit
            group-wide decision so members never mix PTP and NTP timing; None
            (single-stream callers) falls back to the daemon's live state.
        """
        cli_binary = await get_cli_binary()
        prov = cast("AirPlayProvider", self.prov)
        # The "force RAOP" escape hatch is the only override; everything else lets
        # the binary resolve the route from the mDNS TXT (--txt). We never pass
        # "airplay2" explicitly: the binary's auto route selection is authoritative.
        protocol_arg = "raop" if self.player.protocol_override == StreamingProtocol.RAOP else "auto"

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

        # Shared PTP daemon clock (multi-room sync for native AP2 streams). The
        # decision is made once per session and passed in, so every native AP2
        # member of a sync group uses the same timing source and cannot drift.
        # A single-stream caller (use_shared_ptp is None) falls back to the
        # daemon's live state.
        if protocol_arg != "raop":
            shared_ptp = prov.ptp_daemon_running if use_shared_ptp is None else use_shared_ptp
            if shared_ptp:
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

        The binary reports its resolved route at startup, the effective lead
        plus receiver-reported buffering window after connect, and the result
        of MediaRemote now-playing pushes (Apple devices):
          [STATUS] route protocol=<raop|airplay2> flow=<...> timing=<ntp|ptp> buffered=<0|1>
          [STATUS] latency lead_ms=<int> device_min_frames=<int> device_max_frames=<int>
          [STATUS] mrp path=<command> status=<http status>
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
                if "[STATUS] route" in line:
                    self._parse_route_status(line)
                elif "[STATUS] mrp" in line:
                    self._parse_mrp_status(line)
                elif "[STATUS] latency" in line:
                    self._parse_latency_status(line)
                self.player.logger.log(VERBOSE_LOG_LEVEL, line)

    def _parse_mrp_status(self, line: str) -> None:
        """Parse the [STATUS] mrp line and log the now-playing push result."""
        fields = dict(part.split("=", 1) for part in line.split() if "=" in part)
        self.player.logger.info(
            "MRP now-playing push (%s path) for %s: HTTP %s",
            fields.get("path", "?"),
            self.player.display_name,
            fields.get("status", "?"),
        )

    def _parse_route_status(self, line: str) -> None:
        """Parse the [STATUS] route line and log which route this stream took."""
        fields = dict(part.split("=", 1) for part in line.split() if "=" in part)
        protocol = fields.get("protocol", "")
        if protocol == "airplay2":
            flow = fields.get("flow", "")
            timing = fields.get("timing", "")
            details = "buffered" if fields.get("buffered") == "1" else flow
            self.active_route = f"AirPlay 2 ({details}, {timing.upper()})"
        else:
            self.active_route = "RAOP"
        self.player.logger.info(
            "Streaming to %s via %s", self.player.display_name, self.active_route
        )

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
                except ValueError, IndexError:
                    pass
                else:
                    self._update_elapsed(millis / 1000)
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
        # The binary only emits this status while actually playing, so it is
        # also the signal that drives the player into the PLAYING state.
        self.player.set_state_from_stream(
            state=PlaybackState.PLAYING, elapsed_time=elapsed_time, stream=self
        )

    async def _prepare_artwork(self, image_url: str) -> str | None:
        """
        Render cover art to a local JPEG file the binary can embed.

        The binary consumes artwork as a local file only; it does not fetch
        URLs. The image is flattened to JPEG (legacy AirPlay receivers cannot
        be assumed to handle transparency) and written to a per-player path.

        :param image_url: The (imageproxy or remote) cover-art URL.
        """
        try:
            jpeg = await get_image_thumb(
                self.mass,
                image_url,
                AIRPLAY_ARTWORK_SIZE,
                "",
                image_format="JPEG",
                flatten_transparency=True,
            )
            async with aiofiles.open(self._artwork_path, "wb") as artfile:
                await artfile.write(jpeg)
        except Exception as err:
            self.player.logger.debug("Could not prepare artwork: %s", err)
            return None
        return self._artwork_path
