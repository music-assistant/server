"""Logic for AirPlay 2 audio streaming to AirPlay devices."""

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

from music_assistant_models.enums import PlaybackState
from music_assistant_models.errors import PlayerCommandFailed

from music_assistant.constants import CONF_SYNC_ADJUST, VERBOSE_LOG_LEVEL
from music_assistant.helpers.audio import get_chunksize, get_player_filter_params
from music_assistant.helpers.ffmpeg import FFMpeg
from music_assistant.helpers.process import AsyncProcess, check_output
from music_assistant.helpers.util import TaskManager, close_async_generator

from .constants import (
    AIRPLAY_PCM_FORMAT,
    CONF_READ_AHEAD_BUFFER,
)
from .helpers import get_cli_binary

if TYPE_CHECKING:
    from music_assistant_models.media_items import AudioFormat
    from music_assistant_models.player_queue import PlayerQueue

    from .player import AirPlayPlayer
    from .provider import AirPlayProvider


class AirPlay2StreamSession:
    """Object that holds the details of an AirPlay2 stream session to one or more players."""

    def __init__(
        self,
        airplay_provider: AirPlayProvider,
        sync_clients: list[AirPlayPlayer],
        input_format: AudioFormat,
        audio_source: AsyncGenerator[bytes, None],
    ) -> None:
        """Initialize AirPlay2StreamSession."""
        assert sync_clients
        self.prov = airplay_provider
        self.mass = airplay_provider.mass
        self.input_format = input_format
        self.sync_clients = sync_clients
        self._audio_source = audio_source
        self._audio_source_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        """Initialize AirPlay2StreamSession."""
        # initialize airplay stream for all players

        # get current ntp and start AirPlay2Stream per player
        cli_bin = await get_cli_binary(2)
        self.prov.logger.debug("Using AirPlay2 CLI binary %s", cli_bin)
        _, stdout = await check_output(cli_bin, "--ntp")
        self.prov.logger.debug(f"Output from ntp check: {stdout.decode().strip()}")
        start_ntp = int(stdout.strip())
        wait_start = 1750 + (250 * len(self.sync_clients))

        async def _start_client(airplay2_player: AirPlayPlayer) -> None:
            # stop existing stream if running
            if airplay2_player.stream and airplay2_player.stream.running:
                await airplay2_player.stream.stop()

            airplay2_player.stream = AirPlay2Stream(self, airplay2_player)
            await airplay2_player.stream.start(start_ntp, wait_start)

        async with TaskManager(self.mass) as tm:
            for _airplay2_player in self.sync_clients:
                tm.create_task(_start_client(_airplay2_player))
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
        assert airplay_player.stream
        assert airplay_player.stream.session == self
        async with self._lock:
            self.sync_clients.remove(airplay_player)
        await airplay_player.stream.stop()
        airplay_player.stream = None
        # if this was the last client, stop the session
        if not self.sync_clients:
            await self.stop()
            return

    async def add_client(self, airplay_player: AirPlayPlayer) -> None:
        """Add a sync client to the session."""
        # TODO: Add the ability to add a new client to an existing session
        # e.g. by counting the number of frames sent etc.

        # temp solution: just restart the whole playback session when new client(s) join
        sync_leader = self.sync_clients[0]
        if not sync_leader.stream or not sync_leader.stream.running:
            return

        await self.stop()  # we need to stop the current session to add a new client
        # this could potentially be called by multiple players at the exact same time
        # so we debounce the resync a bit here with a timer
        if sync_leader.current_media:
            self.mass.call_later(
                0.5,
                self.mass.players.cmd_resume(sync_leader.player_id),
                task_id=f"resync_session_{sync_leader.player_id}",
            )

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
            if not sync_client.stream:
                continue  # guard
            sync_client.stream.start_ffmpeg_stream()

    async def _audio_streamer(self) -> None:
        """Stream audio to all players."""
        generator_exhausted = False
        try:
            async for chunk in self._audio_source:
                async with self._lock:
                    sync_clients = [x for x in self.sync_clients if x.stream and x.stream.running]
                    if not sync_clients:
                        return
                    await asyncio.gather(
                        *[x.stream.write_chunk(chunk) for x in sync_clients if x.stream],
                        return_exceptions=True,
                    )
            # entire stream consumed: send EOF
            generator_exhausted = True
            async with self._lock:
                await asyncio.gather(
                    *[
                        x.stream.write_eof()
                        for x in self.sync_clients
                        if x.stream and x.stream.running
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


class AirPlay2Stream:
    """
    AirPlay 2 Audio Streamer.

    Python is not suitable for realtime audio streaming so we do the actual streaming
    of audio using a small executable written in C based on owntones to do
    the actual timestamped playback. It reads pcm audio from a named pipe
    and we can send some interactive commands using another named pipe.
    """

    def __init__(
        self,
        session: AirPlay2StreamSession,
        player: AirPlayPlayer,
    ) -> None:
        """Initialize AirPlay2Stream."""
        self.session = session
        self.prov = session.prov
        self.mass = session.prov.mass
        self.player = player

        # always generate a new active remote id to prevent race conditions
        # with the named pipes used to send audio and metadata
        # include player_id to reduce risk of duplicate simultaneous random
        # numbers generated for two different players.
        self.active_remote_id: str = str(randint(1000, 8000))
        self.metadata_named_pipe = (
            f"/tmp/ap2-{self.player.player_id}-{self.active_remote_id}.metadata"  # noqa: S108
        )
        self.audio_named_pipe = f"/tmp/ap2-{self.player.player_id}-{self.active_remote_id}"  # noqa: S108
        self.prevent_playback: bool = False
        self._stderr_reader_task: asyncio.Task[None] | None = None
        self._cli_proc: AsyncProcess | None = None
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
            and self._cli_proc is not None
            and not self._cli_proc.closed
        )

    @property
    def _cli_loglevel(self) -> int:
        """Return a cliap2 aligned loglevel."""
        match self.prov.logger.level:
            case logging.CRITICAL:
                return 0
            case logging.ERROR:
                return 1
            case logging.WARNING:
                return 2
            case logging.INFO:
                return 3
            case logging.DEBUG:
                return 4
        if self.prov.logger.isEnabledFor(VERBOSE_LOG_LEVEL):
            return 5
        return 1  # guard: should never happen

    async def start(self, start_ntp: int, wait_start: int = 1000) -> None:
        """Initialize CLI process for a player."""
        assert self.player.cli_bin
        assert self.player.airplay_discovery_info is not None
        # Setup named pipes
        try:
            os.mkfifo(self.audio_named_pipe)
            self.player.logger.debug(f"{self.audio_named_pipe} created")
        except FileExistsError:
            self.player.logger.warning(f"Named pipe {self.audio_named_pipe} already exists.")
        except Exception as e:
            self.player.logger.error(
                f"Error {e} attempting to create named pipe {self.audio_named_pipe}"
            )
        try:
            os.mkfifo(self.metadata_named_pipe)
            self.player.logger.debug(f"{self.metadata_named_pipe} created")
        except FileExistsError:
            self.player.logger.warning(f"Named pipe {self.metadata_named_pipe} already exists.")
        except Exception as e:
            self.player.logger.error(
                f"Error {e} attempting to create named pipe {self.metadata_named_pipe}"
            )

        player_id = self.player.player_id
        sync_adjust = self.mass.config.get_raw_player_config_value(player_id, CONF_SYNC_ADJUST, 0)
        assert isinstance(sync_adjust, int)
        read_ahead = await self.mass.config.get_player_config_value(
            player_id, CONF_READ_AHEAD_BUFFER
        )

        txt_kv: str = ""
        for key, value in self.player.airplay_discovery_info.decoded_properties.items():
            txt_kv += f'"{key}={value}" '

        # ffmpeg handles the player specific stream + filters and pipes
        # audio to the cliap2 process
        self.start_ffmpeg_stream()

        # cliap2 is the binary that handles the actual streaming to the player
        # this binary leverages from the AirPlay2 support in owntones
        # https://github.com/music-assistant/cliairplay
        cli_args = [
            str(self.player.cli_bin),
            "--config",
            os.path.join(os.path.dirname(__file__), "bin", "cliap2.conf"),
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
            "--wait",
            str(wait_start - sync_adjust),
            "--latency",
            str(read_ahead),
            "--volume",
            str(self.player.volume_level),
            "--loglevel",
            str(self._cli_loglevel),
            "--pipe",
            self.audio_named_pipe,
        ]
        self.player.logger.debug(
            "Starting cliap2 process for player %s with args: %s",
            player_id,
            cli_args,
        )
        self._cli_proc = AsyncProcess(cli_args, stdin=True, stderr=True, name="cliap2")
        if platform.system() == "Darwin":
            os.environ["DYLD_LIBRARY_PATH"] = "/usr/local/lib"
        await self._cli_proc.start()
        # read up to first num_lines lines of stderr to get the initial status
        num_lines: int = 50
        if self.prov.logger.level > logging.INFO:
            num_lines *= 10
        for _ in range(num_lines):
            line = (await self._cli_proc.read_stderr()).decode("utf-8", errors="ignore")
            self.player.logger.debug(line)
            if "airplay: Adding AirPlay device " in line:
                self.player.logger.info("AirPlay device connected. Starting playback.")
                self._started.set()
                break
            # TODO: @bradkeifer to confirm the error message upon connect failure
            if "Cannot connect to AirPlay device" in line:
                if self._ffmpeg_reader_task:
                    self._ffmpeg_reader_task.cancel()
                raise PlayerCommandFailed("Cannot connect to AirPlay device")
        # repeat sending the volume level to the player because some players seem
        # to ignore it the first time
        # https://github.com/music-assistant/support/issues/3330
        # await self.send_cli_command(f"VOLUME={self.player.volume_level}\n")
        # start reading the stderr of the cliap2 process from another task
        self._stderr_reader_task = self.mass.create_task(self._stderr_reader())

    async def stop(self) -> None:
        """Stop playback and cleanup."""
        await self.send_cli_command("ACTION=STOP")
        self._stopped = True
        if self._stderr_reader_task and not self._stderr_reader_task.done():
            self._stderr_reader_task.cancel()
        if self._ffmpeg_reader_task and not self._ffmpeg_reader_task.done():
            self._ffmpeg_reader_task.cancel()
        if self._cli_proc and not self._cli_proc.closed:
            await self._cli_proc.close(True)
        if self._ffmpeg_proc and not self._ffmpeg_proc.closed:
            await self._ffmpeg_proc.close(True)
        try:
            os.remove(self.audio_named_pipe)
            self.player.logger.debug(f"{self.audio_named_pipe} removed")
        except Exception as e:
            self.player.logger.error(
                f"Error {e} attempting to remove named pipe {self.audio_named_pipe}"
            )
        try:
            os.remove(self.metadata_named_pipe)
            self.player.logger.debug(f"{self.metadata_named_pipe} removed")
        except Exception as e:
            self.player.logger.error(
                f"Error {e} attempting to remove named pipe {self.metadata_named_pipe}"
            )
        self.player.set_state_from_stream(state=PlaybackState.IDLE, elapsed_time=0)

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
        """Send an interactive command to the running CLIap2 binary."""
        if self._stopped or not self._cli_proc or self._cli_proc.closed:
            return
        await self._started.wait()

        if not command.endswith("\n"):
            command += "\n"

        def send_data() -> None:
            with suppress(BrokenPipeError), open(self.metadata_named_pipe, "w") as f:
                f.write(command)

        self.player.logger.log(VERBOSE_LOG_LEVEL, "sending command %s", command)
        self.player.last_command_sent = time.time()
        await asyncio.to_thread(send_data)

    def start_ffmpeg_stream(self) -> None:
        """Start (or replace) the player-specific ffmpeg stream to feed cliap2."""
        # cancel existing ffmpeg reader task
        if self._ffmpeg_reader_task and not self._ffmpeg_reader_task.done():
            self._ffmpeg_reader_task.cancel()
        if self._ffmpeg_proc and not self._ffmpeg_proc.closed:
            self.mass.create_task(self._ffmpeg_proc.close(True))
        # start new ffmpeg reader task
        self._ffmpeg_reader_task = self.mass.create_task(self._ffmpeg_reader())

    async def _ffmpeg_reader(self) -> None:
        """Read audio from the audio source and pipe it to the named pipe towards cliap2."""
        self._ffmpeg_proc = FFMpeg(
            audio_input="-",
            input_format=self.session.input_format,
            output_format=AIRPLAY_PCM_FORMAT,
            filter_params=get_player_filter_params(
                self.mass,
                self.player.player_id,
                self.session.input_format,
                AIRPLAY_PCM_FORMAT,
            ),
        )
        self._stream_bytes_sent = 0
        await self._ffmpeg_proc.start()
        chunksize = get_chunksize(AIRPLAY_PCM_FORMAT)
        # wait for cliap2 to be ready
        await asyncio.wait_for(self._started.wait(), 20)
        chunk: bytes = b"0"
        async for chunk in self._ffmpeg_proc.iter_chunked(chunksize):

            def send_audio(audio_chunk: bytes) -> int:
                with suppress(BrokenPipeError), open(self.audio_named_pipe, "wb") as f:
                    return f.write(audio_chunk)
                return 0

            if self._stopped:
                break
            if not self._cli_proc or self._cli_proc.closed:
                break
            # cliap2 reads audio input from a named pipe
            await asyncio.to_thread(send_audio, chunk)
            self._stream_bytes_sent += len(chunk)
            self._total_bytes_sent += len(chunk)
            del chunk
            # we base elapsed time on the amount of bytes sent
            # so we can account for reusing the same session for multiple streams
            self.player.set_state_from_stream(
                elapsed_time=self._stream_bytes_sent / chunksize,
            )
        # if we reach this point, the process exited, most likely because the stream ended
        if self._cli_proc and not self._cli_proc.closed:
            await self._cli_proc.write_eof()

    async def _stderr_reader(self) -> None:
        """Monitor stderr for the running CLIap2 process."""
        player = self.player
        queue = self.mass.players.get_active_queue(player)
        logger = player.logger
        lost_packets = 0
        prev_metadata_checksum: str = ""
        prev_progress_report: float = 0
        if not self._cli_proc:
            return
        async for line in self._cli_proc.iter_stderr():
            # TODO @bradkeifer make cliap2 work this way
            if "elapsed milliseconds:" in line:
                # this is received more or less every second while playing
                # millis = int(line.split("elapsed milliseconds: ")[1])
                # self.player.elapsed_time = (millis / 1000) - self.elapsed_time_correction
                # self.player.elapsed_time_last_updated = time.time()
                # send metadata to player(s) if needed
                # NOTE: this must all be done in separate tasks to not disturb audio
                now = time.time()
                if (
                    (player.elapsed_time or 0) > 2
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
                player.set_state_from_stream(state=PlaybackState.PAUSED)
            if "Restarted at" in line or "restarting w/ pause" in line:
                player.set_state_from_stream(state=PlaybackState.PLAYING)
            if "restarting w/o pause" in line:
                # streaming has started
                player.set_state_from_stream(state=PlaybackState.PLAYING, elapsed_time=0)
            if "lost packet out of backlog" in line:
                lost_packets += 1
                if lost_packets == 100 and queue:
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
