"""MixIn for NicovideoMusicProvider: search and recommendations methods."""

from __future__ import annotations

from typing import override

from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import RecommendationFolder, SearchResults, Track
from music_assistant_models.unique_list import UniqueList

from music_assistant.controllers.cache import use_cache
from music_assistant.providers.nicovideo.provider_mixins.base import (
    NicovideoMusicProviderMixinBase,
)


class NicovideoMusicProviderExplorerMixin(NicovideoMusicProviderMixinBase):
    """Search and recommendations methods for NicovideoMusicProvider."""

    @override
    @use_cache(3600 * 3)  # Cache for 3 hours
    async def search(
        self,
        search_query: str,
        media_types: list[MediaType],
        limit: int = 5,
    ) -> SearchResults:
        """Perform search on musicprovider.

        :param search_query: Search query.
        :param media_types: A list of media_types to include.
        :param limit: Number of items to return in the search (per type).
        """
        search_result = SearchResults()

        if MediaType.TRACK in media_types:
            tracks = await self.service_manager.search.search_videos_by_keyword(search_query, limit)
            search_result.tracks = tracks

        # Search for both playlists and albums in a single API call for efficiency
        list_media_types = [mt for mt in media_types if mt in (MediaType.PLAYLIST, MediaType.ALBUM)]

        if list_media_types:
            await self.service_manager.search.search_playlists_and_albums_by_keyword(
                search_query, limit, search_result, list_media_types
            )

        return search_result

    @override
    @use_cache(1800)  # Cache for 30 minutes
    async def recommendations(self) -> list[RecommendationFolder]:
        """
        Get this provider's recommendations.

        Returns an actual (and often personalised) list of recommendations
        from this provider for the user/account.
        """
        recommendation_folders = []

        # Main recommendations (default: 25 tracks)
        main_recommendation_tracks = await self.service_manager.user.get_recommendations(
            "video_recommendation_recommend", limit=25
        )
        if main_recommendation_tracks:
            recommendation_folders.append(
                RecommendationFolder(
                    item_id="nicovideo_recommendations",
                    name="nicovideo recommendations",
                    provider=self.instance_id,
                    icon="mdi-star-circle-outline",
                    items=UniqueList(main_recommendation_tracks),
                )
            )

        # History Tracks (default: 50 tracks)
        history_tracks = await self.service_manager.user.get_user_history(limit=50)
        if history_tracks:
            recommendation_folders.append(
                RecommendationFolder(
                    item_id="nicovideo_history",
                    name="Recently watched (nicovideo history)",
                    provider=self.instance_id,
                    icon="mdi-history",
                    items=UniqueList(history_tracks),
                )
            )

        # Following activities recommendations (default: 30 tracks)
        following_activities_tracks = await self.service_manager.user.get_following_activities(
            limit=30
        )
        if following_activities_tracks:
            recommendation_folders.append(
                RecommendationFolder(
                    item_id="nicovideo_following_activities",
                    name="New Tracks from Followed Users",
                    provider=self.instance_id,
                    icon="mdi-account-plus-outline",
                    items=UniqueList(following_activities_tracks),
                )
            )

        # Like History recommendations (default: 50 tracks)
        like_history_tracks = await self.service_manager.user.get_like_history(limit=50)
        if like_history_tracks:
            recommendation_folders.append(
                RecommendationFolder(
                    item_id="nicovideo_like_history",
                    name="Recently liked (Like history)",
                    provider=self.instance_id,
                    icon="mdi-heart-outline",
                    items=UniqueList(like_history_tracks),
                )
            )

        return recommendation_folders

    @override
    @use_cache(3600 * 6)  # Cache for 6 hours
    async def get_similar_tracks(self, prov_track_id: str, limit: int = 25) -> list[Track]:
        """Retrieve a dynamic list of similar tracks based on the provided track."""
        return await self.service_manager.user.get_similar_tracks(prov_track_id, limit)
