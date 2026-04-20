"""Recommendations and station helpers for Apple Music."""

from __future__ import annotations

from typing import TYPE_CHECKING

from music_assistant_models.enums import MediaType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import ItemMapping, Playlist, RecommendationFolder, Track

from music_assistant.controllers.cache import use_cache

from .parsers import parse_station_as_playlist, parse_track

if TYPE_CHECKING:
    from .provider import AppleMusicProvider


class AppleMusicRecommendationManager:
    """Handles recommendations, stations, and similar-track lookups."""

    def __init__(self, provider: AppleMusicProvider) -> None:
        """Initialize recommendation manager."""
        self.provider = provider
        self.mass = provider.mass
        self.instance_id = provider.instance_id
        self.domain = provider.domain
        self.api = provider.api_client
        self.logger = provider.logger

    @use_cache(3600 * 24)
    async def get_similar_tracks(self, prov_track_id: str, limit: int = 25) -> list[Track]:
        """Retrieve a dynamic list of tracks based on the provided item."""
        # Apple Music only provides ~2 tracks per call, cap at 6 to avoid flooding the API.
        limit = min(limit, 6)
        endpoint = f"me/stations/next-tracks/ra.{prov_track_id}"
        found_tracks: list[Track] = []
        while len(found_tracks) < limit:
            response = await self.api.post_data(endpoint, include="artists")
            if not response or "data" not in response:
                break
            track_ids = [track["id"] for track in response["data"] if track and track["id"]]
            rating_response = await self.api.get_ratings(track_ids, MediaType.TRACK)
            for track in response["data"]:
                if track and track["id"]:
                    found_tracks.append(
                        parse_track(self.provider, track, rating_response.get(track["id"]))
                    )
        return found_tracks

    async def get_station_playlist(self, station_id: str) -> Playlist:
        """Fetch name and artwork for a radio station and return it as a dynamic Playlist."""
        try:
            station_response = await self.api.get_data(
                f"catalog/{self.provider._storefront}/stations/{station_id}"
            )
            station_obj = station_response["data"][0]
            station_obj["id"] = station_id
            return parse_station_as_playlist(self.provider, station_obj)
        except (MediaNotFoundError, KeyError, IndexError):
            return parse_station_as_playlist(self.provider, {"id": station_id})

    @use_cache(3600)
    async def get_personal_recommendations(self) -> list[RecommendationFolder]:
        """Fetch personal recommendations grouped into folders by section title."""
        response = await self.api.get_data(
            "me/recommendations?include[personal-recommendation]=contents"
        )
        seen: set[str] = set()
        folders: dict[str, RecommendationFolder] = {}
        for recommendation in response.get("data", []):
            rec_id = recommendation.get("id", "")
            title = (
                recommendation.get("attributes", {}).get("title", {}).get("stringForDisplay", "")
            )
            if not rec_id or not title:
                continue
            contents = recommendation.get("relationships", {}).get("contents", {})
            for item in contents.get("data", []):
                if item.get("type") != "stations":
                    continue
                station_id = item.get("id")
                if not station_id or station_id in seen:
                    continue
                attributes = item.get("attributes", {})
                if attributes.get("isLive", False):
                    # Live broadcast stations require Widevine DRM; skip them.
                    continue
                seen.add(station_id)
                if attributes.get("name"):
                    playlist = parse_station_as_playlist(self.provider, item)
                else:
                    playlist = await self.provider.get_playlist(station_id)
                    if playlist.name == station_id:
                        continue
                if title not in folders:
                    folders[title] = RecommendationFolder(
                        item_id=rec_id,
                        provider=self.provider.instance_id,
                        name=title,
                    )
                folders[title].items.append(playlist)
        return list(folders.values())

    async def browse_stations(self) -> list[ItemMapping | Playlist]:
        """Return recommended radio stations from personal recommendations."""
        return [
            item for folder in await self.get_personal_recommendations() for item in folder.items
        ]
