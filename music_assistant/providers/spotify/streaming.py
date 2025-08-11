"""Streaming functionality for Spotify provider using librespot."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING

from music_assistant_models.enums import ContentType, MediaType, StreamType
from music_assistant_models.errors import AudioError
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.constants import VERBOSE_LOG_LEVEL
from music_assistant.helpers.process import AsyncProcess

from .constants import LIBRESPOT_PROFILES, LibrespotProfile

if TYPE_CHECKING:
    from . import SpotifyProvider


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
        async for chunk in self._stream_via_librespot(streamdetails, seek_position):
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

    async def _stream_via_librespot(
        self, streamdetails: StreamDetails, seek_position: int = 0
    ) -> AsyncGenerator[bytes, None]:
        """Stream track or episode using librespot with retry logic."""
        is_episode = streamdetails.data == "episode"
        media_type = "episode" if is_episode else "track"
        spotify_uri = f"spotify:{media_type}:{streamdetails.item_id}"

        self.logger.log(VERBOSE_LOG_LEVEL, f"Start streaming {spotify_uri} using librespot")
        self.logger.info(f"Starting librespot for {media_type}: {spotify_uri}")

        config = self._get_streaming_config(is_episode)

        for profile_num in range(1, config["max_profiles"] + 1):
            # Get profile name
            profile = f"episode_{profile_num}" if is_episode else "track"

            for attempt in range(1, config["attempts_per_profile"] + 1):
                # Calculate timeout and attempt label
                timeout = config["timeout_base"] if is_episode else config["timeout_base"] * attempt
                attempt_label = f"{profile_num}.{attempt}" if is_episode else str(attempt)

                try:
                    async for chunk in self._attempt_stream(
                        spotify_uri,
                        profile,
                        seek_position,
                        timeout,
                        attempt_label,
                        config,
                        is_episode,
                        media_type,
                        streamdetails.item_id,
                    ):
                        yield chunk
                    return  # Success - exit completely

                except (TimeoutError, AudioError) as e:
                    # Check if this is the last attempt
                    is_last_profile = profile_num == config["max_profiles"]
                    is_last_attempt = attempt == config["attempts_per_profile"]

                    if is_last_profile and is_last_attempt:
                        error_msg = f"All attempts failed for {media_type} {streamdetails.item_id}"
                        self.logger.error(error_msg)
                        raise AudioError(str(e))

                    await self._handle_retry_delay(is_episode, attempt_label, e, config)

    def _get_streaming_config(self, is_episode: bool) -> dict[str, int]:
        """Get streaming configuration based on media type."""
        if is_episode:
            return {
                "max_profiles": 3,
                "attempts_per_profile": 2,
                "timeout_base": 2,
                "initial_read_size": 8192,
            }
        else:
            return {
                "max_profiles": 1,
                "attempts_per_profile": 2,
                "timeout_base": 5,
                "initial_read_size": 64000,
            }

    async def _handle_retry_delay(
        self, is_episode: bool, attempt_label: str, error: Exception, config: dict[str, int]
    ) -> None:
        """Handle delay and logging before retry."""
        media_type = "Podcast" if is_episode else "Track"
        error_msg = str(error).strip() or f"{type(error).__name__} (no details)"
        self.logger.warning(f"{media_type} Stream Attempt {attempt_label} failed - {error_msg}")

        if is_episode:
            self.logger.debug(f"Waiting {config['timeout_base']} secs before next attempt...")
            await asyncio.sleep(config["timeout_base"])  # 2 seconds for episodes
        else:
            self.logger.warning(f"{error_msg} - will retry once")
            await asyncio.sleep(config["timeout_base"])  # 5 seconds for tracks

    async def _attempt_stream(
        self,
        spotify_uri: str,
        profile: str,
        seek_position: int,
        timeout: int,
        attempt_label: str,
        config: dict[str, int],
        is_episode: bool,
        media_type: str,
        item_id: str,
    ) -> AsyncGenerator[bytes, None]:
        """Attempt to stream using librespot with the given parameters."""
        self.logger.debug(
            f"{media_type.title()} streaming attempt {attempt_label} with {timeout}s timeout"
        )
        args = self._get_librespot_args(spotify_uri, profile, seek_position)

        if not is_episode:
            self.logger.debug(f"Librespot command: {' '.join(args)}")

        async with AsyncProcess(
            args,
            stdout=True,
            stderr=None if self.logger.isEnabledFor(VERBOSE_LOG_LEVEL) else False,
            name="librespot",
        ) as librespot_proc:
            chunks_received = 0

            # Get initial chunk(s)
            if is_episode:
                chunks_received, initial_chunks = await self._process_initial_chunk(
                    librespot_proc, timeout, attempt_label, config["initial_read_size"]
                )
                for chunk in initial_chunks:
                    yield chunk
            else:
                chunk = await asyncio.wait_for(
                    librespot_proc.read(config["initial_read_size"]), timeout=timeout
                )
                if not chunk:
                    raise AudioError("No audio received from librespot - empty chunk")

                self.logger.debug(
                    f"Successfully received initial audio chunk ({len(chunk)} bytes) "
                    f"on attempt {attempt_label}"
                )
                yield chunk
                chunks_received = 1

            # Stream remaining chunks
            async for chunk in librespot_proc.iter_chunked():
                chunks_received += 1
                yield chunk

            # Check for interrupted but successful streams
            if chunks_received > 50:
                self.logger.warning(
                    f"Stream interrupted after receiving {chunks_received} chunks - "
                    f"treating as successful completion"
                )
                return

            self.logger.debug(f"Completed streaming {media_type} - total chunks: {chunks_received}")

    async def _process_initial_chunk(
        self, librespot_proc: AsyncProcess, timeout: int, attempt_label: str, read_size: int
    ) -> tuple[int, list[bytes]]:
        """Process initial chunk and handle small chunk scenarios for episodes."""
        chunk = await asyncio.wait_for(librespot_proc.read(read_size), timeout=timeout)

        if not chunk:
            raise AudioError("No audio received from librespot - empty chunk")

        if len(chunk) < 500:
            self.logger.warning(
                f"Received small chunk ({len(chunk)} bytes) - checking for continuation..."
            )
            try:
                next_chunk = await asyncio.wait_for(librespot_proc.read(read_size), timeout=3)
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
