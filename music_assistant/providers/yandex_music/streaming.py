"""Streaming operations for Yandex Music."""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any

import aiohttp
from Crypto.Cipher import AES
from music_assistant_models.enums import ContentType, StreamType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.streamdetails import StreamDetails

from .constants import (
    CONF_PRELOAD_BUFFER_MB,
    CONF_QUALITY,
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

    def _track_id_from_item_id(self, item_id: str) -> str:
        """Extract API track ID from item_id (may be track_id@station_id for My Wave)."""
        if RADIO_TRACK_ID_SEP in item_id:
            return item_id.split(RADIO_TRACK_ID_SEP, 1)[0]
        return item_id

    async def get_stream_details(self, item_id: str) -> StreamDetails:
        """Get stream details for a track.

        :param item_id: Track ID or composite track_id@station_id for My Wave.
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
                    content_type = self._get_content_type(codec)

                    # Handle encrypted URLs from encraw transport
                    if needs_decryption and "key" in file_info:
                        self.logger.info(
                            "Streaming encrypted FLAC for track %s (codec=%s) - "
                            "will decrypt on-the-fly",
                            track_id,
                            codec,
                        )
                        # Return StreamType.CUSTOM for streaming decryption
                        # Store encrypted URL and decryption key in data for get_audio_stream
                        return StreamDetails(
                            item_id=item_id,
                            provider=self.provider.instance_id,
                            audio_format=AudioFormat(
                                content_type=content_type,
                                bit_rate=0,  # FLAC is variable bitrate
                            ),
                            stream_type=StreamType.CUSTOM,
                            duration=track.duration,
                            data={
                                "encrypted_url": url,
                                "decryption_key": file_info["key"],
                                "codec": codec,
                            },
                            can_seek=False,  # Seeking not supported in streaming mode
                            allow_seek=False,
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
                        audio_format=AudioFormat(
                            content_type=content_type,
                            bit_rate=0,
                        ),
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

        content_type = self._get_content_type(selected_info.codec)
        bitrate = selected_info.bitrate_in_kbps or 0

        return StreamDetails(
            item_id=item_id,
            provider=self.provider.instance_id,
            audio_format=AudioFormat(
                content_type=content_type,
                bit_rate=bitrate,
            ),
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
            # Prefer AAC for efficiency, then MP3
            for codec in ("aac", "he-aac", "mp3"):
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
            # Prefer AAC over MP3 at similar bitrate
            for codec in ("aac", "mp3"):
                for info in balanced_infos:
                    if info.codec and info.codec.lower() == codec:
                        return info
            return balanced_infos[0]

        # Fallback to highest available if no balanced option
        return sorted_infos[0] if sorted_infos else None

    def _get_content_type(self, codec: str | None) -> ContentType:
        """Determine content type from codec string.

        :param codec: Codec string from Yandex API.
        :return: ContentType enum value.
        """
        if not codec:
            return ContentType.UNKNOWN

        codec_lower = codec.lower()
        if codec_lower in ("flac", "flac-mp4"):
            return ContentType.FLAC
        if codec_lower in ("mp3", "mpeg"):
            return ContentType.MP3
        if codec_lower == "aac":
            return ContentType.AAC

        return ContentType.UNKNOWN

    def _prepare_cipher(self, streamdetails: StreamDetails) -> tuple[Any, str, str]:
        """Prepare AES-256-CTR cipher and return (cipher, encrypted_url, codec).

        :param streamdetails: Stream details containing encrypted URL and key.
        :return: Tuple of (cipher, encrypted_url, codec).
        """
        encrypted_url: str = streamdetails.data["encrypted_url"]
        key_hex: str = streamdetails.data["decryption_key"]
        codec: str = streamdetails.data.get("codec", "flac")
        key_bytes = bytes.fromhex(key_hex)
        nonce = bytes(12)
        cipher = AES.new(key=key_bytes, nonce=nonce, mode=AES.MODE_CTR)
        return cipher, encrypted_url, codec

    async def get_audio_stream(
        self, streamdetails: StreamDetails, seek_position: int = 0
    ) -> AsyncGenerator[bytes, None]:
        """Return the audio stream for the provider item with decryption.

        Dispatches to the configured streaming mode: direct, buffered, or preload.

        :param streamdetails: Stream details containing encrypted URL and key.
        :param seek_position: Seek position (not supported for encrypted streams).
        :return: Async generator yielding decrypted audio bytes.
        """
        mode = self.provider.config.get_value(CONF_STREAMING_MODE) or STREAMING_MODE_BUFFERED

        if mode == STREAMING_MODE_DIRECT:
            gen = self._stream_direct(streamdetails)
        elif mode == STREAMING_MODE_PRELOAD:
            gen = self._stream_preload(streamdetails)
        else:
            gen = self._stream_buffered(streamdetails)

        async for chunk in gen:
            yield chunk

    async def _stream_direct(self, streamdetails: StreamDetails) -> AsyncGenerator[bytes, None]:
        """Stream and decrypt on-the-fly (original behavior).

        Download and decryption are coupled — each chunk is decrypted as it arrives.
        Best for fast networks and powerful CPUs.

        :param streamdetails: Stream details containing encrypted URL and key.
        """
        cipher, encrypted_url, codec = self._prepare_cipher(streamdetails)

        self.logger.info(
            "Starting direct streaming decryption for track %s (codec=%s)",
            streamdetails.item_id,
            codec,
        )

        chunk_size = 65536
        total_bytes = 0
        timeout = aiohttp.ClientTimeout(total=None, connect=30, sock_read=600)

        try:
            async with (
                aiohttp.ClientSession() as session,
                session.get(encrypted_url, timeout=timeout) as response,
            ):
                if response.status != 200:
                    msg = f"Failed to stream encrypted track: HTTP {response.status}"
                    self.logger.error(msg)
                    raise MediaNotFoundError(msg)

                self.logger.debug("Started streaming from %s", encrypted_url[:100])

                async for encrypted_chunk in response.content.iter_chunked(chunk_size):
                    decrypted_chunk = cipher.decrypt(encrypted_chunk)
                    total_bytes += len(decrypted_chunk)
                    yield decrypted_chunk

                self.logger.info(
                    "Completed direct streaming for track %s: %d bytes total",
                    streamdetails.item_id,
                    total_bytes,
                )

        except Exception as err:
            self.logger.exception(
                "Error during direct streaming for track %s: %s",
                streamdetails.item_id,
                err,
            )
            raise

    async def _stream_buffered(self, streamdetails: StreamDetails) -> AsyncGenerator[bytes, None]:
        """Download and decrypt via async queue, decoupling download from consumption.

        A background task downloads and decrypts chunks into a bounded queue.
        The consumer yields from the queue at its own pace. Backpressure is handled
        by the queue's maxsize — download blocks when the queue is full.

        :param streamdetails: Stream details containing encrypted URL and key.
        """
        cipher, encrypted_url, codec = self._prepare_cipher(streamdetails)

        self.logger.info(
            "Starting buffered streaming for track %s (codec=%s)",
            streamdetails.item_id,
            codec,
        )

        chunk_size = 65536
        queue_max = 32  # max 32 * 64KB = 2MB buffered ahead
        queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=queue_max)
        error_holder: list[BaseException | None] = [None]
        sentinel = None
        timeout = aiohttp.ClientTimeout(total=None, connect=30, sock_read=600)

        async def _download_and_decrypt() -> None:
            try:
                async with (
                    aiohttp.ClientSession() as session,
                    session.get(encrypted_url, timeout=timeout) as response,
                ):
                    if response.status != 200:
                        msg = f"Failed to stream encrypted track: HTTP {response.status}"
                        error_holder[0] = MediaNotFoundError(msg)
                        return

                    async for encrypted_chunk in response.content.iter_chunked(chunk_size):
                        decrypted = cipher.decrypt(encrypted_chunk)
                        await queue.put(decrypted)
            except Exception as exc:
                error_holder[0] = exc
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

    async def _stream_preload(self, streamdetails: StreamDetails) -> AsyncGenerator[bytes, None]:
        """Download the entire encrypted file first, then decrypt and yield.

        Uses SpooledTemporaryFile which keeps data in memory until the configured
        limit is exceeded, then transparently spills to disk.

        :param streamdetails: Stream details containing encrypted URL and key.
        """
        cipher, encrypted_url, codec = self._prepare_cipher(streamdetails)

        self.logger.info(
            "Starting preload streaming for track %s (codec=%s)",
            streamdetails.item_id,
            codec,
        )

        buffer_limit = int(
            self.provider.config.get_value(CONF_PRELOAD_BUFFER_MB) or 100  # type: ignore[arg-type]
        )
        max_bytes = buffer_limit * 1024 * 1024
        chunk_size = 65536
        timeout = aiohttp.ClientTimeout(total=None, connect=30, sock_read=600)

        with tempfile.SpooledTemporaryFile(max_size=max_bytes) as buf:
            try:
                async with (
                    aiohttp.ClientSession() as session,
                    session.get(encrypted_url, timeout=timeout) as response,
                ):
                    if response.status != 200:
                        msg = f"Failed to stream encrypted track: HTTP {response.status}"
                        self.logger.error(msg)
                        raise MediaNotFoundError(msg)

                    self.logger.debug("Preloading encrypted data from %s", encrypted_url[:100])
                    async for chunk in response.content.iter_chunked(chunk_size):
                        buf.write(chunk)

                download_size = buf.tell()
                self.logger.debug(
                    "Preloaded %d bytes for track %s, decrypting",
                    download_size,
                    streamdetails.item_id,
                )

                buf.seek(0)
                total_bytes = 0
                while True:
                    encrypted_chunk = buf.read(chunk_size)
                    if not encrypted_chunk:
                        break
                    decrypted = cipher.decrypt(encrypted_chunk)
                    total_bytes += len(decrypted)
                    yield decrypted

                self.logger.info(
                    "Completed preload streaming for track %s: %d bytes total",
                    streamdetails.item_id,
                    total_bytes,
                )

            except Exception as err:
                self.logger.exception(
                    "Error during preload streaming for track %s: %s",
                    streamdetails.item_id,
                    err,
                )
                raise
