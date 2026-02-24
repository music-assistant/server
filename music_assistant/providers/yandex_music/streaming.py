"""Streaming operations for Yandex Music."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any

from aiohttp import ClientPayloadError
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from music_assistant_models.enums import ContentType, StreamType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.streamdetails import StreamDetails

from .constants import (
    CONF_QUALITY,
    QUALITY_EFFICIENT,
    QUALITY_HIGH,
    QUALITY_SUPERB,
    RADIO_TRACK_ID_SEP,
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

    async def get_audio_stream(
        self, streamdetails: StreamDetails, seek_position: int = 0
    ) -> AsyncGenerator[bytes, None]:
        """Return the audio stream for the provider item with on-the-fly decryption.

        Downloads and decrypts the encrypted stream chunk-by-chunk without buffering.
        On connection drop, reconnects using a Range header and resumes AES-CTR
        decryption from the correct block boundary (up to 3 retries).

        :param streamdetails: Stream details containing encrypted URL and key.
        :param seek_position: Always 0 (seeking delegated to ffmpeg via allow_seek=True).
        :return: Async generator yielding decrypted audio bytes.
        """
        encrypted_url: str = streamdetails.data["encrypted_url"]
        key_hex: str = streamdetails.data["decryption_key"]
        key_bytes = bytes.fromhex(key_hex)
        if len(key_bytes) not in (16, 24, 32):
            raise MediaNotFoundError(f"Unsupported AES key length: {len(key_bytes)} bytes")

        block_size = 16  # AES-CTR block size in bytes
        max_retries = 3
        bytes_yielded = 0  # total decrypted bytes delivered to caller

        for attempt in range(max_retries + 1):
            if attempt > 0:
                await asyncio.sleep(min(2**attempt, 8))  # 2s, 4s, 8s

            # Align resume position to AES-CTR block boundary
            block_start = (bytes_yielded // block_size) * block_size
            block_skip = bytes_yielded - block_start  # overlap bytes to discard in first chunk

            # AES-CTR: original nonce is 0x00..00, so counter = block number
            nonce = (block_start // block_size).to_bytes(block_size, "big")
            decryptor = Cipher(algorithms.AES(key_bytes), modes.CTR(nonce)).decryptor()
            headers = {"Range": f"bytes={block_start}-"} if block_start > 0 else {}

            try:
                async with self.mass.http_session.get(encrypted_url, headers=headers) as response:
                    try:
                        response.raise_for_status()
                    except Exception as err:
                        raise MediaNotFoundError(
                            f"Failed to fetch encrypted stream: {err}"
                        ) from err

                    carry_skip = block_skip
                    async for chunk in response.content.iter_chunked(65536):
                        decrypted = decryptor.update(chunk)
                        if carry_skip > 0:
                            skip = min(carry_skip, len(decrypted))
                            decrypted = decrypted[skip:]
                            carry_skip -= skip
                        if decrypted:
                            bytes_yielded += len(decrypted)
                            yield decrypted

                    final = decryptor.finalize()
                    if final:
                        bytes_yielded += len(final)
                        yield final
                    return  # stream completed normally

            except asyncio.CancelledError:
                raise  # propagate cancellation immediately, do not retry
            except ClientPayloadError as err:
                if attempt < max_retries:
                    self.logger.warning(
                        "Encrypted stream dropped at %d bytes (attempt %d/%d): %s — retrying",
                        bytes_yielded,
                        attempt + 1,
                        max_retries,
                        err,
                    )
                else:
                    raise MediaNotFoundError(
                        "Encrypted stream ended early after retries were exhausted"
                    ) from err
