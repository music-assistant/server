"""Streaming operations for KION Music."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any

import aiohttp
from aiohttp import ClientPayloadError, ServerDisconnectedError
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from music_assistant_models.enums import ContentType, StreamType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.helpers.throttle_retry import BYPASS_THROTTLER

from .constants import (
    CONF_QUALITY,
    QUALITY_EFFICIENT,
    QUALITY_HIGH,
    QUALITY_LOSSLESS,
    RADIO_TRACK_ID_SEP,
)

if TYPE_CHECKING:
    from yandex_music import DownloadInfo

    from .provider import KionMusicProvider


# Encrypted-stream tuning constants
_CHUNK_SIZE = 16384  # smaller than default 65536 for faster first-byte after retry
_STREAM_TIMEOUT = aiohttp.ClientTimeout(total=None, sock_read=30)
# Kion CDN drops TCP every ~6-7 MB per connection (observed via live traffic capture).
# By capping each Range request to 4 MB we stay well below that limit, so CDN drops
# should never occur during normal windowed playback.
_RANGE_WINDOW = 4 * 1024 * 1024  # 4 MB — must be a multiple of AES block size (16)
# Flat short delays for any residual TCP drops (network glitches within a 4 MB window)
_TCP_DROP_DELAYS = (0.5, 1.0, 2.0)
# Exponential delays for true network stalls (read timeout)
_STALL_DELAYS = (2.0, 4.0, 8.0)


class KionMusicStreamingManager:
    """Manages KION Music streaming operations."""

    def __init__(self, provider: KionMusicProvider) -> None:
        """Initialize streaming manager.

        :param provider: The KION Music provider instance.
        """
        self.provider = provider
        self.client = provider.client
        self.mass = provider.mass
        self.logger = provider.logger

    def _track_id_from_item_id(self, item_id: str) -> str:
        """Extract API track ID from item_id (may be track_id@station_id for My Mix)."""
        if RADIO_TRACK_ID_SEP in item_id:
            return item_id.split(RADIO_TRACK_ID_SEP, 1)[0]
        return item_id

    async def get_stream_details(self, item_id: str) -> StreamDetails:
        """Get stream details for a track.

        :param item_id: Track ID or composite track_id@station_id for My Mix.
        :return: StreamDetails for the track (item_id preserved for on_streamed).
        :raises MediaNotFoundError: If stream URL cannot be obtained.
        """
        track_id = self._track_id_from_item_id(item_id)
        track = await self.provider.get_track(item_id)
        if not track:
            raise MediaNotFoundError(f"Track {item_id} not found")

        quality = self.provider.config.get_value(CONF_QUALITY)
        quality_str = str(quality) if quality is not None else None
        preferred_normalized = (quality_str or "").strip().lower()

        # Check for superb (lossless) quality
        want_lossless = preferred_normalized in (QUALITY_LOSSLESS, "superb")

        # Backward compatibility: also check old "lossless" value (exact match)
        if preferred_normalized == "lossless":
            want_lossless = True

        # When user wants lossless, try get-file-info first (FLAC; download-info often MP3 only)
        if want_lossless:
            self.logger.debug("Requesting lossless via get-file-info for track %s", track_id)
            file_info = await self.client.get_track_file_info_lossless(track_id)
            if file_info:
                url = file_info.get("url")
                codec = file_info.get("codec") or ""
                needs_decryption = file_info.get("needs_decryption", False)

                if url and codec.lower() in ("flac", "flac-mp4"):
                    audio_format = self._build_audio_format(codec)

                    # Handle encrypted URLs from encraw transport
                    if needs_decryption and "key" in file_info:
                        self.logger.info(
                            "Streaming encrypted %s for track %s - will decrypt on-the-fly",
                            codec,
                            track_id,
                        )
                        # Return StreamType.CUSTOM for streaming decryption.
                        # can_seek=False: provider always streams from position 0;
                        # allow_seek=True: ffmpeg handles seek with -ss input flag.
                        return StreamDetails(
                            item_id=item_id,
                            provider=self.provider.instance_id,
                            audio_format=audio_format,
                            stream_type=StreamType.CUSTOM,
                            duration=track.duration,
                            data={
                                "encrypted_url": url,
                                "decryption_key": file_info["key"],
                                "codec": codec,
                            },
                            can_seek=False,
                            allow_seek=True,
                        )
                    # Unencrypted URL, use directly
                    self.logger.debug(
                        "Unencrypted stream for track %s: codec=%s",
                        item_id,
                        codec,
                    )
                    return StreamDetails(
                        item_id=item_id,
                        provider=self.provider.instance_id,
                        audio_format=audio_format,
                        stream_type=StreamType.HTTP,
                        duration=track.duration,
                        path=url,
                        can_seek=True,
                        allow_seek=True,
                        expiration=50,  # get-file-info URLs expire; force MA to re-fetch
                    )

        # Default: use /tracks/.../download-info and select best quality
        download_infos = await self.client.get_track_download_info(track_id, get_direct_links=True)
        if not download_infos:
            raise MediaNotFoundError(f"No stream info available for track {item_id}")

        codecs_available = [
            (getattr(i, "codec", None), getattr(i, "bitrate_in_kbps", None)) for i in download_infos
        ]
        self.logger.debug(
            "Stream quality for track %s: config quality=%s, available codecs=%s",
            track_id,
            quality_str,
            codecs_available,
        )
        selected_info = self._select_best_quality(download_infos, quality_str)

        if not selected_info or not selected_info.direct_link:
            raise MediaNotFoundError(f"No stream URL available for track {item_id}")

        self.logger.debug(
            "Stream selected for track %s: codec=%s, bitrate=%s",
            track_id,
            getattr(selected_info, "codec", None),
            getattr(selected_info, "bitrate_in_kbps", None),
        )

        bitrate = selected_info.bitrate_in_kbps or 0

        return StreamDetails(
            item_id=item_id,
            provider=self.provider.instance_id,
            audio_format=self._build_audio_format(selected_info.codec, bit_rate=bitrate),
            stream_type=StreamType.HTTP,
            duration=track.duration,
            path=selected_info.direct_link,
            can_seek=True,
            allow_seek=True,
            expiration=50,  # download-info direct links expire after ~60s
        )

    def _select_best_quality(
        self, download_infos: list[Any], preferred_quality: str | None
    ) -> DownloadInfo | None:
        """Select the best quality download info based on user preference.

        :param download_infos: List of DownloadInfo objects.
        :param preferred_quality: User's quality preference (efficient/high/balanced/superb).
        :return: Best matching DownloadInfo or None.
        """
        if not download_infos:
            return None

        preferred_normalized = (preferred_quality or "").strip().lower()

        # Sort by bitrate descending
        sorted_infos = sorted(
            download_infos,
            key=lambda x: x.bitrate_in_kbps or 0,
            reverse=True,
        )

        # Superb: Prefer FLAC (backward compatibility with "lossless")
        if preferred_normalized in {QUALITY_LOSSLESS, "lossless"}:
            # Note: flac-mp4 typically comes from get-file-info API, not download-info,
            # but we check here for forward compatibility in case the API changes.
            for codec in ("flac-mp4", "flac"):
                for info in sorted_infos:
                    if info.codec and info.codec.lower() == codec:
                        return info
            self.logger.warning(
                "Superb quality (FLAC) requested but not available; using best available"
            )
            return sorted_infos[0]

        # Efficient: Prefer lowest bitrate AAC/MP3
        if preferred_normalized == QUALITY_EFFICIENT:
            # Sort ascending for lowest bitrate
            sorted_infos_asc = sorted(
                download_infos,
                key=lambda x: x.bitrate_in_kbps or 999,
            )
            # Prefer AAC for efficiency, then MP3 (include MP4 container variants)
            for codec in ("aac-mp4", "aac", "he-aac-mp4", "he-aac", "mp3"):
                for info in sorted_infos_asc:
                    if info.codec and info.codec.lower() == codec:
                        return info
            return sorted_infos_asc[0]

        # High: Prefer high bitrate MP3 (~320kbps)
        if preferred_normalized == QUALITY_HIGH:
            # Look for MP3 with bitrate >= 256kbps
            high_quality_mp3 = [
                info
                for info in sorted_infos
                if info.codec
                and info.codec.lower() == "mp3"
                and info.bitrate_in_kbps
                and info.bitrate_in_kbps >= 256
            ]
            if high_quality_mp3:
                return high_quality_mp3[0]  # Already sorted by bitrate descending

            # Fallback: any MP3 available (highest bitrate)
            for info in sorted_infos:
                if info.codec and info.codec.lower() == "mp3":
                    return info

            # If no MP3, use highest available (excluding FLAC)
            for info in sorted_infos:
                if info.codec and info.codec.lower() not in ("flac", "flac-mp4"):
                    return info

            # Last resort: highest available
            return sorted_infos[0]

        # Balanced (default): Prefer ~192kbps AAC, or medium quality MP3
        # Look for bitrate around 192kbps (within range 128-256)
        balanced_infos = [
            info
            for info in sorted_infos
            if info.bitrate_in_kbps and 128 <= info.bitrate_in_kbps <= 256
        ]
        if balanced_infos:
            # Prefer AAC over MP3 at similar bitrate (include MP4 container variants)
            for codec in ("aac-mp4", "aac", "he-aac-mp4", "he-aac", "mp3"):
                for info in balanced_infos:
                    if info.codec and info.codec.lower() == codec:
                        return info
            return balanced_infos[0]

        # Fallback to highest available if no balanced option
        return sorted_infos[0] if sorted_infos else None

    def _get_content_type(self, codec: str | None) -> tuple[ContentType, ContentType]:
        """Determine container and codec type from Kion API codec string.

        Kion API returns codec strings like "flac-mp4" (FLAC in MP4 container),
        "aac-mp4" (AAC in MP4 container), or plain "flac", "mp3", "aac".

        :param codec: Codec string from Kion API.
        :return: Tuple of (content_type/container, codec_type).
        """
        if not codec:
            return ContentType.UNKNOWN, ContentType.UNKNOWN

        codec_lower = codec.lower()

        # MP4 container variants: codec is inside an MP4 container
        if codec_lower == "flac-mp4":
            return ContentType.MP4, ContentType.FLAC
        if codec_lower in ("aac-mp4", "he-aac-mp4"):
            return ContentType.MP4, ContentType.AAC

        # Plain single-codec formats: codec is implied by content_type, no separate codec_type
        if codec_lower == "flac":
            return ContentType.FLAC, ContentType.UNKNOWN
        if codec_lower in ("mp3", "mpeg"):
            return ContentType.MP3, ContentType.UNKNOWN
        if codec_lower in ("aac", "he-aac"):
            return ContentType.AAC, ContentType.UNKNOWN

        return ContentType.UNKNOWN, ContentType.UNKNOWN

    def _get_audio_params(self, codec: str | None) -> tuple[int, int]:
        """Return (sample_rate, bit_depth) defaults based on codec string.

        The Kion get-file-info API does not return sample rate or bit depth,
        so we use codec-based defaults. These values help the core select the
        correct PCM output format and avoid unnecessary resampling.

        :param codec: Codec string from Kion API (e.g. "flac-mp4", "flac", "mp3").
        :return: Tuple of (sample_rate, bit_depth).
        """
        if codec and codec.lower() == "flac-mp4":
            return 48000, 24
        # CD-quality defaults for all other codecs
        return 44100, 16

    def _build_audio_format(self, codec: str | None, bit_rate: int = 0) -> AudioFormat:
        """Build AudioFormat with content type and codec-based audio params.

        :param codec: Codec string from Kion API (e.g. "flac-mp4", "flac", "mp3").
        :param bit_rate: Bitrate in kbps (0 for variable/unknown).
        :return: Configured AudioFormat instance.
        """
        content_type, codec_type = self._get_content_type(codec)
        sample_rate, bit_depth = self._get_audio_params(codec)
        return AudioFormat(
            content_type=content_type,
            codec_type=codec_type,
            bit_rate=bit_rate,
            sample_rate=sample_rate,
            bit_depth=bit_depth,
        )

    async def _refresh_encrypted_url(
        self,
        track_item_id: str,
        current_url: str,
        current_key_hex: str,
        http_status: int,
        bytes_yielded: int,
        attempt: int,
        max_retries: int,
    ) -> tuple[str, str] | None:
        """Re-fetch an expired encrypted stream URL.

        Called when the CDN responds with 4xx (URL expired or access revoked).

        :return: (new_url, new_key_hex) on success, or None if retries exhausted.
        """
        if attempt >= max_retries:
            return None
        raw_track_id = self._track_id_from_item_id(track_item_id)
        self.logger.warning(
            "Encrypted stream URL expired (HTTP %d) at %d bytes (attempt %d/%d) — re-fetching",
            http_status,
            bytes_yielded,
            attempt + 1,
            max_retries,
        )
        token = BYPASS_THROTTLER.set(True)
        try:
            file_info = await self.client.get_track_file_info_lossless(raw_track_id)
        finally:
            BYPASS_THROTTLER.reset(token)
        if file_info and file_info.get("url"):
            return file_info["url"], file_info.get("key", current_key_hex)
        return None

    async def _decrypt_response_stream(
        self,
        response: Any,
        key_bytes: bytes,
        block_size: int,
        bytes_delivered: int,
    ) -> AsyncGenerator[bytes, None]:
        """Decrypt one HTTP response and yield plaintext chunks.

        Aligns the AES-CTR counter to the correct block for resumption.
        If the server ignores a Range header (200 instead of 206), resets the
        counter to 0 and skips the already-delivered prefix transparently.

        :param response: aiohttp ClientResponse (open context manager).
        :param key_bytes: Raw AES key bytes.
        :param block_size: AES block size (16 for CTR mode).
        :param bytes_delivered: Total plaintext bytes already sent to the caller.
        :return: Async generator yielding decrypted audio bytes.
        """
        block_start = (bytes_delivered // block_size) * block_size
        block_skip = bytes_delivered - block_start

        if block_start > 0 and response.status == 200:
            self.logger.warning(
                "Server ignored Range header at %d bytes (200 instead of 206)"
                " — restarting decrypt from position 0, skipping %d already-sent bytes",
                block_start,
                bytes_delivered,
            )
            block_skip = bytes_delivered
            block_num = (0).to_bytes(block_size, "big")
        else:
            block_num = (block_start // block_size).to_bytes(block_size, "big")

        decryptor = Cipher(algorithms.AES(key_bytes), modes.CTR(block_num)).decryptor()
        carry_skip = block_skip
        async for chunk in response.content.iter_chunked(_CHUNK_SIZE):
            decrypted = decryptor.update(chunk)
            if carry_skip > 0:
                skip = min(carry_skip, len(decrypted))
                decrypted = decrypted[skip:]
                carry_skip -= skip
            if decrypted:
                yield decrypted
        final = decryptor.finalize()
        if final:
            yield final

    def _handle_stream_error(
        self,
        err: Exception,
        attempt: int,
        max_retries: int,
        bytes_yielded: int,
        delays: tuple[float, ...],
        label: str,
    ) -> tuple[int, float]:
        """Increment retry counter, log a warning, or raise if retries are exhausted.

        :param err: The exception that caused the retry.
        :param attempt: Current retry attempt count (0-based).
        :param max_retries: Maximum number of retries allowed.
        :param bytes_yielded: Bytes delivered so far (for log context).
        :param delays: Backoff delay sequence to pick from.
        :param label: Short verb describing the failure (e.g. "dropped", "stalled").
        :return: (new_attempt, retry_delay) tuple when retrying.
        :raises MediaNotFoundError: When attempt count exceeds max_retries.
        """
        delay = delays[min(attempt, len(delays) - 1)]
        attempt += 1
        if attempt <= max_retries:
            self.logger.warning(
                "Encrypted stream %s at %d bytes (attempt %d/%d) — retrying",
                label,
                bytes_yielded,
                attempt,
                max_retries,
            )
            return attempt, delay
        raise MediaNotFoundError(f"Encrypted stream {label} after retries were exhausted") from err

    @staticmethod
    def _is_content_range_eof(headers: Any, window_end: int) -> bool:
        """Return True when Content-Range indicates *window_end* reached the last file byte.

        Parses ``Content-Range: bytes start-end/total`` and checks whether
        ``window_end >= total - 1``.  Returns False on any malformed header so
        the caller falls back to the next window safely.
        """
        content_range = headers.get("Content-Range", "")
        if not content_range.startswith("bytes "):
            return False
        try:
            _, range_spec = content_range.split(" ", 1)
            _, total_str = range_spec.split("/", 1)
            total_str = total_str.strip()
            return total_str.isdigit() and window_end >= int(total_str) - 1
        except ValueError:
            return False

    async def get_audio_stream(  # noqa: PLR0915
        self, streamdetails: StreamDetails, seek_position: int = 0
    ) -> AsyncGenerator[bytes, None]:
        """Return the audio stream for the provider item with on-the-fly decryption.

        Downloads and decrypts the encrypted stream in windowed Range requests of
        _RANGE_WINDOW bytes each. Kion CDN drops TCP every ~6-7 MB per connection;
        keeping each request at 4 MB prevents that limit from being reached.

        On connection drop (ClientPayloadError, ServerDisconnectedError), the current
        window is retried with a flat short backoff (0.5s/1.0s/2.0s).
        On read stall (asyncio.TimeoutError), the current window is retried with
        exponential backoff (2s/4s/8s).
        On URL expiry (HTTP 4xx), re-fetches the URL and resumes from bytes_yielded.
        Up to max_retries retries per window; the retry counter resets on each
        successful window so long tracks get the same protection as short ones.

        If the server ignores a Range header (returns 200 instead of 206), the decryptor
        is reset to position 0 so decryption stays consistent with the restarted byte stream.

        :param streamdetails: Stream details containing encrypted URL and key.
        :param seek_position: Always 0 (seeking delegated to ffmpeg via allow_seek=True).
        :return: Async generator yielding decrypted audio bytes.
        """
        encrypted_url: str = streamdetails.data["encrypted_url"]
        track_item_id: str = streamdetails.item_id
        key_hex: str = streamdetails.data["decryption_key"]
        key_bytes = bytes.fromhex(key_hex)
        if len(key_bytes) not in (16, 24, 32):
            raise MediaNotFoundError(f"Unsupported AES key length: {len(key_bytes)} bytes")

        block_size = 16  # AES-CTR block size in bytes
        max_retries = 6
        bytes_yielded = 0  # total decrypted bytes delivered to caller
        attempt = 0  # retry counter; resets to 0 after each successful window
        retry_delay: float = 0.0

        while True:
            if attempt > 0:
                await asyncio.sleep(retry_delay)

            block_start = (bytes_yielded // block_size) * block_size
            window_end = block_start + _RANGE_WINDOW - 1
            headers = {"Range": f"bytes={block_start}-{window_end}"}

            try:
                async with self.mass.http_session.get(
                    encrypted_url, headers=headers, timeout=_STREAM_TIMEOUT
                ) as response:
                    if response.status in (401, 403, 410):
                        # URL expired — re-fetch via helper and retry
                        refreshed = await self._refresh_encrypted_url(
                            track_item_id,
                            encrypted_url,
                            key_hex,
                            response.status,
                            bytes_yielded,
                            attempt,
                            max_retries,
                        )
                        if refreshed is None:
                            raise MediaNotFoundError(
                                f"Encrypted stream URL expired (HTTP {response.status}) "
                                "after retries exhausted"
                            )
                        encrypted_url, key_hex = refreshed
                        try:
                            key_bytes = bytes.fromhex(key_hex)
                        except ValueError as err:
                            raise MediaNotFoundError(
                                "Invalid decryption key format after URL refresh"
                            ) from err
                        retry_delay = 0.0
                        attempt += 1  # consume one retry slot, same as TCP-drop path
                        continue
                    if response.status == 416:
                        return  # Range Not Satisfiable — file size is exact window multiple
                    try:
                        response.raise_for_status()
                    except Exception as err:
                        raise MediaNotFoundError(
                            f"Failed to fetch encrypted stream: {err}"
                        ) from err

                    bytes_before = bytes_yielded
                    # block_skip = bytes re-downloaded for AES-block alignment.
                    # Needed below to compute actual HTTP bytes received.
                    block_skip = bytes_before - block_start
                    async for chunk in self._decrypt_response_stream(
                        response, key_bytes, block_size, bytes_yielded
                    ):
                        bytes_yielded += len(chunk)
                        yield chunk

                    # window complete — check if EOF
                    window_got = bytes_yielded - bytes_before
                    # received = actual HTTP bytes the server sent for this Range
                    # request.  window_got alone understates the window when
                    # block_skip > 0 (reconnect at a non-AES-block boundary):
                    # the decryptor skips block_skip bytes, so window_got would be
                    # smaller than _RANGE_WINDOW even for a full server response,
                    # causing premature stream termination without this correction.
                    received = window_got + block_skip
                    if response.status == 200 or received < _RANGE_WINDOW:
                        return  # full file received or last partial window
                    # Exact-boundary guard: if file size is an exact multiple of
                    # _RANGE_WINDOW the size check above won't catch EOF.
                    # Use Content-Range to confirm no bytes remain.
                    if self._is_content_range_eof(response.headers, window_end):
                        return
                    # more data expected: advance to next window
                    attempt = 0
                    retry_delay = 0.0

            except asyncio.CancelledError:
                raise  # propagate cancellation immediately, do not retry
            except (ClientPayloadError, ServerDisconnectedError) as err:
                attempt, retry_delay = self._handle_stream_error(
                    err, attempt, max_retries, bytes_yielded, _TCP_DROP_DELAYS, "dropped"
                )
            except TimeoutError as err:
                attempt, retry_delay = self._handle_stream_error(
                    err, attempt, max_retries, bytes_yielded, _STALL_DELAYS, "stalled"
                )
