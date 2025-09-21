"""Stream handling for the Internet Archive provider."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from music_assistant_models.enums import ContentType, MediaType, StreamType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import AudioFormat
from music_assistant_models.streamdetails import StreamDetails

from .helpers import parse_duration

if TYPE_CHECKING:
    from .helpers import InternetArchiveClient


class InternetArchiveStreaming:
    """Handles stream details and multi-file streaming for Internet Archive."""

    def __init__(self, client: InternetArchiveClient, instance_id: str) -> None:
        """
        Initialize the streaming handler.

        Args:
            client: Internet Archive API client
            instance_id: Provider instance identifier
        """
        self.client = client
        self.instance_id = instance_id

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """
        Get streamdetails for a track or audiobook.

        For multi-file audiobooks, returns a MULTI_FILE stream with all chapter URLs.
        For single files or tracks, returns a standard HTTP stream.

        Args:
            item_id: Provider-specific item identifier
            media_type: The type of media being requested

        Returns:
            StreamDetails object configured for the specific item type

        Raises:
            MediaNotFoundError: If no audio files are found for the item
        """
        if "#" in item_id:
            # This is a track from an album or chapter from audiobook
            return self._get_album_track_stream(item_id, media_type)
        else:
            # This is a single item, find the audio files
            audio_files = await self.client.get_audio_files(item_id)
            if not audio_files:
                raise MediaNotFoundError(f"No audio files found for {item_id}")

            # For audiobooks with multiple files, use MULTI_FILE stream type
            if media_type == MediaType.AUDIOBOOK and len(audio_files) > 1:
                return await self._get_multi_file_audiobook_stream(item_id, audio_files)
            else:
                # Single file - use regular HTTP stream
                return self._get_single_file_stream(item_id, audio_files[0], media_type)

    def _get_album_track_stream(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Get stream details for an album track (item_id contains #)."""
        parent_id, filename = item_id.split("#", 1)
        download_url = self.client.get_download_url(parent_id, filename)

        return StreamDetails(
            provider=self.instance_id,
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

    async def _get_multi_file_audiobook_stream(
        self, item_id: str, audio_files: list[dict[str, Any]]
    ) -> StreamDetails:
        """Get stream details for a multi-file audiobook."""
        # Create list of download URLs for all chapters
        chapter_urls = []
        total_duration = 0

        for file_info in audio_files:
            filename = file_info["name"]
            download_url = self.client.get_download_url(item_id, filename)
            chapter_urls.append(download_url)

            # Add duration if available
            if duration_str := file_info.get("length"):
                if duration := parse_duration(duration_str):
                    total_duration += duration

        return StreamDetails(
            provider=self.instance_id,
            item_id=item_id,
            audio_format=AudioFormat(content_type=ContentType.UNKNOWN),
            media_type=MediaType.AUDIOBOOK,
            stream_type=StreamType.CUSTOM,
            duration=total_duration if total_duration > 0 else None,
            data={"chapters": chapter_urls, "chapters_data": audio_files},
            allow_seek=True,
            can_seek=True,
        )

    def _get_single_file_stream(
        self, item_id: str, file_info: dict[str, Any], media_type: MediaType
    ) -> StreamDetails:
        """Get stream details for a single file."""
        filename = file_info["name"]
        download_url = self.client.get_download_url(item_id, filename)

        return StreamDetails(
            provider=self.instance_id,
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
