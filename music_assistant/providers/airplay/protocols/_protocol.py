"""Base protocol class for AirPlay streaming implementations."""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from random import randint
from typing import TYPE_CHECKING

from music_assistant_models.enums import PlaybackState

from music_assistant.constants import VERBOSE_LOG_LEVEL
from music_assistant.helpers.named_pipe import AsyncNamedPipeWriter
from music_assistant.providers.airplay.constants import AIRPLAY_PCM_FORMAT

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

    _cli_proc: AsyncProcess | None  # reference to the (protocol-specific) CLI process
    session: AirPlayStreamSession | None = None  # reference to the active stream session (if any)

    # the pcm audio format used for streaming to this protocol
    pcm_format = AIRPLAY_PCM_FORMAT
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
        self.logger = player.provider.logger.getChild(f"protocol.{self.__class__.__name__}")
        # Generate unique ID to prevent race conditions with named pipes
        self.active_remote_id: str = str(randint(1000, 8000))
        self.prevent_playback: bool = False
        self._cli_proc: AsyncProcess | None = None
        self.audio_pipe = AsyncNamedPipeWriter(
            f"/tmp/{self.player.protocol.value}-{self.player.player_id}-{self.active_remote_id}-audio",  # noqa: S108
        )
        self.commands_pipe = AsyncNamedPipeWriter(
            f"/tmp/{self.player.protocol.value}-{self.player.player_id}-{self.active_remote_id}-cmd",  # noqa: S108
        )
        # State tracking
        self._stopped = False
        self._total_bytes_sent = 0
        self._stream_bytes_sent = 0

    @property
    def running(self) -> bool:
        """Return boolean if this stream is running."""
        return not self._stopped and self._cli_proc is not None and not self._cli_proc.closed

    @abstractmethod
    async def start(self, start_ntp: int) -> None:
        """Initialize streaming process for the player.

        Args:
            start_ntp: NTP timestamp to start streaming
        """

    async def stop(self) -> None:
        """Stop playback and cleanup."""
        # Send stop command before setting _stopped flag
        await self.send_cli_command("ACTION=STOP")
        self._stopped = True

        # Close the CLI process (wait for it to terminate)
        if self._cli_proc and not self._cli_proc.closed:
            await self._cli_proc.close()

        self.player.set_state_from_stream(state=PlaybackState.IDLE, elapsed_time=0)

        # Cleanup named pipes
        await self.audio_pipe.remove()
        await self.commands_pipe.remove()

    async def start_pairing(self) -> None:
        """Start pairing process for this protocol (if supported)."""
        raise NotImplementedError("Pairing not implemented for this protocol")

    async def finish_pairing(self, pin: str) -> str:
        """Finish pairing process with given PIN (if supported)."""
        raise NotImplementedError("Pairing not implemented for this protocol")

    async def send_cli_command(self, command: str) -> None:
        """Send an interactive command to the running CLI binary."""
        if self._stopped or not self._cli_proc or self._cli_proc.closed:
            return
        if not self.commands_pipe:
            return

        self.player.logger.log(VERBOSE_LOG_LEVEL, "sending command %s", command)
        self.player.last_command_sent = time.time()
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
            cmd = f"TITLE={title}\nARTIST={artist}\nALBUM={album}\n"
            cmd += f"DURATION={duration}\nPROGRESS=0\nACTION=SENDMETA\n"
            await self.send_cli_command(cmd)
            # get image
            if metadata.image_url:
                await self.send_cli_command(f"ARTWORK={metadata.image_url}\n")
        if progress is not None:
            await self.send_cli_command(f"PROGRESS={progress}\n")
