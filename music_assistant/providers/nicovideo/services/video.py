"""Video service for nicovideo."""

from __future__ import annotations

import logging
from io import StringIO
from typing import TYPE_CHECKING, Any, cast

import yt_dlp
from music_assistant_models.errors import UnplayableMediaError

from music_assistant.providers.nicovideo.constants import (
    NICOVIDEO_COOKIE_DOMAIN,
)
from music_assistant.providers.nicovideo.helpers import (
    convert_to_netscape,
)
from music_assistant.providers.nicovideo.services.base import NicovideoBaseService

if TYPE_CHECKING:
    from music_assistant_models.media_items import Track

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
        config = self.nicovideo_config
        sensitive_contents = config.get_sensitive_contents_config()
        user_video_data = await self.service_manager._call_with_throttler(
            self.service_manager.niconico_py_client.user.get_user_videos,
            user_id,
            page=page,
            page_size=page_size,
            sensitive_contents=sensitive_contents,
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
            self.service_manager.niconico_py_client.video.watch.get_watch_data, video_id
        )

        if watch_data:
            return self.converter_manager.track.convert_by_watch_data(watch_data)

        return None

    async def get_stream_format(self, item_id: str) -> dict[str, Any]:
        """Use yt-dlp to extract the best stream URL from nicovideo."""
        netscape_cookie_str = convert_to_netscape(
            self.service_manager.niconico_py_client.session.cookies, NICOVIDEO_COOKIE_DOMAIN
        )

        def _extract() -> dict[str, Any]:
            url = f"https://www.nicovideo.jp/watch/{item_id}"
            ydl_opts = {
                "quiet": self.logger.level > logging.DEBUG,
                "cookiefile": StringIO(netscape_cookie_str),
                "format": "bestaudio/best",
                "nocheckcertificate": True,
                "noplaylist": True,
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                try:
                    info = ydl.extract_info(url, download=False)
                    # Use yt-dlp's format selector like YouTube Music does
                    format_selector = ydl.build_format_selector("bestaudio")
                    if not (
                        stream_format := next(format_selector({"formats": info["formats"]}), None)
                    ):
                        raise UnplayableMediaError("No stream formats found")
                    # Return the format as-is like YouTube Music does
                    return cast("dict[str, Any]", stream_format)
                except yt_dlp.utils.DownloadError as err:
                    raise UnplayableMediaError(f"nicovideo extract error: {err}") from err

        result = await self.service_manager._call_with_throttler(_extract)
        if result is None:
            raise UnplayableMediaError("Failed to extract stream format")
        return result

    async def like_video(self, video_id: str) -> bool:
        """Like a video."""
        result = await self.service_manager._call_with_throttler(
            self.service_manager.niconico_py_client.video.like_video, video_id
        )
        return bool(result)
