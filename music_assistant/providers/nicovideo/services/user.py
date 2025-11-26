"""User adapter for nicovideo."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant.providers.nicovideo.constants import SENSITIVE_CONTENTS
from music_assistant.providers.nicovideo.services.base import NicovideoBaseService

if TYPE_CHECKING:
    from typing import Literal

    from music_assistant_models.media_items import Artist, Track

    from music_assistant.providers.nicovideo.services.manager import NicovideoServiceManager

# Import at runtime for isinstance checks
from niconico.objects.video import EssentialVideo


class NicovideoUserService(NicovideoBaseService):
    """Get user details from nicovideo."""

    def __init__(self, service_manager: NicovideoServiceManager) -> None:
        """Initialize NicovideoUserService with reference to parent service manager."""
        super().__init__(service_manager)

    async def get_user(self, user_id: str) -> Artist | None:
        """Get user details as Artist."""
        user = await self.service_manager._call_with_throttler(
            self.niconico_py_client.user.get_user, user_id
        )
        return self.converter_manager.artist.convert_by_owner_or_user(user) if user else None

    async def get_recommendations(
        self,
        recipe_id: Literal[
            "video_watch_recommendation", "video_recommendation_recommend", "video_top_recommend"
        ] = "video_watch_recommendation",
        limit: int = 25,
    ) -> list[Track]:
        """Get recommendations from nicovideo."""
        recommendations = await self.service_manager._call_with_throttler(
            self.niconico_py_client.user.get_recommendations,
            recipe_id,
            limit=limit,
            sensitive_contents=SENSITIVE_CONTENTS,
        )
        if not recommendations or not recommendations.items:
            return []

        tracks = []
        for item in recommendations.items:
            # Only process video content, skip user recommendations
            if item.content_type != "video":
                continue

            # Type check to ensure content is EssentialVideo
            if isinstance(item.content, EssentialVideo):
                track = self.converter_manager.track.convert_by_essential_video(item.content)
                if track:
                    tracks.append(track)
        return tracks

    async def get_similar_tracks(self, track_id: str, limit: int = 25) -> list[Track]:
        """Get tracks similar to the given track."""
        recommendation_api_item = await self.service_manager._call_with_throttler(
            self.niconico_py_client.user.get_recommendations,
            "video_watch_recommendation",
            video_id=track_id,
            limit=limit,
            sensitive_contents=SENSITIVE_CONTENTS,
        )
        if not recommendation_api_item or not recommendation_api_item.items:
            return []

        tracks = []
        for item in recommendation_api_item.items:
            # Only process video content
            if item.content_type != "video":
                continue

            # Type check to ensure content is EssentialVideo
            if isinstance(item.content, EssentialVideo):
                track = self.converter_manager.track.convert_by_essential_video(item.content)
                if track:
                    tracks.append(track)
        return tracks

    async def get_like_history(self, limit: int = 25) -> list[Track]:
        """Get user's like history from nicovideo."""
        # Calculate page_size based on limit
        page_size = min(limit, 25)  # API max is 25 for like history
        like_history = await self.service_manager._call_with_throttler(
            self.niconico_py_client.video.get_like_history,
            page_size=page_size,
            page=1,
        )
        if not like_history or not like_history.items:
            return []

        tracks = []
        for item in like_history.items:
            track = self.converter_manager.track.convert_by_essential_video(item.video)
            if track:
                tracks.append(track)
        return tracks

    async def get_user_history(self, limit: int = 30) -> list[Track]:
        """Get user's history from nicovideo."""
        # Calculate page_size based on limit
        page_size = min(limit, 100)  # API max is 100
        history = await self.service_manager._call_with_throttler(
            self.niconico_py_client.video.get_history,
            page_size=page_size,
            page=1,
        )
        if not history or not history.items:
            return []

        tracks = []
        for item in history.items:
            track = self.converter_manager.track.convert_by_essential_video(item.video)
            if track:
                tracks.append(track)
        return tracks

    async def get_following_activities(self, limit: int = 50) -> list[Track]:
        """Get latest activities from followed users."""
        feed_data = await self.service_manager._call_with_throttler(
            self.niconico_py_client.user.get_following_activities,
            endpoint="video",
            context="header_timeline",
            cursor=None,
        )

        if not feed_data:
            return []

        # Convert activities directly to tracks using lightweight conversion
        tracks = []
        for activity in feed_data.activities:
            if activity.content and activity.content.video and "video" in activity.kind.lower():
                track = self.converter_manager.track.convert_by_activity(activity)
                if track:
                    tracks.append(track)
                if len(tracks) >= limit:
                    break

        return tracks

    async def get_own_followings(self) -> list[Artist]:
        """Get users the current user is following and convert them to Artists."""
        followings_data = await self.service_manager._call_with_throttler(
            self.niconico_py_client.user.get_own_followings,
            page_size=25,
            page=1,
        )

        if not followings_data or not followings_data.items:
            return []

        artists = []
        for user in followings_data.items:
            artist = self.converter_manager.artist.convert_by_owner_or_user(user)
            artists.append(artist)
        return artists
