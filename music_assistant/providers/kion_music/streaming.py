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
    CONF_CODECS,
    CONF_QUALITY,
    CONF_TRANSPORT,
    QUALITY_BALANCED,
    QUALITY_EFFICIENT,
    QUALITY_FILE_INFO_PARAMS,
    QUALITY_HIGH,
    QUALITY_LOSSLESS,
    RADIO_TRACK_ID_SEP,
    TRANSPORT_RAW,
)

if TYPE_CHECKING:
    from yandex_music import DownloadInfo

    from .provider import KionMusicProvider


# Windowed-stream tuning constants
_CHUNK_SIZE = 16384  # smaller than default 65536 for faster first-byte after retry
_STREAM_TIMEOUT = aiohttp.ClientTimeout(total=None, sock_read=30)
# Kion CDN drops TCP connections for slow consumers (observed at ~45s for raw transport
# at real-time playback rate ~200 KB/s). By capping each Range request to 4 MB we download
# each window quickly, preventing CDN drops for both raw and encrypted transports.
_RANGE_WINDOW = 4 * 1024 * 1024  # 4 MB per Range request
# AES-CTR block size in bytes (used for block-aligned Range requests in encrypted transport)
_AES_BLOCK_SIZE = 16
# Flat short delays for TCP drops (network glitches within a 4 MB window)
_TCP_DROP_DELAYS = (0.5, 1.0, 2.0)
# Exponential delays for true network stalls (read timeout)
_STALL_DELAYS = (2.0, 4.0, 8.0)


class KionMusicStreamingManager:
    """Manages KION Music streaming operations."""

    def __init__(self, provider: KionMusicProvider) -> None:
        """
        Initialize streaming manager.

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
        """
        Get stream details for a track.

        Uses the unified /get-file-info endpoint for all quality tiers.
        Falls back to /tracks/{id}/download-info if get-file-info fails.

        :param item_id: Track ID or composite track_id@station_id for My Mix.
        :return: StreamDetails for the track (item_id preserved for on_streamed).
        :raises MediaNotFoundError: If stream URL cannot be obtained.
        """
        track_id = self._track_id_from_item_id(item_id)
        track = await self.provider.get_track(item_id)
        if not track:
            raise MediaNotFoundError(f"Track {item_id} not found")

        quality = (
            str(self.provider.config.get_value(CONF_QUALITY) or QUALITY_BALANCED).strip().lower()
        )
        transport = (
            str(self.provider.config.get_value(CONF_TRANSPORT) or TRANSPORT_RAW).strip().lower()
        )

        # Backward compatibility: old "lossless" config value
        if quality == "lossless":
            quality = QUALITY_LOSSLESS

        fi_params = QUALITY_FILE_INFO_PARAMS.get(
            quality, QUALITY_FILE_INFO_PARAMS[QUALITY_BALANCED]
        )

        # Allow advanced users to override codecs
        codecs_override = str(self.provider.config.get_value(CONF_CODECS) or "").strip()
        codecs = codecs_override or fi_params["codecs"]

        self.logger.debug(
            "Requesting stream for track %s: quality=%s, transport=%s, codecs=%s",
            track_id,
            quality,
            transport,
            codecs,
        )

        file_info = await self.client.get_track_file_info(
            track_id,
            quality=fi_params["quality"],
            codecs=codecs,
            transport=transport,
        )

        if file_info and file_info.get("url"):
            url = file_info["url"]
            codec = file_info.get("codec") or ""
            needs_decryption = file_info.get("needs_decryption", False)

            # Gather audio params: API response first, then probe container
            bit_rate = file_info.get("bitrate") or file_info.get("bitrate_in_kbps") or 0
            sample_rate = file_info.get("sample_rate") or 0
            bit_depth = file_info.get("bit_depth") or 0

            if (not sample_rate or not bit_depth) and not needs_decryption:
                # Probe raw stream headers for real sample_rate/bit_depth
                probed_sr, probed_bd = await self._probe_stream_params(url, codec)
                sample_rate = sample_rate or probed_sr
                bit_depth = bit_depth or probed_bd

            self.logger.debug(
                "Audio params for track %s: codec=%s, bit_rate=%s, sample_rate=%s, bit_depth=%s",
                track_id,
                codec,
                bit_rate,
                sample_rate,
                bit_depth,
            )

            audio_format = self._build_audio_format(
                codec,
                bit_rate=bit_rate,
                sample_rate=sample_rate,
                bit_depth=bit_depth,
            )

            # Always use StreamType.CUSTOM with windowed Range requests to prevent CDN drops.
            # can_seek=True only for codecs where bitrate * time yields a decodable byte
            # offset — i.e. raw MP3. MP4-container codecs (aac-mp4, flac-mp4) need the
            # ftyp/moov init atoms at the file start, so byte-offset seeks land in mdat
            # with no codec config and produce undecodable data. Raw FLAC frames aren't
            # fixed-size either, so byte-rate math doesn't land on a frame boundary.
            # allow_seek=True lets ffmpeg handle time-based seeking via -ss in those cases.
            byte_seekable = codec.lower() in ("mp3", "mpeg")
            can_seek = not needs_decryption and bit_rate > 0 and byte_seekable
            data: dict[str, Any] = {
                "url": url,
                "codec": codec,
                "transport": transport,
                "bit_rate": bit_rate,
                # Stored for URL refresh on 4xx:
                "fi_quality": fi_params["quality"],
                "fi_codecs": codecs,
            }
            if needs_decryption and "key" in file_info:
                data["decryption_key"] = file_info["key"]

            return StreamDetails(
                item_id=item_id,
                provider=self.provider.instance_id,
                audio_format=audio_format,
                stream_type=StreamType.CUSTOM,
                duration=track.duration,
                data=data,
                can_seek=can_seek,
                allow_seek=not needs_decryption,
            )

        # Fallback: /tracks/{id}/download-info (defensive, should rarely trigger)
        self.logger.warning(
            "get-file-info failed for track %s, falling back to download-info", track_id
        )
        download_infos = await self.client.get_track_download_info(track_id, get_direct_links=True)
        if not download_infos:
            raise MediaNotFoundError(f"No stream info available for track {item_id}")

        selected_info = self._select_best_quality(download_infos, quality)
        if not selected_info or not selected_info.direct_link:
            raise MediaNotFoundError(f"No stream URL available for track {item_id}")

        self.logger.debug(
            "Fallback stream for track %s: codec=%s, bitrate=%s",
            track_id,
            getattr(selected_info, "codec", None),
            getattr(selected_info, "bitrate_in_kbps", None),
        )

        return StreamDetails(
            item_id=item_id,
            provider=self.provider.instance_id,
            audio_format=self._build_audio_format(
                selected_info.codec, bit_rate=selected_info.bitrate_in_kbps or 0
            ),
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
        """
        Select the best quality download info based on user preference.

        Used as fallback when get-file-info is unavailable.

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

        # Superb: Prefer FLAC (accept legacy "lossless" label for backward compatibility)
        if preferred_normalized in {QUALITY_LOSSLESS, "lossless"}:
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
            sorted_infos_asc = sorted(
                download_infos,
                key=lambda x: x.bitrate_in_kbps or 999,
            )
            for codec in ("aac-mp4", "aac", "he-aac-mp4", "he-aac", "mp3"):
                for info in sorted_infos_asc:
                    if info.codec and info.codec.lower() == codec:
                        return info
            return sorted_infos_asc[0]

        # High: Prefer high bitrate MP3 (~320kbps)
        if preferred_normalized == QUALITY_HIGH:
            high_quality_mp3 = [
                info
                for info in sorted_infos
                if info.codec
                and info.codec.lower() == "mp3"
                and info.bitrate_in_kbps
                and info.bitrate_in_kbps >= 256
            ]
            if high_quality_mp3:
                return high_quality_mp3[0]

            for info in sorted_infos:
                if info.codec and info.codec.lower() == "mp3":
                    return info

            for info in sorted_infos:
                if info.codec and info.codec.lower() not in ("flac", "flac-mp4"):
                    return info

            return sorted_infos[0]

        # Balanced (default): Prefer ~192kbps AAC
        balanced_infos = [
            info
            for info in sorted_infos
            if info.bitrate_in_kbps and 128 <= info.bitrate_in_kbps <= 256
        ]
        if balanced_infos:
            for codec in ("aac-mp4", "aac", "he-aac-mp4", "he-aac", "mp3"):
                for info in balanced_infos:
                    if info.codec and info.codec.lower() == codec:
                        return info
            return balanced_infos[0]

        return sorted_infos[0] if sorted_infos else None

    def _get_content_type(self, codec: str | None) -> tuple[ContentType, ContentType]:
        """
        Determine container and codec type from Kion API codec string.

        Kion API returns codec strings like "flac-mp4" (FLAC in MP4 container),
        "aac-mp4" (AAC in MP4 container), or plain "flac", "mp3", "aac".
        For MP4-container variants we return ContentType.MP4 as the container
        and the actual audio codec via codec_type, matching the convention used
        by the Yandex Music provider. This keeps container-aware behaviors in
        MA core (mime type, seek handling, passthrough) correct.

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

        # Plain single-codec formats: codec is implied by content_type
        if codec_lower == "flac":
            return ContentType.FLAC, ContentType.UNKNOWN
        if codec_lower in ("mp3", "mpeg"):
            return ContentType.MP3, ContentType.UNKNOWN
        if codec_lower in ("aac", "he-aac"):
            return ContentType.AAC, ContentType.UNKNOWN

        self.logger.debug("Unknown codec from Kion API: %s", codec)
        return ContentType.UNKNOWN, ContentType.UNKNOWN

    def _build_audio_format(
        self,
        codec: str | None,
        *,
        bit_rate: int = 0,
        sample_rate: int = 0,
        bit_depth: int = 0,
    ) -> AudioFormat:
        """
        Build AudioFormat from codec string and optional stream metadata.

        Values of 0 mean "unknown — let MA/ffmpeg detect from the actual stream".
        Pass real values from the API response when available.

        :param codec: Codec string from Kion API.
        :param bit_rate: Bitrate in kbps (0 = unknown).
        :param sample_rate: Sample rate in Hz (0 = unknown, detect from stream).
        :param bit_depth: Bit depth (0 = unknown, detect from stream).
        :return: Configured AudioFormat instance.
        """
        content_type, codec_type = self._get_content_type(codec)
        kwargs: dict[str, Any] = {
            "content_type": content_type,
            "codec_type": codec_type,
        }
        # Only pass non-zero values; AudioFormat defaults to 44100/16 which
        # MA/ffmpeg rely on.  Passing 0 would override those defaults.
        if bit_rate:
            kwargs["bit_rate"] = bit_rate
        if sample_rate:
            kwargs["sample_rate"] = sample_rate
        if bit_depth:
            kwargs["bit_depth"] = bit_depth
        return AudioFormat(**kwargs)

    @staticmethod
    def _parse_flac_streaminfo(header: bytes) -> tuple[int, int]:
        """
        Extract sample_rate and bit_depth from FLAC STREAMINFO block.

        FLAC format: 4-byte magic "fLaC", then metadata blocks.
        STREAMINFO is always the first block (type 0), 34 bytes payload.
        Bytes 10-17 of STREAMINFO contain sample_rate (20 bits),
        channels (3 bits), bit_depth (5 bits), total samples (36 bits).

        :param header: First 42+ bytes of the FLAC stream.
        :return: (sample_rate, bit_depth) or (0, 0) on parse failure.
        """
        if len(header) < 42 or header[:4] != b"fLaC":
            return 0, 0
        # STREAMINFO payload: 4 magic + 4 block header = 8 byte offset, 34 bytes long
        # Bytes 10-13 of payload: sample_rate(20) | channels(3) | bps(5) | total(4 high)
        payload = header[8:]  # skip "fLaC" + block header
        if len(payload) < 34:
            return 0, 0
        val = int.from_bytes(payload[10:14], "big")
        sample_rate = (val >> 12) & 0xFFFFF
        bit_depth = ((val >> 4) & 0x1F) + 1
        return sample_rate, bit_depth

    @staticmethod
    def _parse_mp4_audio_params(header: bytes) -> tuple[int, int]:
        """
        Extract sample_rate and bit_depth from MP4/fMP4 container.

        Scans for the 'dfLa' (FLAC-in-MP4) box, or falls back to parsing
        the AudioSampleEntry in an 'mp4a' box to read sample size and sample rate.

        :param header: First 8-32 KB of the MP4 stream.
        :return: (sample_rate, bit_depth) or (0, 0) if not found.
        """
        # Quick scan for dfLa box (FLAC-in-MP4: contains FLAC STREAMINFO)
        dfl_pos = header.find(b"dfLa")
        if dfl_pos >= 4:
            # dfLa box layout after "dfLa" type:
            #   4 bytes version/flags
            #   4 bytes STREAMINFO block header (type byte + 3-byte length)
            #   34 bytes STREAMINFO payload
            payload_start = dfl_pos + 4 + 4 + 4  # after type + version/flags + block header
            payload = header[payload_start:]
            if len(payload) >= 34:
                val = int.from_bytes(payload[10:14], "big")
                sample_rate = (val >> 12) & 0xFFFFF
                bit_depth = ((val >> 4) & 0x1F) + 1
                if 8000 <= sample_rate <= 384000 and 1 <= bit_depth <= 32:
                    return sample_rate, bit_depth

        # Scan for mp4a AudioSampleEntry (AAC/generic audio in MP4)
        mp4a_pos = header.find(b"mp4a")
        if mp4a_pos >= 4:
            # AudioSampleEntry: 4-byte size, "mp4a", 6 reserved, 2 data_ref,
            # 2 version, 2 revision, 4 vendor, 2 channels, 2 sample_size,
            # 2 compression_id, 2 packet_size, 4 sample_rate (16.16 fixed-point)
            entry_start = mp4a_pos + 4  # after "mp4a"
            entry = header[entry_start:]
            if len(entry) >= 28:
                sample_size = int.from_bytes(entry[18:20], "big")
                sr_fixed = int.from_bytes(entry[24:28], "big")
                sample_rate = sr_fixed >> 16
                bit_depth = max(0, sample_size)
                if 8000 <= sample_rate <= 384000:
                    return sample_rate, bit_depth

        return 0, 0

    async def _probe_stream_params(self, url: str, codec: str) -> tuple[int, int]:
        """
        Probe audio params by reading the first bytes of the stream.

        Makes a small Range request to read container/stream headers,
        then parses FLAC STREAMINFO or MP4 box structure.

        :param url: Stream URL.
        :param codec: Codec string from API (e.g. "flac-mp4", "flac").
        :return: (sample_rate, bit_depth) or (0, 0) if probing fails.
        """
        codec_lower = (codec or "").lower()
        # Determine how many bytes to read and which parser to use
        if codec_lower == "flac":
            probe_size = 64
            parser = self._parse_flac_streaminfo
        elif "-mp4" in codec_lower:
            probe_size = 32768  # MP4 moov/stsd can be further in
            parser = self._parse_mp4_audio_params
        else:
            return 0, 0  # lossy without container — let MA detect

        try:
            headers = {"Range": f"bytes=0-{probe_size - 1}"}
            async with self.mass.http_session.get(
                url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status not in (200, 206):
                    return 0, 0
                header_bytes = await resp.content.read(probe_size)
                self.logger.debug("Probe read %d bytes for codec=%s", len(header_bytes), codec)
                result = parser(header_bytes)
                self.logger.debug("Probe result: sample_rate=%d, bit_depth=%d", *result)
                return result
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger.debug("Stream probe failed for codec=%s", codec)
            return 0, 0

    async def _refresh_stream_url(
        self,
        streamdetails: StreamDetails,
        http_status: int,
        bytes_yielded: int,
        attempt: int,
        max_retries: int,
    ) -> bool:
        """
        Re-fetch an expired stream URL (works for both raw and encraw).

        Updates streamdetails.data in-place with new URL (and key for encraw).

        :return: True on success, False if retries exhausted.
        """
        if attempt >= max_retries:
            return False
        data = streamdetails.data
        track_id = self._track_id_from_item_id(streamdetails.item_id)
        self.logger.warning(
            "Stream URL expired (HTTP %d) at %d bytes (attempt %d/%d) — re-fetching",
            http_status,
            bytes_yielded,
            attempt + 1,
            max_retries,
        )
        token = BYPASS_THROTTLER.set(True)
        try:
            file_info = await self.client.get_track_file_info(
                track_id,
                quality=data["fi_quality"],
                codecs=data["fi_codecs"],
                transport=data.get("transport", TRANSPORT_RAW),
            )
        finally:
            BYPASS_THROTTLER.reset(token)
        if file_info and file_info.get("url"):
            data["url"] = file_info["url"]
            if "decryption_key" in data and file_info.get("key"):
                data["decryption_key"] = file_info["key"]
            return True
        return False

    async def _decrypt_response_stream(
        self,
        response: Any,
        key_bytes: bytes,
        block_size: int,
        bytes_delivered: int,
    ) -> AsyncGenerator[bytes]:
        """
        Decrypt one HTTP response and yield plaintext chunks.

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
        """
        Increment retry counter, log a warning, or raise if retries are exhausted.

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
                "Stream %s at %d bytes (attempt %d/%d) — retrying",
                label,
                bytes_yielded,
                attempt,
                max_retries,
            )
            return attempt, delay
        raise MediaNotFoundError(f"Stream {label} after retries were exhausted") from err

    @staticmethod
    def _is_content_range_eof(headers: Any, window_end: int) -> bool:
        """
        Return True when Content-Range indicates *window_end* reached the last file byte.

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

    async def _iter_raw_response(
        self,
        response: Any,
        bytes_delivered: int,
        block_start: int,
    ) -> AsyncGenerator[bytes]:
        """
        Yield raw (unencrypted) chunks from one HTTP response.

        If the server ignored the Range header (200 instead of 206), skips the
        already-delivered prefix transparently.

        :param response: aiohttp ClientResponse (open context manager).
        :param bytes_delivered: Total bytes already sent to the caller.
        :param block_start: Requested Range start offset.
        :return: Async generator yielding raw audio bytes.
        """
        range_ignored = response.status == 200 and block_start > 0
        skip_bytes = bytes_delivered if range_ignored else 0
        async for raw_chunk in response.content.iter_chunked(_CHUNK_SIZE):
            if skip_bytes > 0:
                if len(raw_chunk) <= skip_bytes:
                    skip_bytes -= len(raw_chunk)
                    continue
                raw_chunk = raw_chunk[skip_bytes:]  # noqa: PLW2901
                skip_bytes = 0
            if raw_chunk:
                yield raw_chunk

    async def _handle_expired_url(
        self,
        streamdetails: StreamDetails,
        response_status: int,
        bytes_yielded: int,
        attempt: int,
        max_retries: int,
    ) -> bytes:
        """
        Handle URL expiry (401/403/410) by refreshing and returning updated key.

        On success returns the refreshed AES key bytes for encrypted streams,
        or empty bytes for raw transport (caller ignores the return in that
        case). Retry exhaustion raises ``MediaNotFoundError`` instead of
        returning ``None``, so the function is guaranteed to produce a
        ``bytes`` value when it returns.

        :return: Updated AES key bytes (or empty bytes for raw transport).
        :raises MediaNotFoundError: When refresh fails after retries exhausted.
        """
        if not await self._refresh_stream_url(
            streamdetails,
            response_status,
            bytes_yielded,
            attempt,
            max_retries,
        ):
            raise MediaNotFoundError(
                f"Stream URL expired (HTTP {response_status}) after retries exhausted"
            )
        data = streamdetails.data
        if "decryption_key" in data:
            try:
                return bytes.fromhex(data["decryption_key"])
            except ValueError as err:
                raise MediaNotFoundError(f"Invalid decryption key: {err}") from err
        return b""

    @staticmethod
    def _validate_encryption_key(data: dict[str, Any]) -> tuple[bool, bytes | None]:
        """
        Validate and extract encryption parameters from stream data.

        :return: (is_encrypted, key_bytes) tuple.
        :raises MediaNotFoundError: If AES key length is invalid.
        """
        if "decryption_key" not in data:
            return False, None
        try:
            key_bytes = bytes.fromhex(data["decryption_key"])
        except ValueError as err:
            raise MediaNotFoundError(f"Invalid decryption key: {err}") from err
        if len(key_bytes) not in (16, 24, 32):
            raise MediaNotFoundError(f"Unsupported AES key length: {len(key_bytes)} bytes")
        return True, key_bytes

    def _calculate_seek_offset(
        self, data: dict[str, Any], seek_position: int, is_encrypted: bool
    ) -> int:
        """
        Calculate initial byte offset for raw transport seeking.

        Byte-offset seeking is only safe for codecs where byte position and
        time position are linearly related — i.e. raw MP3. MP4-container
        formats (aac-mp4, flac-mp4) need the ftyp/moov init atoms at the
        file start and raw FLAC frames are variable-size, so byte-offset
        seeks there land in the middle of undecodable data. For those, we
        return 0 and let ffmpeg handle time-based seeking via ``-ss``
        (``allow_seek=True`` is set on the StreamDetails for that purpose).

        :param data: Stream data dict (must contain 'bit_rate' in kbps).
        :param seek_position: Seek offset in seconds.
        :param is_encrypted: Whether the stream uses AES encryption.
        :return: Byte offset to start streaming from (0 if not applicable).
        """
        if seek_position <= 0 or is_encrypted:
            return 0
        codec = str(data.get("codec") or "").lower()
        if codec not in ("mp3", "mpeg"):
            return 0
        bit_rate = data.get("bit_rate") or 0
        if not bit_rate:
            return 0
        byte_offset = seek_position * bit_rate * 1000 // 8
        self.logger.debug(
            "Seeking to %ds: byte offset %d (bitrate %d kbps)",
            seek_position,
            byte_offset,
            bit_rate,
        )
        return byte_offset

    async def get_audio_stream(  # noqa: PLR0915
        self, streamdetails: StreamDetails, seek_position: int = 0
    ) -> AsyncGenerator[bytes]:
        """
        Return the audio stream via windowed Range requests.

        Handles both raw (direct) and encraw (AES-CTR encrypted) transports.
        Downloads in windowed Range requests of _RANGE_WINDOW bytes each to prevent
        Kion CDN from dropping slow-consumer TCP connections.

        On connection drop: flat short backoff (0.5s/1.0s/2.0s).
        On read stall: exponential backoff (2s/4s/8s).
        On URL expiry (HTTP 4xx): re-fetches URL and resumes from bytes_yielded.
        Retry counter resets after each successful window.

        :param streamdetails: Stream details with URL (and optional decryption key).
        :param seek_position: Seek offset in seconds for raw transport (0 = from start).
        :return: Async generator yielding audio bytes.
        """
        data = streamdetails.data
        is_encrypted, key_bytes = self._validate_encryption_key(data)
        initial_byte_offset = self._calculate_seek_offset(data, seek_position, is_encrypted)

        max_retries = 6
        bytes_yielded = initial_byte_offset
        attempt = 0
        retry_delay: float = 0.0

        while True:
            if attempt > 0:
                await asyncio.sleep(retry_delay)

            block_start = (
                (bytes_yielded // _AES_BLOCK_SIZE) * _AES_BLOCK_SIZE
                if is_encrypted
                else bytes_yielded
            )
            window_end = block_start + _RANGE_WINDOW - 1

            try:
                async with self.mass.http_session.get(
                    data["url"],
                    headers={"Range": f"bytes={block_start}-{window_end}"},
                    timeout=_STREAM_TIMEOUT,
                ) as response:
                    if response.status in (401, 403, 410):
                        new_key = await self._handle_expired_url(
                            streamdetails,
                            response.status,
                            bytes_yielded,
                            attempt,
                            max_retries,
                        )
                        if is_encrypted:
                            key_bytes = new_key
                        attempt += 1
                        retry_delay = 0.0
                        continue
                    if response.status == 416:
                        # Range Not Satisfiable — last complete window aligned with EOF,
                        # so our next request asked past the end. Treat as EOF.
                        return
                    try:
                        response.raise_for_status()
                    except Exception as err:
                        raise MediaNotFoundError(f"Failed to fetch stream: {err}") from err

                    bytes_before = bytes_yielded
                    if is_encrypted:
                        if key_bytes is None:
                            raise MediaNotFoundError("Missing decryption key")
                        block_skip = bytes_before - block_start
                        async for chunk in self._decrypt_response_stream(
                            response,
                            key_bytes,
                            _AES_BLOCK_SIZE,
                            bytes_yielded,
                        ):
                            bytes_yielded += len(chunk)
                            yield chunk
                    else:
                        range_ignored = response.status == 200 and block_start > 0
                        block_skip = bytes_before if range_ignored else 0
                        async for chunk in self._iter_raw_response(
                            response,
                            bytes_before,
                            block_start,
                        ):
                            bytes_yielded += len(chunk)
                            yield chunk

                    received = (bytes_yielded - bytes_before) + block_skip
                    if response.status == 200 or received < _RANGE_WINDOW:
                        return
                    if self._is_content_range_eof(response.headers, window_end):
                        return
                    attempt = 0
                    retry_delay = 0.0

            except asyncio.CancelledError:
                raise
            except (ClientPayloadError, ServerDisconnectedError) as err:
                attempt, retry_delay = self._handle_stream_error(
                    err,
                    attempt,
                    max_retries,
                    bytes_yielded,
                    _TCP_DROP_DELAYS,
                    "dropped",
                )
            except TimeoutError as err:
                attempt, retry_delay = self._handle_stream_error(
                    err,
                    attempt,
                    max_retries,
                    bytes_yielded,
                    _STALL_DELAYS,
                    "stalled",
                )
