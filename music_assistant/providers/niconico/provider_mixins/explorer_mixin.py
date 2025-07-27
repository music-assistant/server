"""MixIn for NiconicoMusicProvider: search and recommendations methods."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.enums import MediaType, ProviderFeature
from music_assistant_models.media_items import SearchResults, Track

from music_assistant.providers.niconico.provider_mixins.mixin_base import (
    NiconicoMusicProviderMixinBase,
)

if TYPE_CHECKING:
    from music_assistant_models.media_items import RecommendationFolder


class NiconicoMusicProviderExplorerMixin(NiconicoMusicProviderMixinBase):
    """Search and recommendations methods for NiconicoMusicProvider."""

    _supported_features = {
        ProviderFeature.SEARCH,
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

        if MediaType.PLAYLIST in media_types:
            await self.niconico_adapter.search.search_playlists_by_keyword(
                search_query, limit, search_result
            )

        return search_result

    async def recommendations(self) -> list[RecommendationFolder]:
        """
        Get this provider's recommendations.

        Returns an actual (and often personalised) list of recommendations
        from this provider for the user/account.
        """
        # Get this provider's recommendations.
        # This is only called if you reported the RECOMMENDATIONS feature in the supported_features.
        return []

    async def get_similar_tracks(  # type: ignore[empty-body]
        self, prov_track_id: str, limit: int = 25
    ) -> list[Track]:
        """Retrieve a dynamic list of similar tracks based on the provided track."""
        # Get a list of similar tracks based on the provided track.
        # This is only called if the provider supports the SIMILAR_TRACKS feature.
