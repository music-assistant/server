"""MixIn for NiconicoMusicProvider: search and recommendations methods."""

from __future__ import annotations

from music_assistant_models.enums import MediaType, ProviderFeature
from music_assistant_models.media_items import RecommendationFolder, SearchResults, Track
from music_assistant_models.unique_list import UniqueList

from music_assistant.providers.niconico.provider_mixins.mixin_base import (
    NiconicoMusicProviderMixinBase,
)


class NiconicoMusicProviderExplorerMixin(NiconicoMusicProviderMixinBase):
    """Search and recommendations methods for NiconicoMusicProvider."""

    _supported_features = {
        ProviderFeature.SEARCH,
        ProviderFeature.RECOMMENDATIONS,
        ProviderFeature.SIMILAR_TRACKS,
    }

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
            await self.niconico_adapter.search.search_videos_by_keyword(
                search_query, limit, search_result
            )

        # Search for both playlists and albums in a single API call for efficiency
        list_media_types = [mt for mt in media_types if mt in (MediaType.PLAYLIST, MediaType.ALBUM)]

        if list_media_types:
            await self.niconico_adapter.search.search_playlists_and_albums_by_keyword(
                search_query, limit, search_result, list_media_types
            )

        return search_result

    async def recommendations(self) -> list[RecommendationFolder]:
        """
        Get this provider's recommendations.

        Returns an actual (and often personalised) list of recommendations
        from this provider for the user/account.
        """
        try:
            tracks = await self.niconico_adapter.user.get_recommendations(limit=25)
            return [
                RecommendationFolder(
                    item_id="niconico_recommendations",
                    name="NicoNico Recommendations",
                    provider=self.provider.lookup_key,
                    items=UniqueList(tracks),
                )
            ]
        except Exception as err:
            self.provider.logger.warning("Error fetching NicoNico recommendations: %s", err)
            return []

    async def get_similar_tracks(self, prov_track_id: str, limit: int = 25) -> list[Track]:
        """Retrieve a dynamic list of similar tracks based on the provided track."""
        try:
            return await self.niconico_adapter.user.get_similar_tracks(prov_track_id, limit=limit)
        except Exception as err:
            self.provider.logger.warning(
                "Error fetching similar tracks for %s: %s", prov_track_id, err
            )
            return []
