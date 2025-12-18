"""Streaming functionality using librespot for Spotify provider."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING

from music_assistant_models.enums import MediaType
from music_assistant_models.errors import AudioError

from music_assistant.constants import VERBOSE_LOG_LEVEL
from music_assistant.helpers.process import AsyncProcess

if TYPE_CHECKING:
    from music_assistant_models.streamdetails import StreamDetails

    from .provider import SpotifyProvider


class LibrespotStreamer:
    """Handles streaming functionality using librespot."""

    def __init__(self, provider: SpotifyProvider) -> None:
        """Initialize the LibrespotStreamer."""
        self.provider = provider

    async def get_audio_stream(
        self, streamdetails: StreamDetails, seek_position: int = 0
    ) -> AsyncGenerator[bytes, None]:
        """Return the audio stream for the provider item."""
        # Regular track/episode streaming - audiobooks are handled in the provider
        media_type = "episode" if streamdetails.media_type == MediaType.PODCAST_EPISODE else "track"
        spotify_uri = f"spotify://{media_type}:{streamdetails.item_id}"
        async for chunk in self.stream_spotify_uri(spotify_uri, seek_position):
            yield chunk

    async def stream_spotify_uri(
        self, spotify_uri: str, seek_position: int = 0
    ) -> AsyncGenerator[bytes, None]:
        """Return the audio stream for the Spotify URI."""
        self.provider.logger.log(
            VERBOSE_LOG_LEVEL, f"Start streaming {spotify_uri} using librespot"
        )
        # Validate that librespot binary is available
        if not self.provider._librespot_bin:
            raise AudioError("Librespot binary not available")

        args = [
            self.provider._librespot_bin,
            "--cache",
            self.provider.cache_dir,
            "--disable-audio-cache",
            "--passthrough",
            "--bitrate",
            "320",
            "--backend",
            "pipe",
            "--single-track",
            spotify_uri,
            "--disable-discovery",
            "--dither",
            "none",
        ]
        if seek_position:
            args += ["--start-position", str(int(seek_position))]

        async with AsyncProcess(
            args,
            stdout=True,
            stderr=True,
            name="librespot",
        ) as librespot_proc:
            log_history: deque[str] = deque(maxlen=10)
            logger = self.provider.logger

            async def log_librespot_output() -> None:
                """Log librespot output if verbose logging is enabled."""
                async for line in librespot_proc.iter_stderr():
                    log_history.append(line)
                    if "ERROR" in line or "WARNING" in line:
                        logger.warning("[librespot] %s", line)
                        if "Unable to read audio file" in line:
                            # if this happens, we should stop the process to avoid hanging
                            await librespot_proc.close()
                    else:
                        logger.log(VERBOSE_LOG_LEVEL, "[librespot] %s", line)

            librespot_proc.attach_stderr_reader(asyncio.create_task(log_librespot_output()))
            # yield from librespot's stdout
            async for chunk in librespot_proc.iter_chunked():
                yield chunk

            if librespot_proc.returncode != 0:
                raise AudioError(
                    f"Librespot exited with code {librespot_proc.returncode} for {spotify_uri}"
                )
