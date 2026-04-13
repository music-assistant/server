"""Video service for nicovideo."""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import urljoin

from music_assistant_models.errors import InvalidDataError, UnplayableMediaError

from music_assistant.providers.nicovideo.constants import (
    DOMAND_BID_COOKIE_NAME,
    NICOVIDEO_USER_AGENT,
    SENSITIVE_CONTENTS,
)
from music_assistant.providers.nicovideo.converters.stream import (
    StreamConversionData,
)
from music_assistant.providers.nicovideo.services.base import NicovideoBaseService

if TYPE_CHECKING:
    from music_assistant_models.media_items import Track
    from music_assistant_models.streamdetails import StreamDetails
    from niconico.objects.video.watch import WatchData, WatchMediaDomandAudio

    from music_assistant.providers.nicovideo.services.manager import NicovideoServiceManager


class NicovideoVideoService(NicovideoBaseService):
    """Handles video and stream related operations for nicovideo."""

    def __init__(self, service_manager: NicovideoServiceManager) -> None:
        """Initialize NicovideoVideoService with reference to parent service manager."""
        super().__init__(service_manager)

    async def get_user_videos(
        self, user_id: str, page: int = 1, page_size: int = 50
    ) -> list[Track]:
        """Get user videos and convert as Track list."""
        user_video_data = await self.service_manager._call_with_throttler(
            self.niconico_py_client.user.get_user_videos,
            user_id,
            page=page,
            page_size=page_size,
            sensitive_contents=SENSITIVE_CONTENTS,
        )
        if not user_video_data or not user_video_data.items:
            return []
        tracks = []
        for item in user_video_data.items:
            track = self.converter_manager.track.convert_by_essential_video(item.essential)
            if track:
                tracks.append(track)
        return tracks

    async def get_video(self, video_id: str) -> Track | None:
        """Get video details using WatchData and convert as Track."""
        watch_data = await self.service_manager._call_with_throttler(
            self.niconico_py_client.video.watch.get_watch_data, video_id
        )

        if watch_data:
            return self.converter_manager.track.convert_by_watch_data(watch_data)

        return None

    async def get_stream_details(self, video_id: str) -> StreamDetails:
        """Get StreamDetails for a video using WatchData and converter."""
        conversion_data = await self._prepare_conversion_data(video_id)
        return self.converter_manager.stream.convert_from_conversion_data(conversion_data)

    async def _prepare_conversion_data(self, video_id: str) -> StreamConversionData:
        """Prepare StreamConversionData for a video."""
        # 1. Fetch watch data
        watch_data = await self.service_manager._call_with_throttler(
            self.niconico_py_client.video.watch.get_watch_data, video_id
        )
        if not watch_data:
            raise UnplayableMediaError("Failed to fetch watch data")

        # 2. Select best available audio
        selected_audio = self._select_best_audio(watch_data)

        # 3. Get HLS URL for selected audio
        hls_url = await self._get_hls_url(watch_data, selected_audio)

        # 4. Get domand_bid for ffmpeg headers
        domand_bid = self.niconico_py_client.session.cookies.get(DOMAND_BID_COOKIE_NAME)
        if not domand_bid:
            raise UnplayableMediaError("Failed to fetch domand_bid")

        # 5. Fetch HLS playlist text
        playlist_text = await self._fetch_media_playlist_text(hls_url, domand_bid)

        # 6. Return conversion data
        return StreamConversionData(
            watch_data=watch_data,
            selected_audio=selected_audio,
            hls_url=hls_url,
            domand_bid=domand_bid,
            hls_playlist_text=playlist_text,
        )

    def _select_best_audio(self, watch_data: WatchData) -> WatchMediaDomandAudio:
        """Select the best available audio from WatchData."""
        best_audio = None
        best_quality = -1
        for audio in watch_data.media.domand.audios:
            if audio.is_available and audio.quality_level > best_quality:
                best_audio = audio
                best_quality = audio.quality_level

        if not best_audio:
            raise UnplayableMediaError("No available audio found")

        return best_audio

    async def _get_hls_url(
        self, watch_data: WatchData, selected_audio: WatchMediaDomandAudio
    ) -> str:
        """Get HLS URL for selected audio."""
        # Create outputs list with selected audio ID only (audio-only)
        outputs = [selected_audio.id_]

        hls_url = await self.service_manager._call_with_throttler(
            self.niconico_py_client.video.watch.get_hls_content_url,
            watch_data,
            [outputs],  # list[list[str]] format
        )
        if not hls_url:
            raise UnplayableMediaError("Failed to get HLS content URL")

        return str(hls_url)

    async def _fetch_media_playlist_text(self, hls_url: str, domand_bid: str) -> str:
        """Fetch media playlist text from HLS stream.

        Args:
            hls_url: URL to the HLS playlist (master or media)
            domand_bid: Authentication cookie value

        Returns:
            Media playlist text (not parsed)
        """
        headers = {
            "User-Agent": NICOVIDEO_USER_AGENT,
            "Cookie": f"{DOMAND_BID_COOKIE_NAME}={domand_bid}",
        }
        session = self.service_manager.provider.mass.http_session

        # Fetch master playlist
        async with session.get(hls_url, headers=headers) as response:
            response.raise_for_status()
            master_playlist_text = await response.text()

        # Check if this is already a media playlist (has #EXTINF)
        if "#EXTINF:" in master_playlist_text:
            return master_playlist_text

        # Extract media playlist URL from master playlist
        media_playlist_url = self._extract_media_playlist_url(master_playlist_text, hls_url)

        # Fetch media playlist
        async with session.get(media_playlist_url, headers=headers) as response:
            response.raise_for_status()
            return await response.text()

    def _extract_media_playlist_url(self, master_playlist: str, base_url: str) -> str:
        """Extract media playlist URL from master playlist.

        Args:
            master_playlist: Master playlist text
            base_url: Base URL for resolving relative URLs

        Returns:
            Absolute URL to media playlist
        """
        lines = master_playlist.split("\n")
        for i, line in enumerate(lines):
            # Look for stream info line followed by URL
            if line.startswith("#EXT-X-STREAM-INF:"):
                if i + 1 < len(lines):
                    media_url = lines[i + 1].strip()
                    if media_url and not media_url.startswith("#"):
                        # Resolve relative URL if needed
                        return urljoin(base_url, media_url)
        msg = f"No media playlist URL found in master playlist from {base_url}"
        raise InvalidDataError(msg)
