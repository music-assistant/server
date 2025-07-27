"""User adapter for NicoNico."""

from typing import TYPE_CHECKING

from music_assistant_models.media_items import Artist, Track

from music_assistant.providers.niconico.adapters.base import NiconicoBaseAdapter
from music_assistant.providers.niconico.constants import CONF_SENSITIVE_CONTENTS
from music_assistant.providers.niconico.parsers import (
    parse_artist,
    parse_track_by_essential_video,
)

if TYPE_CHECKING:
    from music_assistant.providers.niconico.adapter import NicoNicoMusicAssistantAdapter


class NicoNicoUserAdapter(NiconicoBaseAdapter):
    """Get user details from NicoNico."""

    def __init__(self, adapter: "NicoNicoMusicAssistantAdapter") -> None:
        """Initialize NicoNicoUserAdapter with reference to parent adapter."""
        super().__init__(adapter)

    async def get_user(self, user_id: str) -> Artist | None:
        """Get user details as Artist."""
        user = await self.adapter.call_with_throttler(
            self.adapter.niconico_py_client.user.get_user, user_id
        )
        return parse_artist(self.adapter.provider, user) if user else None

    async def get_recommendations(
        self, recipe_id: str = "video_top_recommend", limit: int = 25
    ) -> list[Track]:
        """Get recommendations from NicoNico."""
        sensitive_contents = self.adapter.provider.config.get_value(CONF_SENSITIVE_CONTENTS) or None
        recommendations = await self.adapter.call_with_throttler(
            self.adapter.niconico_py_client.user.get_recommendations,
            recipe_id,
            limit=limit,
            sensitive_contents=sensitive_contents,
        )
        if not recommendations or not recommendations.items:
            return []

        tracks = []
        for item in recommendations.items:
            track = parse_track_by_essential_video(self.adapter.provider, item.content)
            if track:
                tracks.append(track)
        return tracks

    async def get_similar_tracks(self, video_id: str, limit: int = 25) -> list[Track]:
        """Get similar tracks based on a given video ID."""
        sensitive_contents = self.adapter.provider.config.get_value(CONF_SENSITIVE_CONTENTS) or None
        recommendations = await self.adapter.call_with_throttler(
            self.adapter.niconico_py_client.user.get_recommendations,
            "video_watch_recommendation",
            video_id=video_id,
            limit=limit,
            sensitive_contents=sensitive_contents,
        )
        if not recommendations or not recommendations.items:
            return []

        tracks = []
        for item in recommendations.items:
            track = parse_track_by_essential_video(self.adapter.provider, item.content)
            if track:
                tracks.append(track)
        return tracks
