"""Logic for RAOP (AirPlay 1) audio streaming to Airplay devices."""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import time
from collections.abc import AsyncGenerator
from contextlib import suppress
from random import randint
from typing import TYPE_CHECKING

from music_assistant.common.models.enums import PlayerState
from music_assistant.constants import CONF_SYNC_ADJUST, VERBOSE_LOG_LEVEL
from music_assistant.server.helpers.audio import get_player_filter_params
from music_assistant.server.helpers.ffmpeg import FFMpeg
from music_assistant.server.helpers.process import AsyncProcess, check_output
from music_assistant.server.helpers.util import close_async_generator

from .const import (
    AIRPLAY_PCM_FORMAT,
    CONF_ALAC_ENCODE,
    CONF_BIND_INTERFACE,
    CONF_ENCRYPTION,
    CONF_PASSWORD,
    CONF_READ_AHEAD_BUFFER,
)

if TYPE_CHECKING:
    from music_assistant.common.models.media_items import AudioFormat
    from music_assistant.common.models.player_queue import PlayerQueue

    from .player import AirPlayPlayer
    from .provider import AirplayProvider


class RaopStreamSession:
    """Object that holds the details of a (RAOP) stream session to one or more players."""

    def __init__(
        self,
        airplay_provider: AirplayProvider,
        sync_clients: list[AirPlayPlayer],
        input_format: AudioFormat,
        audio_source: AsyncGenerator[bytes, None],
    ) -> None:
        """Initialize RaopStreamSession."""
        assert sync_clients
        self.prov = airplay_provider
        self.mass = airplay_provider.mass
        self.input_format = input_format
        self._sync_clients = sync_clients
        self._audio_source = audio_source
        self._audio_source_task: asyncio.Task | None = None
        self._stopped: bool = False
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        """Initialize RaopStreamSession."""
        # initialize raop stream for all players
        for airplay_player in self._sync_clients:
            if airplay_player.raop_stream and airplay_player.raop_stream.running:
                raise RuntimeError("Player already has an active stream")
            airplay_player.raop_stream = RaopStream(self, airplay_player)

        async def audio_streamer() -> None:
            """Stream audio to all players."""
            generator_exhausted = False
            try:
                async for chunk in self._audio_source:
                    if not self._sync_clients:
                        return
                    async with self._lock:
                        await asyncio.gather(
                            *[x.raop_stream.write_chunk(chunk) for x in self._sync_clients],
                            return_exceptions=True,
                        )
                # entire stream consumed: send EOF
                generator_exhausted = True
                async with self._lock:
                    await asyncio.gather(
                        *[x.raop_stream.write_eof() for x in self._sync_clients],
                        return_exceptions=True,
                    )
            finally:
                if not generator_exhausted:
                    await close_async_generator(self._audio_source)

        # get current ntp and start RaopStream per player
        _, stdout = await check_output(self.prov.cliraop_bin, "-ntp")
        start_ntp = int(stdout.strip())
        wait_start = 1500 + (250 * len(self._sync_clients))
        async with self._lock:
            await asyncio.gather(
                *[x.raop_stream.start(start_ntp, wait_start) for x in self._sync_clients],
                return_exceptions=True,
            )
        self._audio_source_task = asyncio.create_task(audio_streamer())

    async def stop(self) -> None:
        """Stop playback and cleanup."""
        if self._stopped:
            return
        self._stopped = True
        if self._audio_source_task and not self._audio_source_task.done():
            self._audio_source_task.cancel()
        await asyncio.gather(
            *[self.remove_client(x) for x in self._sync_clients],
            return_exceptions=True,
        )

    async def remove_client(self, airplay_player: AirPlayPlayer) -> None:
        """Remove a sync client from the session."""
        if airplay_player not in self._sync_clients:
            return
        assert airplay_player.raop_stream.session == self
        async with self._lock:
            self._sync_clients.remove(airplay_player)
        await airplay_player.raop_stream.stop()
        airplay_player.raop_stream = None

    async def add_client(self, airplay_player: AirPlayPlayer) -> None:
        """Add a sync client to the session."""
        # TODO: Add the ability to add a new client to an existing session
        # e.g. by counting the number of frames sent etc.
        raise NotImplementedError("Adding clients to a session is not yet supported")


class RaopStream:
    """
    RAOP (Airplay 1) Audio Streamer.

    Python is not suitable for realtime audio streaming so we do the actual streaming
    of (RAOP) audio using a small executable written in C based on libraop to do
    the actual timestamped playback, which reads pcm audio from stdin
    and we can send some interactive commands using a named pipe.
    """

    def __init__(
        self,
        session: RaopStreamSession,
        airplay_player: AirPlayPlayer,
    ) -> None:
        """Initialize RaopStream."""
        self.session = session
        self.prov = session.prov
        self.mass = session.prov.mass
        self.airplay_player = airplay_player

        # always generate a new active remote id to prevent race conditions
        # with the named pipe used to send audio
        self.active_remote_id: str = str(randint(1000, 8000))
        self.prevent_playback: bool = False
        self._log_reader_task: asyncio.Task | None = None
        self._cliraop_proc: AsyncProcess | None = None
        self._ffmpeg_proc: AsyncProcess | None = None
        self._started = asyncio.Event()
        self._stopped = False

    @property
    def running(self) -> bool:
        """Return boolean if this stream is running."""
        return not self._stopped and self._started.is_set()

    async def start(self, start_ntp: int, wait_start: int = 1000) -> None:
        """Initialize CLIRaop process for a player."""
        extra_args = []
        player_id = self.airplay_player.player_id
        mass_player = self.mass.players.get(player_id)
        bind_ip = await self.mass.config.get_provider_config_value(
            self.prov.instance_id, CONF_BIND_INTERFACE
        )
        extra_args += ["-if", bind_ip]
        if self.mass.config.get_raw_player_config_value(player_id, CONF_ENCRYPTION, False):
            extra_args += ["-encrypt"]
        if self.mass.config.get_raw_player_config_value(player_id, CONF_ALAC_ENCODE, True):
            extra_args += ["-alac"]
        for prop in ("et", "md", "am", "pk", "pw"):
            if prop_value := self.airplay_player.discovery_info.decoded_properties.get(prop):
                extra_args += [f"-{prop}", prop_value]
        sync_adjust = self.mass.config.get_raw_player_config_value(player_id, CONF_SYNC_ADJUST, 0)
        if device_password := self.mass.config.get_raw_player_config_value(
            player_id, CONF_PASSWORD, None
        ):
            extra_args += ["-password", device_password]
        if self.prov.logger.isEnabledFor(logging.DEBUG):
            extra_args += ["-debug", "5"]
        elif self.prov.logger.isEnabledFor(VERBOSE_LOG_LEVEL):
            extra_args += ["-debug", "10"]
        read_ahead = await self.mass.config.get_player_config_value(
            player_id, CONF_READ_AHEAD_BUFFER
        )

        # create os pipes to pipe ffmpeg to cliraop
        read, write = await asyncio.to_thread(os.pipe)

        # ffmpeg handles the player specific stream + filters and pipes
        # audio to the cliraop process
        self._ffmpeg_proc = FFMpeg(
            audio_input="-",
            input_format=self.session.input_format,
            output_format=AIRPLAY_PCM_FORMAT,
            filter_params=get_player_filter_params(self.mass, player_id),
            audio_output=write,
        )
        await self._ffmpeg_proc.start()
        await asyncio.to_thread(os.close, write)

        # cliraop is the binary that handles the actual raop streaming to the player
        cliraop_args = [
            self.prov.cliraop_bin,
            "-ntpstart",
            str(start_ntp),
            "-port",
            str(self.airplay_player.discovery_info.port),
            "-wait",
            str(wait_start - sync_adjust),
            "-latency",
            str(read_ahead),
            "-volume",
            str(mass_player.volume_level),
            *extra_args,
            "-dacp",
            self.prov.dacp_id,
            "-activeremote",
            self.active_remote_id,
            "-udn",
            self.airplay_player.discovery_info.name,
            self.airplay_player.address,
            "-",
        ]
        self._cliraop_proc = AsyncProcess(cliraop_args, stdin=read, stderr=True, name="cliraop")
        if platform.system() == "Darwin":
            os.environ["DYLD_LIBRARY_PATH"] = "/usr/local/lib"
        await self._cliraop_proc.start()
        await asyncio.to_thread(os.close, read)
        self._started.set()
        self._log_reader_task = self.mass.create_task(self._log_watcher())

    async def stop(self):
        """Stop playback and cleanup."""
        if self._stopped:
            return
        if self._cliraop_proc.proc and not self._cliraop_proc.closed:
            await self.send_cli_command("ACTION=STOP")
        self._stopped = True  # set after send_cli command!
        if self._cliraop_proc.proc and not self._cliraop_proc.closed:
            await self._cliraop_proc.close(True)
        if self._ffmpeg_proc and not self._ffmpeg_proc.closed:
            await self._ffmpeg_proc.close(True)
        self._cliraop_proc = None
        self._ffmpeg_proc = None

    async def write_chunk(self, chunk: bytes) -> None:
        """Write a (pcm) audio chunk."""
        if self._stopped:
            return
        await self._started.wait()
        await self._ffmpeg_proc.write(chunk)

    async def write_eof(self) -> None:
        """Write EOF."""
        if self._stopped:
            return
        await self._started.wait()
        await self._ffmpeg_proc.write_eof()

    async def send_cli_command(self, command: str) -> None:
        """Send an interactive command to the running CLIRaop binary."""
        if self._stopped:
            return
        await self._started.wait()

        if not command.endswith("\n"):
            command += "\n"

        def send_data():
            with suppress(BrokenPipeError), open(named_pipe, "w") as f:
                f.write(command)

        named_pipe = f"/tmp/raop-{self.active_remote_id}"  # noqa: S108
        self.airplay_player.logger.log(VERBOSE_LOG_LEVEL, "sending command %s", command)
        self.airplay_player.last_command_sent = time.time()
        await asyncio.to_thread(send_data)

    async def _log_watcher(self) -> None:
        """Monitor stderr for the running CLIRaop process."""
        airplay_player = self.airplay_player
        mass_player = self.mass.players.get(airplay_player.player_id)
        queue = self.mass.player_queues.get_active_queue(mass_player.active_source)
        logger = airplay_player.logger
        lost_packets = 0
        prev_metadata_checksum: str = ""
        prev_progress_report: float = 0
        async for line in self._cliraop_proc.iter_stderr():
            if "elapsed milliseconds:" in line:
                # this is received more or less every second while playing
                millis = int(line.split("elapsed milliseconds: ")[1])
                mass_player.elapsed_time = millis / 1000
                mass_player.elapsed_time_last_updated = time.time()
                # send metadata to player(s) if needed
                # NOTE: this must all be done in separate tasks to not disturb audio
                now = time.time()
                if (
                    mass_player.elapsed_time > 2
                    and queue
                    and queue.current_item
                    and queue.current_item.streamdetails
                ):
                    metadata_checksum = (
                        queue.current_item.streamdetails.stream_title
                        or queue.current_item.queue_item_id
                    )
                    if prev_metadata_checksum != metadata_checksum:
                        prev_metadata_checksum = metadata_checksum
                        prev_progress_report = now
                        self.mass.create_task(self._send_metadata(queue))
                    # send the progress report every 5 seconds
                    elif now - prev_progress_report >= 5:
                        prev_progress_report = now
                        self.mass.create_task(self._send_progress(queue))
            if "set pause" in line or "Pause at" in line:
                mass_player.state = PlayerState.PAUSED
                self.mass.players.update(airplay_player.player_id)
            if "Restarted at" in line or "restarting w/ pause" in line:
                mass_player.state = PlayerState.PLAYING
                self.mass.players.update(airplay_player.player_id)
            if "restarting w/o pause" in line:
                # streaming has started
                mass_player.state = PlayerState.PLAYING
                mass_player.elapsed_time = 0
                mass_player.elapsed_time_last_updated = time.time()
                self.mass.players.update(airplay_player.player_id)
            if "lost packet out of backlog" in line:
                lost_packets += 1
                if lost_packets == 100:
                    logger.error("High packet loss detected, restarting playback...")
                    self.mass.create_task(self.mass.player_queues.resume(queue.queue_id))
                else:
                    logger.warning("Packet loss detected!")
            if "end of stream reached" in line:
                logger.debug("End of stream reached")
                break

            logger.log(VERBOSE_LOG_LEVEL, line)

        # if we reach this point, the process exited
        if airplay_player.raop_stream == self:
            mass_player.state = PlayerState.IDLE
            self.mass.players.update(airplay_player.player_id)
        # ensure we're cleaned up afterwards (this also logs the returncode)
        await self.stop()

    async def _send_metadata(self, queue: PlayerQueue) -> None:
        """Send metadata to player (and connected sync childs)."""
        if not queue or not queue.current_item:
            return
        duration = min(queue.current_item.duration or 0, 3600)
        title = queue.current_item.name
        artist = ""
        album = ""
        if queue.current_item.streamdetails and queue.current_item.streamdetails.stream_title:
            # stream title from radio station
            stream_title = queue.current_item.streamdetails.stream_title
            if " - " in stream_title:
                artist, title = stream_title.split(" - ", 1)
            else:
                title = stream_title
            # set album to radio station name
            album = queue.current_item.name
        elif media_item := queue.current_item.media_item:
            title = media_item.name
            if artist_str := getattr(media_item, "artist_str", None):
                artist = artist_str
            if _album := getattr(media_item, "album", None):
                album = _album.name

        cmd = f"TITLE={title or 'Music Assistant'}\nARTIST={artist}\nALBUM={album}\n"
        cmd += f"DURATION={duration}\nPROGRESS=0\nACTION=SENDMETA\n"

        await self.send_cli_command(cmd)

        # get image
        if not queue.current_item.image:
            return

        # the image format needs to be 500x500 jpeg for maximum compatibility with players
        image_url = self.mass.metadata.get_image_url(
            queue.current_item.image, size=500, prefer_proxy=True, image_format="jpeg"
        )
        await self.send_cli_command(f"ARTWORK={image_url}\n")

    async def _send_progress(self, queue: PlayerQueue) -> None:
        """Send progress report to player (and connected sync childs)."""
        if not queue or not queue.current_item:
            return
        progress = int(queue.corrected_elapsed_time)
        await self.send_cli_command(f"PROGRESS={progress}\n")
