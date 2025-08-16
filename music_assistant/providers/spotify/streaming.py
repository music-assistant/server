"""Streaming functionality using librespot for Spotify provider."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING

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
        spotify_uri = f"spotify://track:{streamdetails.item_id}"
        self.provider.logger.log(
            VERBOSE_LOG_LEVEL, f"Start streaming {spotify_uri} using librespot"
        )
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

        # we retry twice in case librespot fails to start
        for attempt in (1, 2):
            log_librespot = self.provider.logger.isEnabledFor(VERBOSE_LOG_LEVEL) or attempt == 2
            async with AsyncProcess(
                args,
                stdout=True,
                stderr=None if log_librespot else False,
                name="librespot",
            ) as librespot_proc:
                # get first chunk with timeout, to catch the issue where librespot is not starting
                # which seems to happen from time to time (but rarely)
                try:
                    chunk = await asyncio.wait_for(librespot_proc.read(64000), timeout=10 * attempt)
                    if not chunk:
                        raise AudioError
                    yield chunk
                except (TimeoutError, AudioError):
                    err_mesg = "No audio received from librespot within timeout"
                    if attempt == 2:
                        raise AudioError(err_mesg)
                    self.provider.logger.warning("%s - will retry once", err_mesg)
                    continue

                # keep yielding chunks until librespot is done
                async for chunk in librespot_proc.iter_chunked():
                    yield chunk

                # if we reach this point, streaming succeeded and we can break the loop
                break
