"""Stream handling for the Internet Archive provider."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from music_assistant_models.enums import ContentType, MediaType, StreamType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.streamdetails import StreamDetails

if TYPE_CHECKING:
    from .provider import InternetArchiveProvider


class InternetArchiveStreaming:
    """Handles stream details and multi-file streaming for Internet Archive."""

    def __init__(self, provider: InternetArchiveProvider) -> None:
        """
        Initialize the streaming handler.

        Args:
            provider: The Internet Archive provider instance
        """
        self.provider = provider

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Get streamdetails for a track or audiobook."""
        if "#" in item_id:
            return self._get_single_file_stream(item_id, {}, media_type)
        else:
            audio_files = await self.provider.client.get_audio_files(item_id)
            if not audio_files:
                raise MediaNotFoundError(f"No audio files found for {item_id}")

            if media_type == MediaType.AUDIOBOOK and len(audio_files) > 1:
                return await self._get_multi_file_audiobook_stream(item_id, audio_files)
            else:
                return self._get_single_file_stream(item_id, audio_files[0], media_type)

    async def _get_multi_file_audiobook_stream(
        self, item_id: str, audio_files: list[dict[str, Any]]
    ) -> StreamDetails:
        """Get stream details for a multi-file audiobook."""
        # Create list of download URLs for all chapters
        chapter_urls = []

        # Use provider's helper method for consistent duration calculation
        total_duration, _ = await self.provider._calculate_audiobook_duration_and_chapters(item_id)

        for file_info in audio_files:
            filename = file_info["name"]
            download_url = self.provider.client.get_download_url(item_id, filename)
            chapter_urls.append(download_url)

        duration_to_set = total_duration if total_duration > 0 else None

        return StreamDetails(
            provider=self.provider.instance_id,
            item_id=item_id,
            audio_format=AudioFormat(content_type=ContentType.UNKNOWN),
            media_type=MediaType.AUDIOBOOK,
            stream_type=StreamType.CUSTOM,
            duration=duration_to_set,
            data={"chapters": chapter_urls, "chapters_data": audio_files},
            allow_seek=True,
            can_seek=True,
        )

    def _get_single_file_stream(
        self, item_id: str, file_info: dict[str, Any], media_type: MediaType
    ) -> StreamDetails:
        """Get stream details for a single file."""
        if "#" in item_id:
            # This is a track from an album - extract parent_id and filename
            parent_id, filename = item_id.split("#", 1)
            download_url = self.provider.client.get_download_url(parent_id, filename)
        else:
            # This is a single item
            filename = file_info["name"]
            download_url = self.provider.client.get_download_url(item_id, filename)

        return StreamDetails(
            provider=self.provider.instance_id,
            item_id=item_id,
            audio_format=AudioFormat(
                content_type=ContentType.UNKNOWN,  # Let ffmpeg detect format
            ),
            media_type=media_type,
            stream_type=StreamType.HTTP,
            path=download_url,
            allow_seek=True,
            can_seek=True,
        )
