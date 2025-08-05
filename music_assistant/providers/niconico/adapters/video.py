"""Video adapter for NicoNico."""

from __future__ import annotations

from io import StringIO
from typing import TYPE_CHECKING, Any

import yt_dlp
from music_assistant_models.errors import UnplayableMediaError

from music_assistant.providers.niconico.adapters.base import NiconicoBaseAdapter
from music_assistant.providers.niconico.constants import (
    NICONICO_COOKIE_DOMAIN,
    ApiPriority,
)
from music_assistant.providers.niconico.converter import convert_track_by_essential_video
from music_assistant.providers.niconico.helpers import (
    convert_to_netscape,
)

if TYPE_CHECKING:
    from music_assistant_models.media_items import Track

    from music_assistant.providers.niconico.adapter import NicoNicoMusicAssistantAdapter


class NiconicoVideoAdapter(NiconicoBaseAdapter):
    """Handles video and stream related operations for NicoNico."""

    def __init__(self, adapter: NicoNicoMusicAssistantAdapter) -> None:
        """Initialize NiconicoVideoAdapter with reference to parent adapter."""
        super().__init__(adapter)

    async def get_user_videos(
        self, user_id: str, page: int = 1, page_size: int = 50
    ) -> list[Track]:
        """Get user videos and convert as Track list."""
        config = self.niconico_config
        sensitive_contents = config.get_sensitive_contents_config()
        user_video_data = await self.adapter._call_with_throttler(
            self.adapter.niconico_py_client.user.get_user_videos,
            user_id,
            page=page,
            page_size=page_size,
            sensitive_contents=sensitive_contents,
        )
        if not user_video_data or not user_video_data.items:
            return []
        tracks = []
        for item in user_video_data.items:
            track = convert_track_by_essential_video(self.adapter.provider, item.essential)
            if track:
                tracks.append(track)
        return tracks

    async def get_video(self, video_id: str) -> Track | None:
        """Get video details and convert as Track."""
        video = await self.adapter._call_with_throttler(
            self.adapter.niconico_py_client.video.get_video, video_id
        )
        return convert_track_by_essential_video(self.adapter.provider, video) if video else None

    async def get_video_tags(
        self, video_id: str, priority: ApiPriority = ApiPriority.HIGH
    ) -> list[str]:
        """Get video tags as list of strings with specified priority."""
        tags = await self.adapter._call_with_throttler_with_priority(
            priority, self.adapter.niconico_py_client.video.get_video_tags, video_id
        )
        if not tags:
            return []
        # Extract tag names from Tag objects
        return [tag.name for tag in tags if hasattr(tag, "name")]

    async def get_stream_format(self, item_id: str) -> dict[str, Any]:
        """Use yt-dlp to extract the best stream URL from Niconico."""
        netscape_cookie_str = convert_to_netscape(
            self.adapter.niconico_py_client.session.cookies, NICONICO_COOKIE_DOMAIN
        )

        def _extract() -> dict[str, Any]:
            url = f"https://www.nicovideo.jp/watch/{item_id}"
            ydl_opts = {
                "quiet": True,
                "format": "bestaudio/best",
                "nocheckcertificate": True,
                "noplaylist": True,
                "cookiefile": StringIO(netscape_cookie_str),
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                try:
                    info = ydl.extract_info(url, download=False)
                    best_format = next(
                        (f for f in info["formats"] if f.get("acodec") != "none"), None
                    )
                    if not best_format:
                        raise UnplayableMediaError("No suitable audio stream found")
                    return {
                        "url": best_format["url"],
                        "audio_ext": best_format["ext"],
                        "audio_channels": best_format.get("channels"),
                        "asr": best_format.get("asr"),
                        "cookies": best_format["cookies"],
                        "user_agent": best_format["http_headers"].get("User-Agent", "Mozilla/5.0"),
                        "duration": info.get("duration"),
                    }
                except Exception as err:
                    raise UnplayableMediaError(f"Niconico extract error: {err}") from err

        result = await self.adapter._call_with_throttler(_extract)
        if result is None:
            raise UnplayableMediaError("Failed to extract stream format")
        return result

    async def like_video(self, video_id: str) -> bool:
        """Like a video."""
        result = await self.adapter._call_with_throttler(
            self.adapter.niconico_py_client.video.like_video, video_id
        )
        return bool(result)
