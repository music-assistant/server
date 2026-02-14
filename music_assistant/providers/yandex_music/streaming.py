"""Streaming operations for Yandex Music."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from music_assistant_models.enums import ContentType, StreamType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.streamdetails import StreamDetails

from .constants import CONF_QUALITY, QUALITY_LOSSLESS, RADIO_TRACK_ID_SEP

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
        want_lossless = (
            QUALITY_LOSSLESS in preferred_normalized or preferred_normalized == QUALITY_LOSSLESS
        )

        # When user wants lossless, try get-file-info first (FLAC; download-info often MP3 only)
        if want_lossless:
            self.logger.debug("Requesting lossless via get-file-info for track %s", track_id)
            file_info = await self.client.get_track_file_info_lossless(track_id)
            if file_info:
                url = file_info.get("url")
                codec = file_info.get("codec") or ""
                if url and codec.lower() in ("flac", "flac-mp4"):
                    content_type = self._get_content_type(codec)
                    self.logger.debug(
                        "Stream selected for track %s via get-file-info: codec=%s",
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
        """Select the best quality download info.

        :param download_infos: List of DownloadInfo objects.
        :param preferred_quality: User's preferred quality (e.g. "lossless" or "Lossless (FLAC)").
        :return: Best matching DownloadInfo or None.
        """
        if not download_infos:
            return None

        # Normalize so we accept "lossless", "Lossless (FLAC)", etc.
        preferred_normalized = (preferred_quality or "").strip().lower()
        want_lossless = (
            QUALITY_LOSSLESS in preferred_normalized or preferred_normalized == QUALITY_LOSSLESS
        )

        # Sort by bitrate descending
        sorted_infos = sorted(
            download_infos,
            key=lambda x: x.bitrate_in_kbps or 0,
            reverse=True,
        )

        # If user wants lossless, prefer flac-mp4 then flac (API formats ~2025)
        if want_lossless:
            for codec in ("flac-mp4", "flac"):
                for info in sorted_infos:
                    if info.codec and info.codec.lower() == codec:
                        return info
            self.logger.warning(
                "Lossless (FLAC) requested but no FLAC in API response for this "
                "track; using best available"
            )

        # Return highest bitrate
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
