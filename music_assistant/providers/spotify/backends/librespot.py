"""Librespot playback backend for the Spotify music provider."""

from __future__ import annotations

import asyncio
import os
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING

from music_assistant_models.enums import ContentType
from music_assistant_models.errors import AudioError, LoginFailed
from music_assistant_models.media_items import AudioFormat

from music_assistant.constants import VERBOSE_LOG_LEVEL
from music_assistant.helpers.process import AsyncProcess
from music_assistant.providers.spotify.constants import (
    CONF_LIBRESPOT_CREDENTIALS,
    CREDENTIALS_FILE,
)
from music_assistant.providers.spotify.helpers import get_librespot_binary

from .base import SpotifyPlaybackBackend

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from music_assistant.helpers.json import SerializableType


class LibrespotBackend(SpotifyPlaybackBackend):
    """
    Fetches Spotify audio through the bundled librespot fork.

    One short-lived ``librespot --single-track`` process per item, yielding the
    original Ogg Vorbis stream (passthrough, no decode).
    """

    _librespot_bin: str | None = None

    @property
    def audio_format(self) -> AudioFormat:
        """Return the audio format this backend delivers, for use in StreamDetails."""
        return AudioFormat(content_type=ContentType.OGG, bit_rate=320)

    @property
    def max_concurrent_streams(self) -> int:
        """Spotify accounts tolerate two concurrent sessions (main + librespot)."""
        return 2

    async def setup(self) -> None:
        """
        Validate the librespot binary and install the stored playback credential.

        :raises LoginFailed: When no playback credential is configured, which requires
            the user to re-run the setup flow.
        """
        # a missing binary is a platform problem, not an auth problem: let the
        # RuntimeError surface as a plain setup failure
        self._librespot_bin = await get_librespot_binary()
        credentials = self.provider.get_setup_value(CONF_LIBRESPOT_CREDENTIALS)
        if not credentials:
            # Spotify's login5 refuses credentials minted with any client id other than the one
            # librespot presents, so installs predating the dedicated playback credential (and
            # anything cached from before) cannot stream and must authorize playback again.
            raise LoginFailed(
                "Spotify playback authorization required",
                translation_key="playback_auth_required",
                translation_owner="provider.spotify",
            )
        await asyncio.to_thread(
            self._write_librespot_credentials, self.provider.cache_dir, str(credentials)
        )

    async def stream_spotify_uri(
        self, spotify_uri: str, seek_position: int = 0
    ) -> AsyncGenerator[bytes]:
        """
        Yield the Ogg Vorbis audio for one Spotify URI.

        :param spotify_uri: Canonical Spotify URI (``spotify:track:<id>`` or
            ``spotify:episode:<id>``).
        :param seek_position: Position in seconds to start from.
        """
        # librespot's --single-track parser wants its own spotify://type:id form
        librespot_uri = spotify_uri.replace("spotify:", "spotify://", 1)
        self.logger.log(VERBOSE_LOG_LEVEL, "Start streaming %s using librespot", spotify_uri)
        if not self._librespot_bin:
            raise AudioError("Librespot binary not available")

        args = [
            self._librespot_bin,
            "--cache",
            self.provider.cache_dir,
            "--disable-audio-cache",
            "--passthrough",
            "--bitrate",
            "320",
            "--backend",
            "pipe",
            "--single-track",
            librespot_uri,
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
            logger = self.logger
            provider = self.provider

            async def log_librespot_output() -> None:
                """Log librespot output if verbose logging is enabled."""
                async for line in librespot_proc.iter_stderr():
                    log_history.append(line)
                    if "ERROR" in line or "WARNING" in line:
                        logger.warning("[librespot] %s", line)
                        if "INVALID_CREDENTIALS" in line and provider.available:
                            # Spotify refused the stored playback credential: surface this as an
                            # auth failure so the provider asks for re-authorization instead of
                            # reporting every track as unplayable. The availability check keeps
                            # concurrent/queued streams from each scheduling their own unload.
                            provider.unload_with_error(
                                LoginFailed(
                                    "Spotify playback authorization required",
                                    translation_key="playback_auth_required",
                                    translation_owner="provider.spotify",
                                )
                            )
                        if "unable to" in line.lower() or "skipping" in line.lower():
                            # if librespot reports a fatal error (e.g. unable to load
                            # or read audio), terminate the process to avoid hanging
                            # indefinitely as it won't produce any audio output.
                            # NOTE: we terminate the underlying process directly instead
                            # of calling close() because this task IS the stderr reader
                            # and close() would try to await itself.
                            if librespot_proc.proc and librespot_proc.proc.returncode is None:
                                librespot_proc.proc.terminate()
                            return
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

    async def get_diagnostics(self) -> dict[str, SerializableType]:
        """Return diagnostic details about the backend."""
        return {"librespot_available": self._librespot_bin is not None}

    @staticmethod
    def _write_librespot_credentials(cache_dir: str, credentials: str) -> None:
        """Write the stored credential to librespot's cache, replacing any stale one."""
        Path(cache_dir).mkdir(parents=True, exist_ok=True)
        credentials_file = os.path.join(cache_dir, CREDENTIALS_FILE)
        with open(credentials_file, "w", encoding="utf-8") as fileobj:
            fileobj.write(credentials)
