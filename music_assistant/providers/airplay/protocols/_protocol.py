"""Base protocol class for AirPlay streaming implementations."""

from __future__ import annotations

import asyncio
import os
import time
from abc import ABC, abstractmethod
from contextlib import suppress
from random import randint
from typing import TYPE_CHECKING, Any

from music_assistant_models.enums import ContentType, PlaybackState
from music_assistant_models.media_items import AudioFormat

from music_assistant.constants import VERBOSE_LOG_LEVEL

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

    # the pcm audio format used for streaming to this protocol
    pcm_format = AudioFormat(
        content_type=ContentType.PCM_S16LE, sample_rate=44100, bit_depth=16, channels=2
    )

    def __init__(
        self,
        session: AirPlayStreamSession,
        player: AirPlayPlayer,
    ) -> None:
        """Initialize base AirPlay protocol.

        Args:
            session: The stream session managing this protocol instance
            player: The player to stream to
        """
        self.session = session
        self.prov = session.prov
        self.mass = session.prov.mass
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
        # File descriptors for named pipes (kept open for session duration)
        self._audio_pipe_fd: Any = None
        self._commands_pipe_fd: Any = None

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

    @abstractmethod
    async def start(self, start_ntp: int) -> None:
        """Initialize streaming process for the player.

        Args:
            start_ntp: NTP timestamp to start streaming
        """

    async def _open_pipes(self) -> None:
        """Open both named pipes and keep them open for the session."""

        def _open() -> None:
            # Open audio pipe in binary mode, unbuffered
            self._audio_pipe_fd = open(self.audio_named_pipe, "wb", buffering=0)  # noqa: SIM115
            # Open metadata pipe in text mode, line buffered (buffering=1)
            # Line buffering flushes automatically after each newline
            self._commands_pipe_fd = open(self.commands_named_pipe, "w", buffering=1)  # noqa: SIM115

        await asyncio.to_thread(_open)
        self.player.logger.debug("Named pipes opened for streaming session")

    async def stop(self) -> None:
        """Stop playback and cleanup."""
        # Send stop command before setting _stopped flag
        await self.send_cli_command("ACTION=STOP")

        # Ensure the command is flushed (line buffering should handle this, but be explicit)
        if self._commands_pipe_fd is not None:
            with suppress(Exception):
                await asyncio.to_thread(self._commands_pipe_fd.flush)

        self._stopped = True

        # Close file descriptors (sends EOF to C side, triggering graceful shutdown)
        if self._audio_pipe_fd is not None:
            with suppress(Exception):
                await asyncio.to_thread(self._audio_pipe_fd.close)
            self._audio_pipe_fd = None

        if self._commands_pipe_fd is not None:
            with suppress(Exception):
                await asyncio.to_thread(self._commands_pipe_fd.close)
            self._commands_pipe_fd = None

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
        Blocks (async) until the data has been written.
        """
        # default implementation simply writes the chunk to the named pipe
        # can be overridden with protocol specific implementation if needed
        if self._audio_pipe_fd is None:
            return

        def _write() -> None:
            if self._audio_pipe_fd is not None:
                self._audio_pipe_fd.write(chunk)
            # No flush needed - unbuffered mode

        await asyncio.to_thread(_write)

    async def write_eof(self) -> None:
        """Write EOF to signal end of stream."""
        # default implementation simply closes the named pipe
        # can be overridden with protocol specific implementation if needed
        if self._audio_pipe_fd is not None:
            await asyncio.to_thread(self._audio_pipe_fd.close)
            self._audio_pipe_fd = None

    async def send_cli_command(self, command: str) -> None:
        """Send an interactive command to the running CLI binary."""
        if self._stopped or not self._cli_proc or self._cli_proc.closed:
            return
        if self._commands_pipe_fd is None:
            return

        await self._started.wait()

        if not command.endswith("\n"):
            command += "\n"

        def send_data() -> None:
            if self._commands_pipe_fd is not None:
                self._commands_pipe_fd.write(command)
            # Line buffering flushes automatically after newline

        self.player.logger.log(VERBOSE_LOG_LEVEL, "sending command %s", command)
        self.player.last_command_sent = time.time()

        with suppress(BrokenPipeError):
            await asyncio.to_thread(send_data)

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
