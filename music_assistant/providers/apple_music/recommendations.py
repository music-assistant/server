"""Recommendations and station helpers for Apple Music."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, cast

from aiohttp import ClientResponseError
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import (
    Artist,
    ItemMapping,
    Playlist,
    RecommendationFolder,
    Track,
)

from music_assistant.controllers.cache import use_cache

from .parsers import parse_artist, parse_station_as_playlist, parse_track

if TYPE_CHECKING:
    from .provider import AppleMusicProvider


def _slugify_title(title: str) -> str:
    """Return a stable, alphanumeric slug for a recommendation folder title."""
    slug = "".join(c for c in title.lower().replace(" ", "_") if c.isalnum() or c == "_")
    if not slug:
        # Fall back to a hash for titles that yield no alphanumeric characters.
        slug = hashlib.md5(title.encode(), usedforsecurity=False).hexdigest()[:12]
    return slug


class AppleMusicRecommendationManager:
    """Handles recommendations, stations, and similar-track lookups."""

    def __init__(self, provider: AppleMusicProvider) -> None:
        """Initialize recommendation manager."""
        self.provider = provider
        self._station_id_to_name: dict[str, str] = {}
        self._station_name_to_id: dict[str, str] = {}
        self.mass = provider.mass
        self.instance_id = provider.instance_id
        self.domain = provider.domain
        self.api = provider.api_client
        self.logger = provider.logger

    @use_cache(3600 * 24, allow_expired_cache=True)
    async def get_similar_tracks(self, prov_track_id: str, limit: int = 25) -> list[Track]:
        """
        Retrieve tracks similar to the provided track.

        :param prov_track_id: The Apple Music track ID.
        :param limit: Maximum number of tracks to return.
        """
        if limit <= 0:
            return []
        endpoint = f"me/stations/next-tracks/ra.{prov_track_id}"
        try:
            response = await self.api.post_data(endpoint, include="artists")
        except ClientResponseError as err:
            if err.status == 500:
                self.logger.debug("Similar tracks unavailable for %s (%s)", prov_track_id, endpoint)
                return []
            raise
        if not response:
            return []
        tracks = [track for track in response.get("data", []) if track and track.get("id")][:limit]
        if not tracks:
            return []
        track_ids = [track["id"] for track in tracks]
        rating_response = await self.api.get_ratings(track_ids, MediaType.TRACK)
        return [
            parse_track(self.provider, track, rating_response.get(track["id"])) for track in tracks
        ]

    @use_cache(3600 * 24)
    async def get_similar_artists(self, prov_artist_id: str, limit: int = 25) -> list[Artist]:
        """Retrieve a list of artists similar to the provided artist via Apple Music similar-artists view."""
        storefront = self.provider._storefront
        response = await self.api.get_data(
            f"catalog/{storefront}/artists/{prov_artist_id}",
            views="similar-artists",
        )
        data = response.get("data", [])
        if not data:
            return []
        similar = data[0].get("views", {}).get("similar-artists", {}).get("data", [])
        artists: list[Artist] = []
        for artist_obj in similar[:limit]:
            parsed = parse_artist(self.provider, artist_obj)
            if isinstance(parsed, Artist):
                artists.append(parsed)
        return artists

    async def get_station_playlist(self, station_id: str) -> Playlist:
        """Fetch name and artwork for a radio station and return it as a dynamic Playlist."""
        try:
            station_response = await self.api.get_data(
                f"catalog/{self.provider._storefront}/stations/{station_id}"
            )
            station_obj = station_response["data"][0]
            station_obj["id"] = station_id
            return parse_station_as_playlist(self.provider, station_obj)
        except MediaNotFoundError, KeyError, IndexError:
            return parse_station_as_playlist(self.provider, {"id": station_id})

    async def get_personal_recommendations(self) -> list[RecommendationFolder]:
        """Fetch personal recommendations grouped into folders by section title."""
        response = await self.api.get_data(
            "me/recommendations?include[personal-recommendation]=contents"
        )
        seen: set[str] = set()
        folders: dict[str, RecommendationFolder] = {}
        # Reset maps so stale entries from previous fetches are not kept.
        self._station_id_to_name.clear()
        self._station_name_to_id.clear()
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
                if playlist.name and playlist.name != station_id:
                    self._station_id_to_name[station_id] = playlist.name
                    self._station_name_to_id[playlist.name] = station_id
                if title not in folders:
                    folders[title] = RecommendationFolder(
                        item_id=_slugify_title(title),
                        provider=self.provider.instance_id,
                        name=title,
                    )
                folders[title].items.append(playlist)
        return list(folders.values())

    async def resolve_station_id(self, stale_id: str) -> str | None:
        """
        Return the current station ID for a stale one.

        :param stale_id: The outdated station ID that may have been rotated by Apple.
        """
        station_name = self._station_id_to_name.get(stale_id)
        if not station_name:
            # Maps may be empty after a process restart; populate from the cached payload first.
            self._populate_station_maps(await self.provider._recommendation_payload())
            station_name = self._station_id_to_name.get(stale_id)
            if not station_name:
                return None
        # Apple rotates station ids: refresh through the mixin cache so the fresh payload
        # (which rebuilds the maps) is also what rows/items serve afterwards.
        await self.provider._refresh_recommendation_payload()
        return self._station_name_to_id.get(station_name)

    async def browse_stations(self) -> list[ItemMapping | Playlist]:
        """Return recommended radio stations from personal recommendations."""
        return cast(
            "list[ItemMapping | Playlist]",
            [
                item
                for folder in await self.provider._recommendation_payload()
                for item in folder.items
            ],
        )

    def _populate_station_maps(self, folders: list[RecommendationFolder]) -> None:
        """
        Populate the station name maps from payload folders, if they are empty.

        After a process restart the payload may be served from the persistent cache
        without running get_personal_recommendations, leaving the maps empty; the
        folder items (stations parsed as playlists) carry the id/name pairs.
        """
        if self._station_id_to_name:
            return
        for folder in folders:
            for item in folder.items:
                if item.name and item.name != item.item_id:
                    self._station_id_to_name[item.item_id] = item.name
                    self._station_name_to_id[item.name] = item.item_id
