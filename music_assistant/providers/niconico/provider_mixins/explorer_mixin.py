"""MixIn for NiconicoMusicProvider: search and recommendations methods."""

from __future__ import annotations

from music_assistant_models.enums import MediaType, ProviderFeature
from music_assistant_models.media_items import RecommendationFolder, SearchResults, Track
from music_assistant_models.unique_list import UniqueList

from music_assistant.providers.niconico.helpers import handle_niconico_errors
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
        recommendation_folders = []

        # Get target count from config
        target_count = self.niconico_config.get_recommendation_count()

        # General recommendations
        async with handle_niconico_errors(self.provider.logger, "fetching recommendations"):
            # Start with the target count, but be prepared to fetch more if filtering reduces count
            tracks = await self._fetch_recommendations_with_filtering(target_count)
            if tracks:
                recommendation_folders.append(
                    RecommendationFolder(
                        item_id="niconico_recommendations",
                        name="niconico recommendations",
                        provider=self.provider.lookup_key,
                        items=UniqueList(tracks),
                    )
                )

        # History-based recommendations
        async with handle_niconico_errors(self.provider.logger, "fetching history"):
            history_count = self.niconico_config.get_history_count()
            history_tracks = await self.niconico_adapter.user.get_user_history(limit=history_count)
            if history_tracks:
                recommendation_folders.append(
                    RecommendationFolder(
                        item_id="niconico_history",
                        name="Recently watched  (niconico history)",
                        provider=self.provider.lookup_key,
                        items=UniqueList(history_tracks),
                    )
                )

        # Following activities recommendations
        async with handle_niconico_errors(self.provider.logger, "fetching following activities"):
            following_count = self.niconico_config.get_following_activities_count()
            following_tracks = await self.niconico_adapter.user.get_following_activities(
                limit=following_count
            )
            if following_tracks:
                recommendation_folders.append(
                    RecommendationFolder(
                        item_id="niconico_following_activities",
                        name="New Tracks from Followed Users",
                        provider=self.provider.lookup_key,
                        items=UniqueList(following_tracks),
                    )
                )

        # Like History recommendations
        async with handle_niconico_errors(self.provider.logger, "fetching like history"):
            like_history_count = self.niconico_config.get_history_count()  # Same as history
            like_history_tracks = await self.niconico_adapter.user.get_like_history(
                limit=like_history_count
            )
            if like_history_tracks:
                recommendation_folders.append(
                    RecommendationFolder(
                        item_id="niconico_like_history",
                        name="Recently liked (Like history)",
                        provider=self.provider.lookup_key,
                        items=UniqueList(like_history_tracks),
                    )
                )

        return recommendation_folders

    async def get_similar_tracks(self, prov_track_id: str, limit: int = 25) -> list[Track]:
        """Retrieve a dynamic list of similar tracks based on the provided track."""
        # Use config count if limit is default
        target_count = self.niconico_config.get_recommendation_count() if limit == 25 else limit

        async with handle_niconico_errors(
            self.provider.logger, "fetching similar tracks", prov_track_id
        ) as error_state:
            tracks = await self._fetch_similar_tracks_with_filtering(prov_track_id, target_count)
            if not error_state:
                return tracks

        return []

    def _track_has_required_tags(self, track_tags: list[str], required_tags: list[str]) -> bool:
        """Check if track has at least one of the required tags."""
        if not required_tags:
            # If no tags are required, allow all tracks
            return True

        if not track_tags:
            # If track has no tags but tags are required, reject
            return False

        # Check if track has at least one required tag
        return any(tag in track_tags for tag in required_tags)

    async def _filter_tracks_by_tags(self, tracks: list[Track]) -> list[Track]:
        """Filter tracks based on required tags from configuration."""
        required_tags = self.niconico_config.get_required_tags_for_recommendations()
        if not required_tags:
            # No filtering needed
            return tracks

        filtered_tracks = []
        for track in tracks:
            try:
                # Get tags from cache first, wait if currently fetching
                tag_names = await self.tag_manager.get_tags(track.item_id, wait_if_fetching=True)

                if self._track_has_required_tags(tag_names, required_tags):
                    filtered_tracks.append(track)
            except Exception as err:
                # If we can't get tags, log warning but don't fail entirely
                self.provider.logger.warning(
                    "Failed to get tags for track %s: %s", track.item_id, err
                )
                # Include track if tag fetching fails (graceful degradation)
                filtered_tracks.append(track)

        return filtered_tracks

    async def _fetch_recommendations_with_filtering(self, target_count: int) -> list[Track]:
        """Fetch recommendations with dynamic count adjustment for tag filtering."""
        required_tags = self.niconico_config.get_required_tags_for_recommendations()
        if not required_tags:
            # No filtering needed, just fetch the target count
            return await self.niconico_adapter.user.get_recommendations(limit=target_count)

        # With filtering, we need to fetch more to account for filtered out tracks
        max_attempts = 5
        all_tracks = []
        seen_track_ids = set()

        for attempt in range(max_attempts):
            current_limit = target_count * 5

            try:
                batch_tracks = await self.niconico_adapter.user.get_recommendations(
                    "video_recommendation_recommend", limit=current_limit
                )

                # Filter out duplicates
                new_tracks = [
                    track for track in batch_tracks if track.item_id not in seen_track_ids
                ]

                for track in new_tracks:
                    seen_track_ids.add(track.item_id)

                all_tracks.extend(new_tracks)

                # Apply filtering to all collected tracks
                filtered_tracks = await self._filter_tracks_by_tags(all_tracks)

                if len(filtered_tracks) >= target_count:
                    # We have enough filtered tracks
                    return filtered_tracks[:target_count]

                # Not enough tracks yet, prepare for next attempt
                if attempt < max_attempts - 1:
                    self.provider.logger.info(
                        "Got %d filtered tracks (target: %d), fetching more...",
                        len(filtered_tracks),
                        target_count,
                    )

            except Exception as err:
                self.provider.logger.warning(
                    "Failed to fetch recommendations batch (attempt %d): %s", attempt + 1, err
                )
                break

        # Return what we have, even if less than target
        filtered_tracks = await self._filter_tracks_by_tags(all_tracks)
        return filtered_tracks[:target_count] if filtered_tracks else []

    async def _fetch_similar_tracks_with_filtering(
        self, prov_track_id: str, target_count: int
    ) -> list[Track]:
        """Fetch similar tracks with dynamic count adjustment for tag filtering."""
        required_tags = self.niconico_config.get_required_tags_for_recommendations()
        if not required_tags:
            # No filtering needed, just fetch the target count
            return await self.niconico_adapter.user.get_similar_tracks(
                prov_track_id, limit=target_count
            )

        # With filtering, we need to fetch more to account for filtered out tracks
        # For similar tracks, we have less control over pagination, so we use a simpler approach
        fetch_limit = min(int(target_count * 2.5), 100)  # Fetch 2.5x target, cap at 100

        try:
            tracks = await self.niconico_adapter.user.get_similar_tracks(
                prov_track_id, limit=fetch_limit
            )
            filtered_tracks = await self._filter_tracks_by_tags(tracks)
            return filtered_tracks[:target_count] if filtered_tracks else []

        except Exception as err:
            self.provider.logger.warning(
                "Failed to fetch similar tracks for %s: %s", prov_track_id, err
            )
            return []
