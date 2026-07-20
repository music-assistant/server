"""MixIn for NicovideoMusicProvider: search and recommendations methods."""

from __future__ import annotations

from typing import override

from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import (
    BrowseFolder,
    ItemMapping,
    MediaItemType,
    RecommendationFolder,
    SearchResults,
    Track,
)
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
        """
        Perform search on musicprovider.

        :param search_query: Search query.
        :param media_types: A list of media_types to include.
        :param limit: Number of items to return in the search (per type).
        """
        search_result = SearchResults()

        if MediaType.TRACK in media_types:
            tracks = await self.service_manager.search.search_videos_by_keyword(search_query, limit)
            search_result.tracks = tracks

        # Search for both playlists and albums in a single API call for efficiency
        list_media_types: list[MediaType] = [
            mt for mt in media_types if mt in (MediaType.PLAYLIST, MediaType.ALBUM)
        ]

        if list_media_types:
            await self.service_manager.search.search_playlists_and_albums_by_keyword(
                search_query, limit, search_result, list_media_types
            )

        return search_result

    @override
    async def get_recommendations(self) -> list[RecommendationFolder]:
        """
        Get this provider's available recommendation rows, without items.

        Must be fast: return static or cached row descriptors only, without
        live backend calls. The items for a row are fetched separately
        through get_recommendation_items.
        """
        return [
            RecommendationFolder(
                item_id="nicovideo_recommendations",
                name="Recommended tracks",
                translation_key="recommended_tracks",
                provider=self.instance_id,
                icon="mdi-star-circle-outline",
            ),
            RecommendationFolder(
                item_id="nicovideo_history",
                name="Recently played",
                translation_key="recently_played",
                provider=self.instance_id,
                icon="mdi-history",
            ),
            RecommendationFolder(
                item_id="nicovideo_following_activities",
                name="New Tracks from Followed Users",
                translation_key="nicovideo_following_activities",
                provider=self.instance_id,
                icon="mdi-account-plus-outline",
            ),
            RecommendationFolder(
                item_id="nicovideo_like_history",
                name="Recently favorited tracks",
                translation_key="recent_favorite_tracks",
                provider=self.instance_id,
                icon="mdi-heart-outline",
            ),
        ]

    @override
    async def get_recommendation_items(
        self, item_id: str
    ) -> UniqueList[MediaItemType | ItemMapping | BrowseFolder]:
        """
        Get the items for a single recommendation row.

        :param item_id: The item_id of the row, as returned by get_recommendations.
        """
        folder = await self._get_recommendation_folder(item_id)
        if folder is None:
            return UniqueList()
        return folder.items

    @use_cache(1800, base_class=RecommendationFolder)  # Cache for 30 minutes
    async def _get_recommendation_folder(self, item_id: str) -> RecommendationFolder | None:
        """
        Build a single recommendation row with its items fetched (cached per row).

        :param item_id: The item_id of the row, as returned by get_recommendations.
        :return: The populated folder, or None for an unknown item_id.
        """
        items: list[Track]
        if item_id == "nicovideo_recommendations":
            items = await self.service_manager.user.get_recommendations(
                "video_recommendation_recommend", limit=25
            )
        elif item_id == "nicovideo_history":
            items = await self.service_manager.user.get_user_history(limit=50)
        elif item_id == "nicovideo_following_activities":
            items = await self.service_manager.user.get_following_activities(limit=30)
        elif item_id == "nicovideo_like_history":
            items = await self.service_manager.user.get_like_history(limit=50)
        else:
            return None
        folder = next(
            (row for row in await self.get_recommendations() if row.item_id == item_id),
            None,
        )
        if folder is None:
            return None
        folder.items = UniqueList(items)
        return folder

    @override
    @use_cache(3600 * 6, allow_expired_cache=True)  # Cache for 6 hours
    async def get_similar_tracks(self, prov_track_id: str, limit: int = 25) -> list[Track]:
        """Retrieve a dynamic list of similar tracks based on the provided track."""
        return await self.service_manager.user.get_similar_tracks(prov_track_id, limit)
