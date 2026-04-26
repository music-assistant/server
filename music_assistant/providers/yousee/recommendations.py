"""Recommendation logic for YouSee Musik."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.enums import MediaType
from music_assistant_models.media_items import (
    RecommendationFolder,
    UniqueList,
)

from music_assistant.providers.yousee.constants import IMAGE_SIZE, PAGE_SIZE
from music_assistant.providers.yousee.parsers import parse_album, parse_track

if TYPE_CHECKING:
    from music_assistant.providers.yousee.provider import YouSeeMusikProvider


class YouSeeRecommendationsManager:
    """Manages YouSee Musik recommendations."""

    def __init__(self, provider: YouSeeMusikProvider):
        """Initialize recommendation manager."""
        self.provider = provider
        self.api = provider.api
        self.auth = provider.auth
        self.logger = provider.logger
        self.mass = provider.mass

    async def get_recommendations(self) -> list[RecommendationFolder]:
        """Get recommendations from YouSee Musik."""
        query = """
            query Recommendations($imageSize: Int = 512, $first: Int = 50) {
                me {
                    recommendations {
                        albumRecommendations: recommendation(id: "discoveralbums") {
                            id
                            title
                            subtitle
                            description
                            cover(size: $imageSize)
                            ... on AlbumsRecommendation {
                                albums(first: $first) {
                                    items {
                                        id
                                        title
                                        tracksCount
                                        genre
                                        label
                                        releaseDate
                                        available
                                        upc
                                        type
                                        share
                                        cover(size: $imageSize)
                                        artist {
                                            id
                                            title
                                            cover(size: $imageSize)
                                        }
                                        featuredArtists {
                                            items {
                                                id
                                                title
                                                cover(size: $imageSize)
                                            }
                                        }
                                    }
                                }
                            }
                        }
                        trackRecommendations: recommendation(id: "discovertracks") {
                            ...RecommendationTracks
                        }
                        weeklyDiscoveries: recommendation(id: "weeklyDiscoveries") {
                            ...RecommendationTracks
                        }
                        trackRecommendationsFirstMostPlayed: recommendation(
                            id: "tracksbasedonfirstmostplayedartist"
                        ) {
                            ...RecommendationTracks
                        }
                        trackRecommendationsSecondMostPlayed: recommendation(
                            id: "tracksbasedonSecondmostplayedartist"
                        ) {
                            ...RecommendationTracks
                        }
                        historyTopTracks: recommendation(
                            id: "toptracks"
                        ) {
                            ...RecommendationTracks
                        }
                        historyRecentTracks: recommendation(
                            id: "recenttracks"
                        ) {
                            ...RecommendationTracks
                        }
                        yourmix1: recommendation(
                            id: "yourmix"
                        ) {
                            ...RecommendationTracks
                        }
                        yourmix2: recommendation(
                            id: "yourmix2"
                        ) {
                            ...RecommendationTracks
                        }
                        yourmix3: recommendation(
                            id: "yourmix3"
                        ) {
                            ...RecommendationTracks
                        }
                    }
                }
            }
            fragment RecommendationTracks on Recommendation {
                id
                title
                subtitle
                description
                cover(size: $imageSize)
                ... on TracksRecommendation {
                    tracks(first: $first) {
                        items {
                            id
                            title
                            cover(size: $imageSize)
                            isrc
                            duration
                            label
                            artist {
                                id
                                title
                                cover(size: $imageSize)
                            }
                            featuredArtists {
                                items {
                                    id
                                    title
                                    cover(size: $imageSize)
                                }
                            }
                            share
                            genre
                        }
                    }
                }
            }
        """

        variables = {
            "imageSize": IMAGE_SIZE,
            "first": PAGE_SIZE,
        }

        result = await self.api.post_graphql(query, variables)

        if not result or not result.get("data", {}).get("me", {}).get("recommendations"):
            return []

        recommendations: list[RecommendationFolder] = []

        album_keys = ["albumRecommendations"]
        track_keys = [
            "trackRecommendations",
            "weeklyDiscoveries",
            "trackRecommendationsFirstMostPlayed",
            "trackRecommendationsSecondMostPlayed",
            "historyTopTracks",
            "historyRecentTracks",
            "yourmix1",
            "yourmix2",
            "yourmix3",
        ]

        for key in album_keys:
            rec_data = result["data"]["me"]["recommendations"].get(key)
            if rec_data:
                folder = RecommendationFolder(
                    name=rec_data.get("title"),
                    subtitle=rec_data.get("subtitle"),
                    provider=self.provider.instance_id,
                    item_id=rec_data["id"],
                    media_type=MediaType.ALBUM,
                    items=UniqueList(
                        [
                            await parse_album(self.provider, item)
                            for item in rec_data.get("albums", {}).get("items", [])
                        ]
                    ),
                )
                recommendations.append(folder)
        for key in track_keys:
            rec_data = result["data"]["me"]["recommendations"].get(key)
            if rec_data:
                folder = RecommendationFolder(
                    name=rec_data.get("title"),
                    subtitle=rec_data.get("subtitle"),
                    provider=self.provider.instance_id,
                    item_id=rec_data["id"],
                    media_type=MediaType.TRACK,
                    items=UniqueList(
                        [
                            await parse_track(self.provider, item)
                            for item in rec_data.get("tracks", {}).get("items", [])
                        ]
                    ),
                )
                recommendations.append(folder)

        return recommendations
