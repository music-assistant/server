"""Logic for RAOP (AirPlay 1) audio streaming to AirPlay devices."""

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

from music_assistant_models.enums import PlayerState
from music_assistant_models.errors import PlayerCommandFailed

from music_assistant.constants import CONF_SYNC_ADJUST, VERBOSE_LOG_LEVEL
from music_assistant.helpers.audio import get_chunksize, get_player_filter_params
from music_assistant.helpers.ffmpeg import FFMpeg
from music_assistant.helpers.process import AsyncProcess, check_output
from music_assistant.helpers.util import TaskManager, close_async_generator

from .const import (
    AIRPLAY_PCM_FORMAT,
    CONF_ALAC_ENCODE,
    CONF_BIND_INTERFACE,
    CONF_ENCRYPTION,
    CONF_PASSWORD,
    CONF_READ_AHEAD_BUFFER,
)

if TYPE_CHECKING:
    from music_assistant_models.media_items import AudioFormat
    from music_assistant_models.player_queue import PlayerQueue

    from .player import AirPlayPlayer
    from .provider import AirPlayProvider


class RaopStreamSession:
    """Object that holds the details of a (RAOP) stream session to one or more players."""

    def __init__(
        self,
        airplay_provider: AirPlayProvider,
        sync_clients: list[AirPlayPlayer],
        input_format: AudioFormat,
        audio_source: AsyncGenerator[bytes, None],
    ) -> None:
        """Initialize RaopStreamSession."""
        assert sync_clients
        self.prov = airplay_provider
        self.mass = airplay_provider.mass
        self.input_format = input_format
        self.sync_clients = sync_clients
        self._audio_source = audio_source
        self._audio_source_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        """Initialize RaopStreamSession."""
        # initialize raop stream for all players

        # get current ntp and start RaopStream per player
        assert self.prov.cliraop_bin
        _, stdout = await check_output(self.prov.cliraop_bin, "-ntp")
        start_ntp = int(stdout.strip())
        wait_start = 1750 + (250 * len(self.sync_clients))

        async def _start_client(raop_player: AirPlayPlayer) -> None:
            # stop existing stream if running
            if raop_player.raop_stream and raop_player.raop_stream.running:
                await raop_player.raop_stream.stop()

            raop_player.raop_stream = RaopStream(self, raop_player)
            await raop_player.raop_stream.start(start_ntp, wait_start)

        async with TaskManager(self.mass) as tm:
            for _raop_player in self.sync_clients:
                tm.create_task(_start_client(_raop_player))
        self._audio_source_task = asyncio.create_task(self._audio_streamer())

    async def stop(self) -> None:
        """Stop playback and cleanup."""
        if self._audio_source_task and not self._audio_source_task.done():
            self._audio_source_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._audio_source_task
        await asyncio.gather(
            *[self.remove_client(x) for x in self.sync_clients],
            return_exceptions=True,
        )

    async def remove_client(self, airplay_player: AirPlayPlayer) -> None:
        """Remove a sync client from the session."""
        if airplay_player not in self.sync_clients:
            return
        assert airplay_player.raop_stream
        assert airplay_player.raop_stream.session == self
        async with self._lock:
            self.sync_clients.remove(airplay_player)
        await airplay_player.raop_stream.stop()
        airplay_player.raop_stream = None
        # if this was the last client, stop the session
        if not self.sync_clients:
            await self.stop()
            return

    async def add_client(self, airplay_player: AirPlayPlayer) -> None:
        """Add a sync client to the session."""
        # TODO: Add the ability to add a new client to an existing session
        # e.g. by counting the number of frames sent etc.
        raise NotImplementedError("Adding clients to a session is not yet supported")

    async def replace_stream(self, audio_source: AsyncGenerator[bytes, None]) -> None:
        """Replace the audio source of the stream."""
        # cancel the current audio source task
        assert self._audio_source_task  # for type checker
        self._audio_source_task.cancel()
        with suppress(asyncio.CancelledError):
            await self._audio_source_task
        # set new audio source and restart the stream
        self._audio_source = audio_source
        self._audio_source_task = asyncio.create_task(self._audio_streamer())
        # restart the (player-specific) ffmpeg stream for all players
        # this is the easiest way to ensure the new audio source is used
        # as quickly as possible, without waiting for the buffers to be drained
        # it also allows to change the player settings such as DSP on the fly
        for sync_client in self.sync_clients:
            if not sync_client.raop_stream:
                continue  # guard
            sync_client.raop_stream.start_ffmpeg_stream()

    async def _audio_streamer(self) -> None:
        """Stream audio to all players."""
        generator_exhausted = False
        try:
            async for chunk in self._audio_source:
                async with self._lock:
                    sync_clients = [
                        x for x in self.sync_clients if x.raop_stream and x.raop_stream.running
                    ]
                    if not sync_clients:
                        return
                    await asyncio.gather(
                        *[x.raop_stream.write_chunk(chunk) for x in sync_clients if x.raop_stream],
                        return_exceptions=True,
                    )
            # entire stream consumed: send EOF
            generator_exhausted = True
            async with self._lock:
                await asyncio.gather(
                    *[
                        x.raop_stream.write_eof()
                        for x in self.sync_clients
                        if x.raop_stream and x.raop_stream.running
                    ],
                    return_exceptions=True,
                )
        except Exception as err:
            logger = self.prov.logger
            logger.error(
                "Stream error: %s",
                str(err) or err.__class__.__name__,
                exc_info=err if logger.isEnabledFor(logging.DEBUG) else None,
            )
            raise
        finally:
            if not generator_exhausted:
                await close_async_generator(self._audio_source)


class RaopStream:
    """
    RAOP (AirPlay 1) Audio Streamer.

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
        self._stderr_reader_task: asyncio.Task[None] | None = None
        self._cliraop_proc: AsyncProcess | None = None
        self._ffmpeg_proc: AsyncProcess | None = None
        self._ffmpeg_reader_task: asyncio.Task[None] | None = None
        self._started = asyncio.Event()
        self._stopped = False
        self._total_bytes_sent = 0
        self._stream_bytes_sent = 0

    @property
    def running(self) -> bool:
        """Return boolean if this stream is running."""
        return (
            not self._stopped
            and self._started.is_set()
            and self._cliraop_proc is not None
            and not self._cliraop_proc.closed
        )

    async def start(self, start_ntp: int, wait_start: int = 1000) -> None:
        """Initialize CLIRaop process for a player."""
        assert self.prov.cliraop_bin
        extra_args: list[str] = []
        player_id = self.airplay_player.player_id
        mass_player = self.mass.players.get(player_id)
        if not mass_player:
            return
        bind_ip = str(
            await self.mass.config.get_provider_config_value(
                self.prov.instance_id, CONF_BIND_INTERFACE
            )
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
        assert isinstance(sync_adjust, int)
        if device_password := self.mass.config.get_raw_player_config_value(
            player_id, CONF_PASSWORD, None
        ):
            extra_args += ["-password", str(device_password)]
        if self.prov.logger.isEnabledFor(logging.DEBUG):
            extra_args += ["-debug", "5"]
        elif self.prov.logger.isEnabledFor(VERBOSE_LOG_LEVEL):
            extra_args += ["-debug", "10"]
        read_ahead = await self.mass.config.get_player_config_value(
            player_id, CONF_READ_AHEAD_BUFFER
        )
        # ffmpeg handles the player specific stream + filters and pipes
        # audio to the cliraop process
        self.start_ffmpeg_stream()

        # cliraop is the binary that handles the actual raop streaming to the player
        # this is a slightly modified version of philippe44's libraop
        # https://github.com/music-assistant/libraop
        # we use this intermediate binary to do the actual streaming because attempts to do
        # so using pure python (e.g. pyatv) were not successful due to the realtime nature
        # TODO: Either enhance libraop with airplay 2 support or find a better alternative
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
        self._cliraop_proc = AsyncProcess(cliraop_args, stdin=True, stderr=True, name="cliraop")
        if platform.system() == "Darwin":
            os.environ["DYLD_LIBRARY_PATH"] = "/usr/local/lib"
        await self._cliraop_proc.start()
        # read first 20 lines of stderr to get the initial status
        for _ in range(20):
            line = (await self._cliraop_proc.read_stderr()).decode("utf-8", errors="ignore")
            self.airplay_player.logger.debug(line)
            if "connected to " in line:
                self._started.set()
                break
            if "Cannot connect to AirPlay device" in line:
                if self._ffmpeg_reader_task:
                    self._ffmpeg_reader_task.cancel()
                raise PlayerCommandFailed("Cannot connect to AirPlay device")
        # repeat sending the volume level to the player because some players seem
        # to ignore it the first time
        # https://github.com/music-assistant/support/issues/3330
        await self.send_cli_command(f"VOLUME={mass_player.volume_level}\n")
        # start reading the stderr of the cliraop process from another task
        self._stderr_reader_task = self.mass.create_task(self._stderr_reader())

    async def stop(self) -> None:
        """Stop playback and cleanup."""
        await self.send_cli_command("ACTION=STOP")
        self._stopped = True
        if self._stderr_reader_task and not self._stderr_reader_task.done():
            self._stderr_reader_task.cancel()
        if self._ffmpeg_reader_task and not self._ffmpeg_reader_task.done():
            self._ffmpeg_reader_task.cancel()
        if self._cliraop_proc and not self._cliraop_proc.closed:
            await self._cliraop_proc.close(True)
        if self._ffmpeg_proc and not self._ffmpeg_proc.closed:
            await self._ffmpeg_proc.close(True)
        if mass_player := self.mass.players.get(self.airplay_player.player_id):
            mass_player.state = PlayerState.IDLE
            self.mass.players.update(mass_player.player_id)

    async def write_chunk(self, chunk: bytes) -> None:
        """Write a (pcm) audio chunk."""
        if self._stopped:
            raise RuntimeError("Stream is already stopped")
        await self._started.wait()
        assert self._ffmpeg_proc
        await self._ffmpeg_proc.write(chunk)

    async def write_eof(self) -> None:
        """Write EOF."""
        if self._stopped:
            raise RuntimeError("Stream is already stopped")
        await self._started.wait()
        assert self._ffmpeg_proc
        await self._ffmpeg_proc.write_eof()

    async def send_cli_command(self, command: str) -> None:
        """Send an interactive command to the running CLIRaop binary."""
        if self._stopped or not self._cliraop_proc or self._cliraop_proc.closed:
            return
        await self._started.wait()

        if not command.endswith("\n"):
            command += "\n"

        def send_data() -> None:
            with suppress(BrokenPipeError), open(named_pipe, "w") as f:
                f.write(command)

        named_pipe = f"/tmp/raop-{self.active_remote_id}"  # noqa: S108
        self.airplay_player.logger.log(VERBOSE_LOG_LEVEL, "sending command %s", command)
        self.airplay_player.last_command_sent = time.time()
        await asyncio.to_thread(send_data)

    def start_ffmpeg_stream(self) -> None:
        """Start (or replace) the player-specific ffmpeg stream to feed cliraop."""
        # cancel existing ffmpeg reader task
        if self._ffmpeg_reader_task and not self._ffmpeg_reader_task.done():
            self._ffmpeg_reader_task.cancel()
        if self._ffmpeg_proc and not self._ffmpeg_proc.closed:
            self.mass.create_task(self._ffmpeg_proc.close(True))
        # start new ffmpeg reader task
        self._ffmpeg_reader_task = self.mass.create_task(self._ffmpeg_reader())

    async def _ffmpeg_reader(self) -> None:
        """Read audio from the audio source and pipe it to the CLIRaop process."""
        self._ffmpeg_proc = FFMpeg(
            audio_input="-",
            input_format=self.session.input_format,
            output_format=AIRPLAY_PCM_FORMAT,
            filter_params=get_player_filter_params(
                self.mass,
                self.airplay_player.player_id,
                self.session.input_format,
                AIRPLAY_PCM_FORMAT,
            ),
        )
        self._stream_bytes_sent = 0
        mass_player = self.mass.players.get(self.airplay_player.player_id)
        assert mass_player  # for type checker
        await self._ffmpeg_proc.start()
        chunksize = get_chunksize(AIRPLAY_PCM_FORMAT)
        # wait for cliraop to be ready
        await asyncio.wait_for(self._started.wait(), 20)
        async for chunk in self._ffmpeg_proc.iter_chunked(chunksize):
            if self._stopped:
                break
            if not self._cliraop_proc or self._cliraop_proc.closed:
                break
            await self._cliraop_proc.write(chunk)
            self._stream_bytes_sent += len(chunk)
            self._total_bytes_sent += len(chunk)
            del chunk
            # we base elapsed time on the amount of bytes sent
            # so we can account for reusing the same session for multiple streams
            mass_player.elapsed_time = self._stream_bytes_sent / chunksize
            mass_player.elapsed_time_last_updated = time.time()
        # if we reach this point, the process exited, most likely because the stream ended
        if self._cliraop_proc and not self._cliraop_proc.closed:
            await self._cliraop_proc.write_eof()

    async def _stderr_reader(self) -> None:
        """Monitor stderr for the running CLIRaop process."""
        airplay_player = self.airplay_player
        mass_player = self.mass.players.get(airplay_player.player_id)
        if not mass_player or not mass_player.active_source:
            return
        queue = self.mass.player_queues.get_active_queue(mass_player.active_source)
        logger = airplay_player.logger
        lost_packets = 0
        prev_metadata_checksum: str = ""
        prev_progress_report: float = 0
        if not self._cliraop_proc:
            return
        async for line in self._cliraop_proc.iter_stderr():
            if "elapsed milliseconds:" in line:
                # this is received more or less every second while playing
                # millis = int(line.split("elapsed milliseconds: ")[1])
                # mass_player.elapsed_time = (millis / 1000) - self.elapsed_time_correction
                # mass_player.elapsed_time_last_updated = time.time()
                # send metadata to player(s) if needed
                # NOTE: this must all be done in separate tasks to not disturb audio
                now = time.time()
                if (
                    (mass_player.elapsed_time or 0) > 2
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
                    self.mass.create_task(self.mass.player_queues.resume(queue.queue_id, False))
                else:
                    logger.warning("Packet loss detected!")
            if "end of stream reached" in line:
                logger.debug("End of stream reached")
                break
            logger.log(VERBOSE_LOG_LEVEL, line)

        # ensure we're cleaned up afterwards (this also logs the returncode)
        await self.stop()

    async def _send_metadata(self, queue: PlayerQueue) -> None:
        """Send metadata to player (and connected sync childs)."""
        if not queue or not queue.current_item or self._stopped:
            return
        duration = min(queue.current_item.duration or 0, 3600)
        title = queue.current_item.name
        artist = ""
        album = ""
        if queue.current_item.streamdetails and queue.current_item.streamdetails.stream_title:
            # stream title/metadata from radio/live stream
            if " - " in queue.current_item.streamdetails.stream_title:
                artist, title = queue.current_item.streamdetails.stream_title.split(" - ", 1)
            else:
                title = queue.current_item.streamdetails.stream_title
                artist = ""
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
        if not queue.current_item.image or self._stopped:
            return

        # the image format needs to be 500x500 jpeg for maximum compatibility with players
        image_url = self.mass.metadata.get_image_url(
            queue.current_item.image, size=500, prefer_proxy=True, image_format="jpeg"
        )
        await self.send_cli_command(f"ARTWORK={image_url}\n")

    async def _send_progress(self, queue: PlayerQueue) -> None:
        """Send progress report to player (and connected sync childs)."""
        if not queue or not queue.current_item or self._stopped:
            return
        progress = int(queue.corrected_elapsed_time)
        await self.send_cli_command(f"PROGRESS={progress}\n")
