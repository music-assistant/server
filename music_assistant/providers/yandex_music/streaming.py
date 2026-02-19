"""Streaming operations for Yandex Music."""

from __future__ import annotations

import asyncio
import contextlib
import os
import tempfile
import threading
import time
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiohttp
from Crypto.Cipher import AES
from music_assistant_models.enums import ContentType, StreamType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.streamdetails import StreamDetails

from . import mp4_seek
from .constants import (
    CONF_PRELOAD_BUFFER_MB,
    CONF_QUALITY,
    CONF_STREAM_BUFFER_MB,
    CONF_STREAMING_MODE,
    QUALITY_EFFICIENT,
    QUALITY_HIGH,
    QUALITY_SUPERB,
    RADIO_TRACK_ID_SEP,
    STREAMING_MODE_BUFFERED,
    STREAMING_MODE_DIRECT,
    STREAMING_MODE_PRELOAD,
)

if TYPE_CHECKING:
    from yandex_music import DownloadInfo

    from .provider import YandexMusicProvider

# Temp file prefix for easy identification and cleanup
TEMP_FILE_PREFIX = "yandex_audio_"


class YandexMusicStreamingManager:
    """Manages Yandex Music streaming operations."""

    def __init__(self, provider: YandexMusicProvider) -> None:
        """Initialize streaming manager.

        :param provider: The Yandex Music provider instance.
        """
        self.provider = provider
        self.client = provider.client
        self.mass = provider.mass
        self.logger = provider.logger
        # Track temp files for cleanup: item_id -> temp_file_path
        self._temp_files: dict[str, str] = {}
        self._temp_files_lock = threading.Lock()
        # Shared aiohttp session for streaming downloads (reuses TCP connections to CDN)
        self._streaming_session: aiohttp.ClientSession | None = None
        self._streaming_session_lock = asyncio.Lock()

    async def _get_streaming_session(self) -> aiohttp.ClientSession:
        """Get or create the shared streaming aiohttp session."""
        if self._streaming_session is not None and not self._streaming_session.closed:
            return self._streaming_session
        async with self._streaming_session_lock:
            if self._streaming_session is None or self._streaming_session.closed:
                self._streaming_session = aiohttp.ClientSession()
            return self._streaming_session

    async def close(self) -> None:
        """Close the streaming session and clean up resources."""
        if self._streaming_session and not self._streaming_session.closed:
            await self._streaming_session.close()
            self._streaming_session = None

    def _track_id_from_item_id(self, item_id: str) -> str:
        """Extract API track ID from item_id (may be track_id@station_id for My Wave)."""
        if RADIO_TRACK_ID_SEP in item_id:
            return item_id.split(RADIO_TRACK_ID_SEP, 1)[0]
        return item_id

    async def _get_content_length(self, url: str) -> int | None:
        """Get Content-Length from URL via HEAD request.

        :param url: URL to check.
        :return: Content length in bytes, or None if unavailable.
        """
        timeout = aiohttp.ClientTimeout(total=30, connect=10)
        try:
            async with self.mass.http_session.head(
                url, timeout=timeout, allow_redirects=True
            ) as response:
                if response.status == 200:
                    content_length = response.headers.get("Content-Length")
                    if content_length:
                        return int(content_length)
        except Exception as err:
            self.logger.debug("Failed to get Content-Length for %s: %s", url[:80], err)
        return None

    async def _download_and_decrypt_to_file(
        self,
        encrypted_url: str,
        decryption_key: str,
        item_id: str,
        content_type: ContentType = ContentType.FLAC,
    ) -> str:
        """Download encrypted data, decrypt, and save to a temp file.

        Uses a shared streaming aiohttp session (not self.mass.http_session)
        to reuse TCP connections to Yandex CDN while keeping streaming traffic
        isolated from the shared session's connection pool.

        :param encrypted_url: URL of the encrypted file.
        :param decryption_key: Hex-encoded AES-256 decryption key.
        :param item_id: Track item ID for logging.
        :param content_type: Container content type for file extension.
        :return: Path to the decrypted temp file.
        :raises MediaNotFoundError: If download fails.
        """
        key_bytes = bytes.fromhex(decryption_key)
        nonce = bytes(12)
        cipher = AES.new(key=key_bytes, nonce=nonce, mode=AES.MODE_CTR)

        chunk_size = 65536
        timeout = aiohttp.ClientTimeout(total=None, connect=30, sock_read=600)

        # Create temp file with extension matching the container format
        suffix = f".{content_type.value}" if content_type != ContentType.UNKNOWN else ".flac"
        temp_fd, temp_path = tempfile.mkstemp(prefix=TEMP_FILE_PREFIX, suffix=suffix)

        try:
            session = await self._get_streaming_session()
            async with session.get(encrypted_url, timeout=timeout) as response:
                if response.status != 200:
                    msg = f"Failed to download encrypted track: HTTP {response.status}"
                    self.logger.error(msg)
                    raise MediaNotFoundError(msg)

                total_bytes = 0
                fd = temp_fd
                temp_fd = -1  # fdopen takes ownership; prevent double-close on error
                with os.fdopen(fd, "wb") as f:
                    async for encrypted_chunk in response.content.iter_chunked(chunk_size):
                        decrypted_chunk = cipher.decrypt(encrypted_chunk)
                        f.write(decrypted_chunk)
                        total_bytes += len(decrypted_chunk)

                self.logger.info(
                    "Preloaded and decrypted track %s to %s (%d bytes)",
                    item_id,
                    temp_path,
                    total_bytes,
                )
                return temp_path

        except Exception:
            # Close fd if not yet closed by os.fdopen
            if temp_fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(temp_fd)
            # Clean up temp file on error
            with contextlib.suppress(OSError):
                os.unlink(temp_path)  # noqa: PTH108
            raise

    def _replace_temp_file(self, item_id: str, new_path: str) -> None:
        """Atomically replace the tracked temp file for an item, deleting the old one.

        :param item_id: Track item ID.
        :param new_path: Path to the new temp file.
        """
        with self._temp_files_lock:
            old_path = self._temp_files.pop(item_id, None)
            self._temp_files[item_id] = new_path
        if old_path:
            with contextlib.suppress(OSError):
                Path(old_path).unlink(missing_ok=True)

    def cleanup_temp_file(self, item_id: str) -> None:
        """Clean up temp file for a track after playback.

        :param item_id: Track item ID.
        """
        with self._temp_files_lock:
            temp_path = self._temp_files.pop(item_id, None)
        if temp_path:
            try:
                Path(temp_path).unlink(missing_ok=True)
                self.logger.debug("Cleaned up temp file for %s: %s", item_id, temp_path)
            except Exception as err:
                self.logger.warning("Failed to clean up temp file %s: %s", temp_path, err)

    def cleanup_stale_temp_files(self, max_age_seconds: int = 600) -> None:
        """Clean up temp files older than max_age_seconds.

        :param max_age_seconds: Maximum age in seconds (default: 600 = 10 minutes).
        """
        now = time.time()
        with self._temp_files_lock:
            snapshot = dict(self._temp_files)
        stale_ids: list[str] = []
        for item_id, temp_path in snapshot.items():
            try:
                file_age = now - Path(temp_path).stat().st_mtime
                if file_age > max_age_seconds:
                    stale_ids.append(item_id)
            except OSError:
                stale_ids.append(item_id)
        for item_id in stale_ids:
            self.cleanup_temp_file(item_id)

    def cleanup_all_temp_files(self) -> None:
        """Clean up all tracked temp files (called on provider unload)."""
        with self._temp_files_lock:
            item_ids = list(self._temp_files.keys())
        for item_id in item_ids:
            self.cleanup_temp_file(item_id)

    async def get_stream_details(self, item_id: str) -> StreamDetails:
        """Get stream details for a track.

        :param item_id: Track ID or composite track_id@station_id for My Wave.
        :return: StreamDetails for the track (item_id preserved for on_streamed).
        :raises MediaNotFoundError: If stream URL cannot be obtained.
        """
        # Clean up stale temp files periodically
        self.cleanup_stale_temp_files()

        track_id = self._track_id_from_item_id(item_id)
        track = await self.provider.get_track(item_id)
        if not track:
            raise MediaNotFoundError(f"Track {item_id} not found")

        quality = self.provider.config.get_value(CONF_QUALITY)
        quality_str = str(quality) if quality is not None else None
        preferred_normalized = (quality_str or "").strip().lower()

        # Check for superb (lossless) quality
        want_lossless = preferred_normalized in (QUALITY_SUPERB, "superb")

        # Backward compatibility: also check old "lossless" value
        if "lossless" in preferred_normalized:
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
                    content_type = audio_format.content_type

                    # Handle encrypted URLs from encraw transport
                    if needs_decryption and "key" in file_info:
                        streaming_mode = (
                            self.provider.config.get_value(CONF_STREAMING_MODE)
                            or STREAMING_MODE_BUFFERED
                        )

                        # For Preload mode: check file size and preload if within limit
                        if streaming_mode == STREAMING_MODE_PRELOAD:
                            max_size_mb = int(
                                self.provider.config.get_value(CONF_PRELOAD_BUFFER_MB) or 100  # type: ignore[arg-type]
                            )
                            max_size_bytes = max_size_mb * 1024 * 1024

                            # Check file size first
                            content_length = await self._get_content_length(url)
                            if content_length and content_length <= max_size_bytes:
                                self.logger.info(
                                    "Preloading encrypted %s for track %s (size=%dMB, limit=%dMB)",
                                    codec,
                                    track_id,
                                    content_length // (1024 * 1024),
                                    max_size_mb,
                                )
                                # Download, decrypt, save to temp file
                                temp_path = await self._download_and_decrypt_to_file(
                                    url, file_info["key"], item_id, content_type
                                )
                                self._replace_temp_file(item_id, temp_path)

                                return StreamDetails(
                                    item_id=item_id,
                                    provider=self.provider.instance_id,
                                    audio_format=audio_format,
                                    stream_type=StreamType.LOCAL_FILE,
                                    duration=track.duration,
                                    path=temp_path,
                                    can_seek=True,
                                    allow_seek=True,
                                )
                            # File too large or size unknown - fall back to buffered
                            size_info = (
                                f"{content_length // (1024 * 1024)}MB"
                                if content_length
                                else "unknown size"
                            )
                            self.logger.info(
                                "Track %s (%s) exceeds preload limit (%dMB), "
                                "using buffered streaming",
                                track_id,
                                size_info,
                                max_size_mb,
                            )
                            # Return with streaming_mode_override to force buffered mode
                            # MP4 container supports seek via sample table
                            can_seek = codec.lower() in ("flac-mp4", "mp4")
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
                                    "streaming_mode_override": STREAMING_MODE_BUFFERED,
                                },
                                can_seek=can_seek,
                                allow_seek=True,
                            )

                        self.logger.info(
                            "Streaming encrypted %s for track %s - will decrypt on-the-fly",
                            codec,
                            track_id,
                        )
                        # Return StreamType.CUSTOM for streaming decryption
                        # Store encrypted URL and decryption key in data for get_audio_stream
                        # MP4 container supports seek via sample table
                        can_seek = codec.lower() in ("flac-mp4", "mp4")
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
                            can_seek=can_seek,
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
        )

    def _select_best_quality(
        self, download_infos: list[Any], preferred_quality: str | None
    ) -> DownloadInfo | None:
        """Select the best quality download info based on user preference.

        :param download_infos: List of DownloadInfo objects.
        :param preferred_quality: User's quality preference (efficient/balanced/superb).
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
        if preferred_normalized == QUALITY_SUPERB or "lossless" in preferred_normalized:
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
        """Determine container and codec type from Yandex API codec string.

        Yandex API returns codec strings like "flac-mp4" (FLAC in MP4 container),
        "aac-mp4" (AAC in MP4 container), or plain "flac", "mp3", "aac".

        :param codec: Codec string from Yandex API.
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

        # Plain formats: container and codec are the same
        if codec_lower == "flac":
            return ContentType.FLAC, ContentType.UNKNOWN
        if codec_lower in ("mp3", "mpeg"):
            return ContentType.MP3, ContentType.UNKNOWN
        if codec_lower in ("aac", "he-aac"):
            return ContentType.AAC, ContentType.UNKNOWN

        return ContentType.UNKNOWN, ContentType.UNKNOWN

    def _get_audio_params(self, codec: str | None) -> tuple[int, int]:
        """Return (sample_rate, bit_depth) defaults based on codec string.

        The Yandex get-file-info API does not return sample rate or bit depth,
        so we use codec-based defaults. These values help the core select the
        correct PCM output format and avoid unnecessary resampling.

        :param codec: Codec string from Yandex API (e.g. "flac-mp4", "flac", "mp3").
        :return: Tuple of (sample_rate, bit_depth).
        """
        if codec and codec.lower() == "flac-mp4":
            return 48000, 24
        # CD-quality defaults for all other codecs
        return 44100, 16

    def _build_audio_format(self, codec: str | None, bit_rate: int = 0) -> AudioFormat:
        """Build AudioFormat with content type and codec-based audio params.

        :param codec: Codec string from Yandex API (e.g. "flac-mp4", "flac", "mp3").
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

    def _prepare_cipher(
        self, streamdetails: StreamDetails, start_block: int = 0
    ) -> tuple[Any, str, str]:
        """Prepare AES-256-CTR cipher and return (cipher, encrypted_url, codec).

        A fresh cipher is created per streaming call. CTR mode requires the counter
        to start from zero for each complete download — this is safe because retries
        restart the full HTTP request (and thus the ciphertext) from the beginning.

        :param streamdetails: Stream details containing encrypted URL and key.
        :param start_block: AES block number (byte_offset // 16) to initialise counter.
        :return: Tuple of (cipher, encrypted_url, codec).
        """
        encrypted_url: str = streamdetails.data["encrypted_url"]
        key_hex: str = streamdetails.data["decryption_key"]
        codec: str = streamdetails.data.get("codec", "flac")
        key_bytes = bytes.fromhex(key_hex)
        nonce = bytes(12)
        cipher = AES.new(key=key_bytes, nonce=nonce, mode=AES.MODE_CTR, initial_value=start_block)
        return cipher, encrypted_url, codec

    async def get_audio_stream(
        self, streamdetails: StreamDetails, seek_position: int = 0
    ) -> AsyncGenerator[bytes, None]:
        """Return the audio stream for the provider item with decryption.

        Dispatches to the configured streaming mode: direct or buffered.
        For MP4-container tracks with a non-zero seek_position, dispatches to
        _stream_buffered_seek which parses the moov atom and issues a Range request.
        Preload mode is handled entirely in get_stream_details() which downloads,
        decrypts, and returns a LOCAL_FILE StreamType — so get_audio_stream is never
        called for preload.

        :param streamdetails: Stream details containing encrypted URL and key.
        :param seek_position: Seek position in seconds; 0 means play from start.
        :return: Async generator yielding decrypted audio bytes.
        """
        mode = (
            streamdetails.data.get("streaming_mode_override")
            or self.provider.config.get_value(CONF_STREAMING_MODE)
            or STREAMING_MODE_BUFFERED
        )
        codec = streamdetails.data.get("codec", "")
        is_mp4 = codec.lower() in ("flac-mp4", "mp4")

        if seek_position > 0 and is_mp4 and mode != STREAMING_MODE_DIRECT:
            gen = self._stream_buffered_seek(streamdetails, seek_position)
        elif mode == STREAMING_MODE_DIRECT:
            gen = self._stream_direct(streamdetails)
        else:
            gen = self._stream_buffered(streamdetails)

        async for chunk in gen:
            yield chunk

    async def _stream_direct(self, streamdetails: StreamDetails) -> AsyncGenerator[bytes, None]:
        """Stream and decrypt on-the-fly (original behavior).

        Download and decryption are coupled — each chunk is decrypted as it arrives.
        Best for fast networks and powerful CPUs. Retries on CDN connection drops
        (ClientPayloadError) by resuming the download with an HTTP Range request.

        :param streamdetails: Stream details containing encrypted URL and key.
        """
        _, encrypted_url, codec = self._prepare_cipher(streamdetails)

        self.logger.info(
            "Starting direct streaming decryption for track %s (codec=%s)",
            streamdetails.item_id,
            codec,
        )

        chunk_size = 65536
        total_bytes = 0
        max_retries = 3
        timeout = aiohttp.ClientTimeout(total=None, connect=30, sock_read=600)

        for attempt in range(max_retries + 1):
            try:
                # Resume from byte-aligned block boundary for correct CTR decryption
                resume_block = total_bytes // 16
                resume_byte = resume_block * 16
                skip_bytes = total_bytes - resume_byte
                cipher, _, _ = self._prepare_cipher(streamdetails, start_block=resume_block)

                headers = {}
                if total_bytes > 0:
                    headers["Range"] = f"bytes={resume_byte}-"

                session = await self._get_streaming_session()
                async with session.get(encrypted_url, timeout=timeout, headers=headers) as response:
                    if response.status not in (200, 206):
                        msg = f"Failed to stream encrypted track: HTTP {response.status}"
                        self.logger.error(msg)
                        raise MediaNotFoundError(msg)

                    if total_bytes == 0:
                        self.logger.debug("Started streaming from %s", encrypted_url[:100])

                    first_chunk = True
                    async for encrypted_chunk in response.content.iter_chunked(chunk_size):
                        decrypted_chunk = cipher.decrypt(encrypted_chunk)
                        if first_chunk and skip_bytes:
                            decrypted_chunk = decrypted_chunk[skip_bytes:]
                            first_chunk = False
                        total_bytes += len(decrypted_chunk)
                        yield decrypted_chunk

                self.logger.info(
                    "Completed direct streaming for track %s: %d bytes total",
                    streamdetails.item_id,
                    total_bytes,
                )
                return

            except aiohttp.ClientPayloadError as err:
                if attempt >= max_retries:
                    self.logger.exception(
                        "Error during direct streaming for track %s: %s",
                        streamdetails.item_id,
                        err,
                    )
                    raise
                self.logger.warning(
                    "Stream dropped at %d bytes for track %s, retry %d/%d: %s",
                    total_bytes,
                    streamdetails.item_id,
                    attempt + 1,
                    max_retries,
                    err,
                )
                await asyncio.sleep(1)

            except Exception as err:
                self.logger.exception(
                    "Error during direct streaming for track %s: %s",
                    streamdetails.item_id,
                    err,
                )
                raise

    async def _stream_buffered(  # noqa: PLR0915
        self, streamdetails: StreamDetails
    ) -> AsyncGenerator[bytes, None]:
        """Download and decrypt via async queue, decoupling download from consumption.

        A background task downloads and decrypts chunks into a bounded queue.
        The consumer yields from the queue at its own pace. Backpressure is handled
        by the queue's maxsize — download blocks when the queue is full. Retries on
        CDN connection drops (ClientPayloadError) by resuming with HTTP Range.

        :param streamdetails: Stream details containing encrypted URL and key.
        """
        _, encrypted_url, codec = self._prepare_cipher(streamdetails)

        self.logger.info(
            "Starting buffered streaming for track %s (codec=%s)",
            streamdetails.item_id,
            codec,
        )

        chunk_size = 65536
        buffer_mb = int(self.provider.config.get_value(CONF_STREAM_BUFFER_MB) or 8)  # type: ignore[arg-type]
        queue_max = (buffer_mb * 1024 * 1024) // chunk_size

        self.logger.debug(
            "Buffered streaming buffer: %dMB (%d chunks x %dKB)",
            buffer_mb,
            queue_max,
            chunk_size // 1024,
        )

        queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=queue_max)
        error_holder: list[BaseException | None] = [None]
        sentinel = None
        timeout = aiohttp.ClientTimeout(total=None, connect=30, sock_read=600)
        max_retries = 3

        async def _download_and_decrypt() -> None:
            bytes_downloaded = 0
            try:
                for attempt in range(max_retries + 1):
                    try:
                        # Resume from block-aligned boundary for correct CTR decryption
                        resume_block = bytes_downloaded // 16
                        resume_byte = resume_block * 16
                        skip_bytes = bytes_downloaded - resume_byte
                        cipher, _, _ = self._prepare_cipher(streamdetails, start_block=resume_block)

                        headers = {}
                        if bytes_downloaded > 0:
                            headers["Range"] = f"bytes={resume_byte}-"

                        session = await self._get_streaming_session()
                        async with session.get(
                            encrypted_url, timeout=timeout, headers=headers
                        ) as response:
                            if response.status not in (200, 206):
                                msg = f"Failed to stream encrypted track: HTTP {response.status}"
                                error_holder[0] = MediaNotFoundError(msg)
                                return

                            first_chunk = True
                            async for encrypted_chunk in response.content.iter_chunked(chunk_size):
                                decrypted = cipher.decrypt(encrypted_chunk)
                                if first_chunk and skip_bytes:
                                    decrypted = decrypted[skip_bytes:]
                                    first_chunk = False
                                bytes_downloaded += len(decrypted)
                                await queue.put(decrypted)
                        return  # Download complete

                    except aiohttp.ClientPayloadError as exc:
                        if attempt >= max_retries:
                            error_holder[0] = exc
                            return
                        self.logger.warning(
                            "Stream dropped at %d bytes for track %s, retry %d/%d: %s",
                            bytes_downloaded,
                            streamdetails.item_id,
                            attempt + 1,
                            max_retries,
                            exc,
                        )
                        await asyncio.sleep(1)

                    except Exception as exc:
                        error_holder[0] = exc
                        return
            finally:
                await queue.put(sentinel)

        task = asyncio.create_task(_download_and_decrypt())
        total_bytes = 0
        try:
            while True:
                item = await queue.get()
                if item is sentinel:
                    break
                total_bytes += len(item)
                yield item

            if error_holder[0]:
                raise error_holder[0]

            self.logger.info(
                "Completed buffered streaming for track %s: %d bytes total",
                streamdetails.item_id,
                total_bytes,
            )
        except Exception as err:
            self.logger.exception(
                "Error during buffered streaming for track %s: %s",
                streamdetails.item_id,
                err,
            )
            raise
        finally:
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    async def _fetch_and_decrypt_partial(
        self,
        encrypted_url: str,
        key_hex: str,
        max_bytes: int = 1_048_576,
    ) -> bytes:
        """Fetch and decrypt the first max_bytes of an encrypted file.

        Used to read the moov atom without downloading the full file.

        :param encrypted_url: URL of the encrypted file.
        :param key_hex: Hex-encoded AES-256 decryption key.
        :param max_bytes: Maximum bytes to fetch (default 1 MB).
        :return: Decrypted bytes.
        """
        key_bytes = bytes.fromhex(key_hex)
        nonce = bytes(12)
        cipher = AES.new(key=key_bytes, nonce=nonce, mode=AES.MODE_CTR, initial_value=0)
        headers = {"Range": f"bytes=0-{max_bytes - 1}"}
        timeout = aiohttp.ClientTimeout(total=60, connect=15)
        session = await self._get_streaming_session()
        async with session.get(encrypted_url, headers=headers, timeout=timeout) as response:
            if response.status not in (200, 206):
                return b""
            encrypted = await response.read()
        return cipher.decrypt(encrypted)

    def _patch_stco(self, moov_data: bytes, delta: int) -> bytes:
        """Subtract delta from all stco/co64 chunk offsets in the moov atom.

        Used to adjust absolute file offsets after seeking. After serving a modified
        moov prefix to ffmpeg and then streaming from seek_byte onwards, chunk offsets
        must be shifted so they are relative to the new stream start.

        :param moov_data: Bytes containing the moov atom (and any preceding boxes).
        :param delta: Value to subtract from every chunk offset entry.
        :return: Modified bytes with adjusted offsets.
        """
        return mp4_seek.patch_stco(moov_data, delta)

    async def _stream_buffered_seek(  # noqa: PLR0915
        self, streamdetails: StreamDetails, seek_position: int
    ) -> AsyncGenerator[bytes, None]:
        """Stream an MP4-container track starting from seek_position seconds.

        Fetches the first 1 MB to read the moov atom, finds the file byte offset
        for seek_position via mp4_seek, patches moov chunk offsets so ffmpeg can
        parse the modified stream, then streams the remainder from that offset.
        Falls back to _stream_buffered (from the beginning) if moov parsing fails.

        :param streamdetails: Stream details containing encrypted URL and key.
        :param seek_position: Target playback position in seconds.
        """
        encrypted_url: str = streamdetails.data["encrypted_url"]
        key_hex: str = streamdetails.data["decryption_key"]
        codec: str = streamdetails.data.get("codec", "flac-mp4")

        self.logger.info(
            "Seek request for track %s: %.1fs (codec=%s)",
            streamdetails.item_id,
            seek_position,
            codec,
        )

        # Step 1: Fetch and decrypt the first 1 MB to locate moov
        try:
            first_mb = await self._fetch_and_decrypt_partial(encrypted_url, key_hex)
        except Exception as err:
            self.logger.warning(
                "Seek fallback (fetch error) for track %s: %s",
                streamdetails.item_id,
                err,
            )
            async for chunk in self._stream_buffered(streamdetails):
                yield chunk
            return

        # Step 2: Find the byte offset for the requested seek position
        seek_byte = mp4_seek.find_seek_byte(first_mb, seek_position)
        moov_end = mp4_seek.find_moov_end(first_mb)

        if seek_byte is None or moov_end is None:
            self.logger.warning(
                "Seek fallback (moov parse failed) for track %s at %.1fs",
                streamdetails.item_id,
                seek_position,
            )
            async for chunk in self._stream_buffered(streamdetails):
                yield chunk
            return

        self.logger.info(
            "Seek to %.1fs for track %s: byte offset %d, moov_end=%d",
            seek_position,
            streamdetails.item_id,
            seek_byte,
            moov_end,
        )

        # Step 3: Build modified moov prefix with adjusted chunk offsets.
        # The modified prefix (bytes 0..moov_end) is served to ffmpeg so it can
        # parse the container structure; then mdat data starts at seek_byte.
        # Chunk offsets in stco/co64 are absolute file positions, so we subtract
        # seek_byte and add moov_end to account for the prefix we serve.
        moov_prefix = first_mb[:moov_end]
        offset_delta = seek_byte - moov_end
        modified_moov = self._patch_stco(moov_prefix, offset_delta)

        yield modified_moov

        # Step 4: Stream encrypted data from seek_byte with correct CTR counter.
        # Align to AES block boundary (16 bytes).
        resume_block = seek_byte // 16
        resume_byte = resume_block * 16
        skip_in_first_chunk = seek_byte - resume_byte

        max_retries = 3
        bytes_streamed = 0
        timeout = aiohttp.ClientTimeout(total=None, connect=30, sock_read=600)
        chunk_size = 65536

        for attempt in range(max_retries + 1):
            try:
                current_byte = resume_byte + bytes_streamed
                current_block = current_byte // 16
                aligned_byte = current_block * 16
                skip_bytes = current_byte - aligned_byte
                cipher, _, _ = self._prepare_cipher(streamdetails, start_block=current_block)

                headers = {"Range": f"bytes={aligned_byte}-"}
                session = await self._get_streaming_session()
                async with session.get(encrypted_url, headers=headers, timeout=timeout) as response:
                    if response.status not in (200, 206):
                        msg = (
                            f"Seek stream HTTP {response.status} for track {streamdetails.item_id}"
                        )
                        raise MediaNotFoundError(msg)

                    first_chunk = True
                    async for encrypted_chunk in response.content.iter_chunked(chunk_size):
                        decrypted = cipher.decrypt(encrypted_chunk)
                        # Skip partial AES block and/or the initial seek_byte alignment gap
                        if first_chunk:
                            trim = skip_bytes + (skip_in_first_chunk if bytes_streamed == 0 else 0)
                            decrypted = decrypted[trim:]
                            first_chunk = False
                        bytes_streamed += len(decrypted)
                        yield decrypted
                return  # Stream complete

            except aiohttp.ClientPayloadError as err:
                if attempt >= max_retries:
                    self.logger.exception(
                        "Seek stream failed for track %s: %s",
                        streamdetails.item_id,
                        err,
                    )
                    raise
                self.logger.warning(
                    "Seek stream dropped at %d bytes for track %s, retry %d/%d: %s",
                    bytes_streamed,
                    streamdetails.item_id,
                    attempt + 1,
                    max_retries,
                    err,
                )
                await asyncio.sleep(1)

            except Exception as err:
                self.logger.exception(
                    "Seek stream error for track %s: %s",
                    streamdetails.item_id,
                    err,
                )
                raise
