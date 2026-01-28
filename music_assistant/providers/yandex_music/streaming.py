"""Streaming operations for Yandex Music."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from music_assistant_models.enums import ContentType, StreamType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.streamdetails import StreamDetails

from .constants import QUALITY_LOSSLESS

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

    async def get_stream_details(self, item_id: str) -> StreamDetails:
        """Get stream details for a track.

        :param item_id: Track ID.
        :return: StreamDetails for the track.
        :raises MediaNotFoundError: If stream URL cannot be obtained.
        """
        # Get track info first
        track = await self.provider.get_track(item_id)
        if not track:
            raise MediaNotFoundError(f"Track {item_id} not found")

        # Get download info
        download_infos = await self.client.get_track_download_info(item_id, get_direct_links=True)
        if not download_infos:
            raise MediaNotFoundError(f"No stream info available for track {item_id}")

        # Select best quality based on config
        quality = self.provider.config.get_value("quality")
        quality_str = str(quality) if quality is not None else None
        selected_info = self._select_best_quality(download_infos, quality_str)

        if not selected_info or not selected_info.direct_link:
            raise MediaNotFoundError(f"No stream URL available for track {item_id}")

        # Determine content type
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
        :param preferred_quality: User's preferred quality setting.
        :return: Best matching DownloadInfo or None.
        """
        if not download_infos:
            return None

        # Sort by bitrate descending
        sorted_infos = sorted(
            download_infos,
            key=lambda x: x.bitrate_in_kbps or 0,
            reverse=True,
        )

        # If user wants lossless, try to find FLAC first
        if preferred_quality == QUALITY_LOSSLESS:
            for info in sorted_infos:
                if info.codec and info.codec.lower() == "flac":
                    return info

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
        if codec_lower == "flac":
            return ContentType.FLAC
        if codec_lower in ("mp3", "mpeg"):
            return ContentType.MP3
        if codec_lower == "aac":
            return ContentType.AAC

        return ContentType.UNKNOWN
