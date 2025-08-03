"""User adapter for NicoNico."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Literal

from music_assistant_models.errors import MediaNotFoundError

from music_assistant.providers.niconico.adapters.base import NiconicoBaseAdapter
from music_assistant.providers.niconico.config import NiconicoConfig
from music_assistant.providers.niconico.parsers import (
    parse_artist,
    parse_track_by_essential_video,
)

if TYPE_CHECKING:
    from music_assistant_models.media_items import Artist, Track
    from niconico.objects.nvapi import FollowingMylistsData

    from music_assistant.providers.niconico.adapter import NicoNicoMusicAssistantAdapter

# Import at runtime for isinstance checks
from niconico.objects.video import EssentialVideo


class NicoNicoUserAdapter(NiconicoBaseAdapter):
    """Get user details from NicoNico."""

    def __init__(self, adapter: NicoNicoMusicAssistantAdapter) -> None:
        """Initialize NicoNicoUserAdapter with reference to parent adapter."""
        super().__init__(adapter)

    async def get_user(self, user_id: str) -> Artist | None:
        """Get user details as Artist."""
        user = await self.adapter.call_with_throttler(
            self.adapter.niconico_py_client.user.get_user, user_id
        )
        return parse_artist(self.adapter.provider, user) if user else None

    async def get_recommendations(
        self,
        recipe_id: Literal[
            "video_watch_recommendation", "video_recommendation_recommend", "video_top_recommend"
        ] = "video_watch_recommendation",
        limit: int = 25,
    ) -> list[Track]:
        """Get recommendations from NicoNico."""
        try:
            config = NiconicoConfig(self.adapter.provider)
            sensitive_contents = config.get_sensitive_contents_config()
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
                # Only process video content, skip user recommendations
                if item.content_type != "video":
                    continue

                # Type check to ensure content is EssentialVideo
                if isinstance(item.content, EssentialVideo):
                    track = parse_track_by_essential_video(self.adapter.provider, item.content)
                    if track:
                        tracks.append(track)
            return tracks
        except Exception as err:
            self.adapter.provider.logger.warning(
                "Failed to fetch recommendations for recipe %s: %s", recipe_id, err
            )
            return []

    async def get_similar_tracks(self, video_id: str, limit: int = 25) -> list[Track]:
        """Get similar tracks based on a given video ID."""
        try:
            config = NiconicoConfig(self.adapter.provider)
            sensitive_contents = config.get_sensitive_contents_config()
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
                # Only process video content, skip user recommendations
                if item.content_type != "video":
                    continue

                # Type check to ensure content is EssentialVideo
                if isinstance(item.content, EssentialVideo):
                    track = parse_track_by_essential_video(self.adapter.provider, item.content)
                    if track:
                        tracks.append(track)
            return tracks
        except Exception as err:
            self.adapter.provider.logger.warning(
                "Failed to fetch similar tracks for %s: %s", video_id, err
            )
            return []

    async def get_like_history(self, limit: int = 25) -> list[Track]:
        """Get user's like history from NicoNico."""
        # Calculate page_size based on limit
        page_size = min(limit, 25)  # API max is 25 for like history
        like_history = await self.adapter.call_with_throttler(
            self.adapter.niconico_py_client.video.get_like_history,
            page_size=page_size,
            page=1,
        )
        if not like_history or not like_history.items:
            return []

        tracks = []
        for item in like_history.items:
            track = parse_track_by_essential_video(self.adapter.provider, item.video)
            if track:
                tracks.append(track)
        return tracks

    async def get_user_history(self, limit: int = 30) -> list[Track]:
        """Get user's history from NicoNico."""
        # Calculate page_size based on limit
        page_size = min(limit, 100)  # API max is 100
        history = await self.adapter.call_with_throttler(
            self.adapter.niconico_py_client.video.get_history,
            page_size=page_size,
            page=1,
        )
        if not history or not history.items:
            return []

        tracks = []
        for item in history.items:
            track = parse_track_by_essential_video(self.adapter.provider, item.video)
            if track:
                tracks.append(track)
        return tracks

    async def get_following_activities(self, limit: int = 50) -> list[Track]:
        """Get latest activities from followed users."""
        try:
            feed_data = await self.adapter.call_with_throttler(
                self.adapter.niconico_py_client.user.get_following_activities,
                context="header_timeline",
                cursor=None,
            )

            if not feed_data or not hasattr(feed_data, "activities"):
                return []

            # Collect video IDs first
            video_ids = []
            for activity in feed_data.activities:
                if (
                    hasattr(activity, "content")
                    and activity.content
                    and hasattr(activity.content, "video")
                    and activity.content.video
                    and hasattr(activity, "kind")
                    and "video" in activity.kind.lower()
                ):
                    video_ids.append(activity.content.id_)
                    if len(video_ids) >= limit:
                        break

            # Process tracks with limited concurrency to avoid DB overload

            semaphore = asyncio.Semaphore(5)  # Limit to 5 concurrent requests

            async def get_track_with_limit(video_id: str) -> Track | None:
                async with semaphore:
                    try:
                        return await self.adapter.provider.mass.music.tracks.get_provider_item(
                            video_id, self.adapter.provider.instance_id
                        )
                    except MediaNotFoundError:
                        return None

            # Execute with limited concurrency
            track_tasks = [get_track_with_limit(video_id) for video_id in video_ids]
            tracks_results = await asyncio.gather(*track_tasks, return_exceptions=True)

            # Filter successful results
            tracks: list[Track] = []
            for result in tracks_results:
                if isinstance(result, Exception):
                    continue
                if result is not None and not isinstance(result, BaseException):
                    tracks.append(result)

            return tracks[:limit]

        except Exception as err:
            # Log the error but don't raise to avoid breaking other functionality
            self.adapter.provider.logger.warning("Error fetching following activities: %s", err)
            return []

    async def get_following_mylists(self) -> FollowingMylistsData | None:
        """Get mylists from users you follow."""
        try:
            following_mylists = await self.adapter.call_with_throttler(
                self.adapter.niconico_py_client.user.get_own_following_mylists,
            )
            return following_mylists if following_mylists else None
        except Exception as err:
            # Log the error but don't raise to avoid breaking other functionality
            self.adapter.provider.logger.warning("Error fetching following mylists: %s", err)
            return None

    async def get_own_followings(self) -> list[Artist]:
        """Get users you are following as Artists."""
        try:
            followings_data = await self.adapter.call_with_throttler(
                self.adapter.niconico_py_client.user.get_own_followings,
            )

            if not followings_data:
                return []

            # Extract users from followings data and convert to Artist objects
            artists: list[Artist] = []
            if hasattr(followings_data, "items"):
                for item in followings_data.items:
                    if hasattr(item, "user") and item.user:
                        artist = parse_artist(self.adapter.provider, item.user)
                        if artist:
                            artists.append(artist)

            return artists

        except Exception as err:
            # Log the error but don't raise to avoid breaking other functionality
            self.adapter.provider.logger.warning("Error fetching following users: %s", err)
            return []
