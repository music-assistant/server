"""Base protocol class for AirPlay streaming implementations."""

from __future__ import annotations

import asyncio
import os
import time
from abc import ABC, abstractmethod
from contextlib import suppress
from random import randint
from typing import TYPE_CHECKING

from music_assistant_models.enums import ContentType, PlaybackState
from music_assistant_models.media_items import AudioFormat

from music_assistant.constants import VERBOSE_LOG_LEVEL
from music_assistant.helpers.namedpipe import AsyncNamedPipeWriter

if TYPE_CHECKING:
    from music_assistant_models.player import PlayerMedia

    from music_assistant.helpers.process import AsyncProcess
    from music_assistant.providers.airplay.player import AirPlayPlayer
    from music_assistant.providers.airplay.stream_session import AirPlayStreamSession


class AirPlayProtocol(ABC):
    """Base class for AirPlay streaming protocols (RAOP and AirPlay2).

    This class contains common logic shared between protocol implementations,
    with abstract methods for protocol-specific behavior.
    """

    session: AirPlayStreamSession | None = None  # reference to the active stream session (if any)

    # the pcm audio format used for streaming to this protocol
    pcm_format = AudioFormat(
        content_type=ContentType.PCM_S16LE, sample_rate=44100, bit_depth=16, channels=2
    )
    supports_pairing = False  # whether this protocol supports pairing
    is_pairing: bool = False  # whether this protocol instance is in pairing mode

    def __init__(
        self,
        player: AirPlayPlayer,
    ) -> None:
        """Initialize base AirPlay protocol.

        Args:
            player: The player to stream to
        """
        self.prov = player.provider
        self.mass = player.provider.mass
        self.player = player
        # Generate unique ID to prevent race conditions with named pipes
        self.active_remote_id: str = str(randint(1000, 8000))
        self.prevent_playback: bool = False
        self._cli_proc: AsyncProcess | None = None
        # State tracking
        self._started = asyncio.Event()
        self._stopped = False
        self._total_bytes_sent = 0
        self._stream_bytes_sent = 0
        self.audio_named_pipe = (
            f"/tmp/{player.protocol.value}-{self.player.player_id}-{self.active_remote_id}-audio"  # noqa: S108
        )
        self.commands_named_pipe = (
            f"/tmp/{player.protocol.value}-{self.player.player_id}-{self.active_remote_id}-cmd"  # noqa: S108
        )
        # Async named pipe writers (kept open for session duration)
        self._audio_pipe: AsyncNamedPipeWriter | None = None
        self._commands_pipe: AsyncNamedPipeWriter | None = None

    @property
    def running(self) -> bool:
        """Return boolean if this stream is running."""
        return (
            not self._stopped
            and self._started.is_set()
            and self._cli_proc is not None
            and not self._cli_proc.closed
        )

    @abstractmethod
    async def get_ntp(self) -> int:
        """Get current NTP timestamp from the CLI binary."""
        # this can probably be removed now that we already get the ntp
        # in python (within the stream session start)

    @abstractmethod
    async def start(self, start_ntp: int, skip: int = 0) -> None:
        """Initialize streaming process for the player.

        Args:
            start_ntp: NTP timestamp to start streaming
            skip: Number of seconds to skip (for late joiners)
        """

    async def start_pairing(self) -> None:
        """Start pairing process for this protocol (if supported)."""
        raise NotImplementedError("Pairing not implemented for this protocol")

    async def finish_pairing(self, pin: str) -> str:
        """Finish pairing process with given PIN (if supported)."""
        raise NotImplementedError("Pairing not implemented for this protocol")

    async def _open_pipes(self) -> None:
        """Open both named pipes in non-blocking mode for async I/O."""
        # Create named pipes first if they don't exist
        await asyncio.to_thread(self._create_named_pipe, self.audio_named_pipe)
        await asyncio.to_thread(self._create_named_pipe, self.commands_named_pipe)

        # Open audio pipe with buffer size optimization
        self._audio_pipe = AsyncNamedPipeWriter(self.audio_named_pipe, logger=self.player.logger)
        await self._audio_pipe.open(increase_buffer=True)

        # Open command pipe (no need to increase buffer for small commands)
        self._commands_pipe = AsyncNamedPipeWriter(
            self.commands_named_pipe, logger=self.player.logger
        )
        await self._commands_pipe.open(increase_buffer=False)

        self.player.logger.debug("Named pipes opened in non-blocking mode for streaming session")

    def _create_named_pipe(self, pipe_path: str) -> None:
        """Create a named pipe (FIFO) if it doesn't exist."""
        if not os.path.exists(pipe_path):
            os.mkfifo(pipe_path)

    async def stop(self) -> None:
        """Stop playback and cleanup."""
        # Send stop command before setting _stopped flag
        await self.send_cli_command("ACTION=STOP")

        self._stopped = True

        # Close named pipes (sends EOF to C side, triggering graceful shutdown)
        if self._audio_pipe is not None:
            await self._audio_pipe.close()
            self._audio_pipe = None

        if self._commands_pipe is not None:
            await self._commands_pipe.close()
            self._commands_pipe = None

        # Close the CLI process (wait for it to terminate)
        if self._cli_proc and not self._cli_proc.closed:
            await self._cli_proc.close(True)

        self.player.set_state_from_stream(state=PlaybackState.IDLE, elapsed_time=0)

        # Remove named pipes from filesystem
        with suppress(Exception):
            await asyncio.to_thread(os.remove, self.audio_named_pipe)
        with suppress(Exception):
            await asyncio.to_thread(os.remove, self.commands_named_pipe)

    async def write_chunk(self, chunk: bytes) -> None:
        """
        Write a (pcm) audio chunk to the stream.

        Writes one second worth of audio data based on the pcm format.
        Uses non-blocking I/O with asyncio event loop (no thread pool consumption).
        """
        if self._audio_pipe is None or not self._audio_pipe.is_open:
            return

        pipe_write_start = time.time()

        try:
            await self._audio_pipe.write(chunk)
        except TimeoutError as e:
            # Re-raise with player context
            raise TimeoutError(f"Player {self.player.player_id}: {e}") from e

        pipe_write_elapsed = time.time() - pipe_write_start

        # Log only truly abnormal pipe writes (>5s indicates a real stall)
        # Normal writes take ~1s due to pipe rate-limiting to playback speed
        # Can take up to ~4s if player's latency buffer is full
        if pipe_write_elapsed > 5.0:
            self.player.logger.error(
                "!!! STALLED PIPE WRITE: Player %s took %.3fs to write %d bytes to pipe",
                self.player.player_id,
                pipe_write_elapsed,
                len(chunk),
            )

    async def write_eof(self) -> None:
        """Write EOF to signal end of stream."""
        # default implementation simply closes the named pipe
        # can be overridden with protocol specific implementation if needed
        if self._audio_pipe is not None:
            await self._audio_pipe.close()
            self._audio_pipe = None

    async def send_cli_command(self, command: str) -> None:
        """Send an interactive command to the running CLI binary using non-blocking I/O."""
        if self._stopped or not self._cli_proc or self._cli_proc.closed:
            return
        if self._commands_pipe is None or not self._commands_pipe.is_open:
            return

        await self._started.wait()

        if not command.endswith("\n"):
            command += "\n"

        self.player.logger.log(VERBOSE_LOG_LEVEL, "sending command %s", command)
        self.player.last_command_sent = time.time()

        # Write command to pipe
        data = command.encode("utf-8")

        with suppress(BrokenPipeError):
            try:
                # Use shorter timeout for commands (1 second per wait iteration)
                await self._commands_pipe.write(data, timeout_per_wait=1.0)
            except TimeoutError:
                self.player.logger.warning("Command pipe write timeout for %s", command.strip())

    async def send_metadata(self, progress: int | None, metadata: PlayerMedia | None) -> None:
        """Send metadata to player."""
        if self._stopped:
            return
        if metadata:
            duration = min(metadata.duration or 0, 3600)
            title = metadata.title or ""
            artist = metadata.artist or ""
            album = metadata.album or ""
            cmd = f"TITLE={title}\nARTIST={artist}\nALBUM={album}\n"
            cmd += f"DURATION={duration}\nPROGRESS=0\nACTION=SENDMETA\n"
            await self.send_cli_command(cmd)
            # get image
            if metadata.image_url:
                await self.send_cli_command(f"ARTWORK={metadata.image_url}\n")
        if progress is not None:
            await self.send_cli_command(f"PROGRESS={progress}\n")
