"""Streaming functionality for Spotify provider using librespot."""

from __future__ import annotations

import asyncio
import contextlib
from asyncio import Task
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, TypedDict

from music_assistant_models.enums import ContentType, MediaType, StreamType
from music_assistant_models.errors import AudioError
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.constants import VERBOSE_LOG_LEVEL
from music_assistant.helpers.process import AsyncProcess

from .constants import LIBRESPOT_PROFILES, LibrespotProfile

if TYPE_CHECKING:
    from . import SpotifyProvider


class StderrAnalysis(TypedDict):
    """Analysis results from monitoring librespot stderr output.

    Contains categorized error counts and messages for debugging
    streaming issues with librespot audio processing.

    Attributes:
        audio_key_errors: Number of audio key retrieval errors encountered
        decoder_errors: List of decoder-specific error messages (e.g., Ogg capture issues)
        cdn_issues: List of CDN-related error messages (e.g., URL parsing failures)
    """

    audio_key_errors: int
    decoder_errors: list[str]
    cdn_issues: list[str]


class LibrespotStreamer:
    """Handles streaming functionality using librespot."""

    def __init__(self, provider: SpotifyProvider):
        """Initialize the LibrespotStreamer with a reference to the Spotify provider."""
        self.provider = provider
        self.logger = provider.logger

    def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Return the content details for the given track/episode when it will be streamed."""
        bit_rate = 160 if media_type == MediaType.PODCAST_EPISODE else 320
        data_type = "episode" if media_type == MediaType.PODCAST_EPISODE else "track"

        return StreamDetails(
            item_id=item_id,
            provider=self.provider.lookup_key,
            media_type=media_type,
            audio_format=AudioFormat(
                content_type=ContentType.OGG,
                bit_rate=bit_rate,
            ),
            stream_type=StreamType.CUSTOM,
            allow_seek=True,
            can_seek=True,
            data=data_type,
        )

    async def get_audio_stream(
        self, streamdetails: StreamDetails, seek_position: int = 0
    ) -> AsyncGenerator[bytes, None]:
        """Return the audio stream for the provider item."""
        if streamdetails.data == "episode":
            async for chunk in self._stream_episode_via_librespot(streamdetails, seek_position):
                yield chunk
        else:
            async for chunk in self._stream_track_via_librespot(streamdetails, seek_position):
                yield chunk

    def _get_librespot_args(
        self, spotify_uri: str, profile: str, seek_position: int = 0
    ) -> list[str]:
        """Get librespot arguments for given profile."""
        if profile not in LIBRESPOT_PROFILES:
            raise ValueError(f"Unknown librespot profile: {profile}")

        config: LibrespotProfile = LIBRESPOT_PROFILES[profile]

        if not self.provider._librespot_bin:
            raise RuntimeError("Librespot binary path not configured")

        args: list[str] = [
            self.provider._librespot_bin,  # mypy knows this is str after the check above
            "--cache",
            self.provider.cache_dir,
            "--passthrough",
            "--backend",
            "pipe",
            "--single-track",
            spotify_uri,
            "--disable-discovery",
            "--verbose",
            "--bitrate",
            config["bitrate"],
        ]

        args.extend(config["args"])

        if seek_position:
            args.extend(["--start-position", str(int(seek_position))])

        return args

    async def _stream_track_via_librespot(
        self, streamdetails: StreamDetails, seek_position: int = 0
    ) -> AsyncGenerator[bytes, None]:
        """Stream track using librespot."""
        spotify_uri = f"spotify://track:{streamdetails.item_id}"
        self.logger.log(VERBOSE_LOG_LEVEL, f"Start streaming {spotify_uri} using librespot")
        self.logger.info(f"Starting librespot for track: {spotify_uri}")

        for attempt in (1, 2):
            args = self._get_librespot_args(spotify_uri, "track", seek_position)
            self.logger.debug(f"Librespot command: {' '.join(args)}")

            async with AsyncProcess(
                args,
                stdout=True,
                stderr=None if self.logger.isEnabledFor(VERBOSE_LOG_LEVEL) else False,
                name="librespot",
            ) as librespot_proc:
                chunks_received = 0

                try:
                    chunk = await asyncio.wait_for(librespot_proc.read(64000), timeout=5 * attempt)

                    if not chunk:
                        raise AudioError("No audio received from librespot - empty chunk")

                    self.logger.debug(
                        f"Successfully received initial audio chunk ({len(chunk)} bytes) "
                        f"on attempt {attempt}"
                    )
                    yield chunk
                    chunks_received = 1

                    async for chunk in librespot_proc.iter_chunked():
                        chunks_received += 1
                        yield chunk

                    self.logger.debug(
                        f"Completed streaming track - total chunks: {chunks_received}"
                    )
                    return

                except (TimeoutError, AudioError):
                    if chunks_received > 50:
                        self.logger.warning(
                            f"Stream interrupted after receiving {chunks_received} chunks - "
                            f"treating as successful completion"
                        )
                        return

                    err_msg = "No audio received from librespot within timeout"

                    if attempt == 2:
                        self.logger.error(f"All attempts failed for track {streamdetails.item_id}")
                        raise AudioError(err_msg)
                    else:
                        self.logger.warning(f"{err_msg} - will retry once")
                        continue

    async def _stream_episode_via_librespot(
        self, streamdetails: StreamDetails, seek_position: int = 0
    ) -> AsyncGenerator[bytes, None]:
        """Stream episode using librespot with retry logic."""
        self.logger.info(f"Episode {streamdetails.item_id}")
        spotify_uri = f"spotify:episode:{streamdetails.item_id}"

        for attempt_type in (1, 2):
            profile = f"episode_{attempt_type}"
            for sub_attempt in range(1, 4):
                timeout = 2
                attempt_label = f"{attempt_type}.{sub_attempt}"

                self.logger.debug(
                    f"Episode streaming attempt {attempt_label} with {timeout}s timeout"
                )
                args = self._get_librespot_args(spotify_uri, profile, seek_position)

                async with AsyncProcess(
                    args, stdout=True, stderr=True, name="librespot"
                ) as librespot_proc:
                    stderr_task: Task[StderrAnalysis] = asyncio.create_task(
                        self._monitor_librespot_stderr_enhanced(librespot_proc, attempt_label)
                    )
                    chunks_received = 0

                    try:
                        chunks_received, initial_chunks = await self._process_episode_initial_chunk(
                            librespot_proc, timeout, attempt_label
                        )

                        for chunk in initial_chunks:
                            yield chunk

                        async for chunk in librespot_proc.iter_chunked():
                            chunks_received += 1
                            yield chunk

                        self.logger.info(
                            f"Completed streaming episode - total chunks: {chunks_received}"
                        )
                        return

                    except (TimeoutError, AudioError) as e:
                        if chunks_received > 50:
                            self.logger.warning(
                                f"Stream interrupted after receiving {chunks_received} chunks - "
                                f"treating as success"
                            )
                            return

                        await self._handle_episode_stream_error(stderr_task, attempt_label, e)

                        if not (attempt_type == 2 and sub_attempt == 3):
                            self.logger.debug(f"Waiting {timeout} secs before next attempt...")
                            await asyncio.sleep(timeout)

                    finally:
                        if stderr_task and not stderr_task.done():
                            stderr_task.cancel()
                            with contextlib.suppress(asyncio.CancelledError):
                                await stderr_task

        error_msg = "Episode streaming failed after all attempts"
        raise AudioError(error_msg)

    async def _process_episode_initial_chunk(
        self, librespot_proc: AsyncProcess, timeout: int, attempt_label: str
    ) -> tuple[int, list[bytes]]:
        """Process initial chunk and handle small chunk scenarios."""
        chunk = await asyncio.wait_for(librespot_proc.read(8192), timeout=timeout)

        if not chunk:
            raise AudioError("No audio received from librespot - empty chunk")

        if len(chunk) < 500:
            self.logger.warning(
                f"Received small chunk ({len(chunk)} bytes) - checking for continuation..."
            )
            try:
                next_chunk = await asyncio.wait_for(librespot_proc.read(8192), timeout=3)
                if next_chunk and len(next_chunk) > 1000:
                    self.logger.debug(f"Got valid continuation chunk ({len(next_chunk)} bytes)")
                    return 2, [chunk, next_chunk]
                else:
                    raise AudioError(
                        f"Small chunk with insufficient follow-up: "
                        f"{len(chunk)} + {len(next_chunk) if next_chunk else 0} bytes"
                    )
            except TimeoutError:
                raise AudioError(f"Small chunk with no follow-up: {len(chunk)} bytes")
        else:
            self.logger.debug(
                f"Successfully received initial audio chunk "
                f"({len(chunk)} bytes) on attempt {attempt_label}"
            )
            return 1, [chunk]

    async def _handle_episode_stream_error(
        self,
        stderr_task: Task[StderrAnalysis] | None,
        attempt_label: str,
        error: Exception,
    ) -> None:
        """Handle episode streaming error and log analysis."""
        error_analysis = None
        if stderr_task and not stderr_task.done():
            stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                error_analysis = await stderr_task

        if error_analysis:
            self.logger.warning(
                f"Attempt {attempt_label} failed - "
                f"CDN issues: {len(error_analysis.get('cdn_issues', []))}, "
                f"Audio key errors: {error_analysis.get('audio_key_errors', 0)}, "
                f"Decoder errors: {len(error_analysis.get('decoder_errors', []))} - {error!s}"
            )
        else:
            self.logger.warning(f"Attempt {attempt_label} failed - {error!s}")

    async def _monitor_librespot_stderr_enhanced(
        self, librespot_proc: AsyncProcess, attempt: str
    ) -> StderrAnalysis:
        """Enhanced stderr monitoring with CDN-specific diagnostics."""
        """This was used for debugging so could be removed in production? """
        audio_key_errors = 0
        decoder_errors = []
        cdn_issues = []

        try:
            while True:
                try:
                    stderr_data = await asyncio.wait_for(librespot_proc.read_stderr(), timeout=1.0)
                    if not stderr_data:
                        break
                    line = stderr_data.decode("utf-8", errors="ignore").strip()
                    if line:
                        self.logger.log(
                            VERBOSE_LOG_LEVEL, f"Librespot stderr (attempt {attempt}): {line}"
                        )
                        # Track different error types
                        if "error audio key" in line:
                            audio_key_errors += 1
                            self.logger.warning(f"Audio key error #{audio_key_errors}: {line}")
                        # CDN-specific issues
                        if "Cannot parse CDN URL" in line:
                            cdn_issues.append(line)
                            self.logger.warning(f"CDN URL parsing issue: {line}")
                            # Extract the problematic URL for analysis
                            if "verify=" in line:
                                verify_param = (
                                    line.split("verify=")[1].split("'")[0]
                                    if "verify=" in line
                                    else "unknown"
                                )
                                self.logger.info(f"CDN verify parameter: {verify_param}")
                        # Decoder-specific errors
                        decoder_error_patterns = [
                            "No Ogg capture pattern found",
                            "Deadline expired before operation could complete",
                            "Passthrough Decoder Error",
                            "Symphonia Decoder Error",
                            "Invalid audio format",
                            "Unsupported codec",
                        ]
                        for pattern in decoder_error_patterns:
                            if pattern in line:
                                decoder_errors.append(pattern)
                                self.logger.warning(f"Decoder error detected: {pattern}")
                        # General error logging
                        if " WARN " in line or " ERROR " in line:
                            self.logger.warning(f"Librespot potential error detected: {line}")
                except TimeoutError:
                    continue
        except asyncio.CancelledError:
            pass
        except Exception as e:
            self.logger.debug(f"Error monitoring stderr: {e}")

        return {
            "audio_key_errors": audio_key_errors,
            "decoder_errors": decoder_errors,
            "cdn_issues": cdn_issues,
        }
