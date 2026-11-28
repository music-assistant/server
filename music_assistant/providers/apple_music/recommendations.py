"""Recommendations and station helpers for Apple Music."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

from aiohttp import ClientResponseError
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import (
    Album,
    Artist,
    ItemMapping,
    Playlist,
    Radio,
    RecommendationFolder,
    Track,
)

from music_assistant.controllers.cache import use_cache

from .constants import PARSED_ITEM_CACHE_CHECKSUM
from .parsers import parse_album, parse_artist, parse_playlist, parse_station, parse_track

if TYPE_CHECKING:
    from .provider import AppleMusicProvider

# stations looked up per request when resolving the unnamed stations of the recommendations
_STATION_BATCH_SIZE = 25
# content types of the personal recommendations that are kept
_RECOMMENDED_TYPES = ("stations", "albums", "playlists")


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
        return [parse_track(self.provider, track) for track in tracks]

    @use_cache(3600 * 24)
    async def get_similar_artists(self, prov_artist_id: str, limit: int = 25) -> list[Artist]:
        """Retrieve a list of artists similar to the provided artist via Apple Music similar-artists view."""
        storefront = self.provider.storefront
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

    @use_cache(3600 * 24, cache_checksum=PARSED_ITEM_CACHE_CHECKSUM)
    async def get_station(self, station_id: str) -> Radio:
        """Fetch name and artwork for a radio station and return it as a Radio."""
        try:
            station_response = await self.api.get_data(
                f"catalog/{self.provider.storefront}/stations/{station_id}"
            )
            station_obj = station_response["data"][0]
            station_obj["id"] = station_id
            return parse_station(self.provider, station_obj)
        except MediaNotFoundError, KeyError, IndexError:
            return parse_station(self.provider, {"id": station_id})

    async def get_personal_recommendations(self) -> list[RecommendationFolder]:
        """Fetch personal recommendations grouped into folders by section title."""
        response = await self.api.get_data(
            "me/recommendations?include[personal-recommendation]=contents"
        )
        seen: set[tuple[str, str]] = set()
        entries: list[tuple[str, dict[str, Any]]] = []
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
                item_type = item.get("type")
                item_id = item.get("id")
                if item_type not in _RECOMMENDED_TYPES or not item_id:
                    continue
                if (item_type, item_id) in seen:
                    continue
                if item_type == "stations" and item.get("attributes", {}).get("isLive", False):
                    # the live stations have their own listing in browse
                    continue
                seen.add((item_type, item_id))
                entries.append((title, item))
        unnamed = [
            item["id"]
            for _, item in entries
            if item["type"] == "stations" and not item.get("attributes", {}).get("name")
        ]
        fetched = await self._get_stations(unnamed)
        folders: dict[str, RecommendationFolder] = {}
        for title, item in entries:
            if (media_item := self._parse_recommended_item(item, fetched)) is None:
                continue
            if title not in folders:
                folders[title] = RecommendationFolder(
                    item_id=_slugify_title(title),
                    provider=self.provider.instance_id,
                    name=title,
                )
            folders[title].items.append(media_item)
        return list(folders.values())

    async def resolve_station_id(self, stale_id: str) -> str | None:
        """
        Return the current station ID for a stale one.

        :param stale_id: The outdated station ID that may have been rotated by Apple.
        """
        station_name = self._station_id_to_name.get(stale_id)
        if not station_name:
            # Maps may be empty after a process restart; populate from the cached payload first.
            self._populate_station_maps(await self.provider.get_recommendation_payload())
            station_name = self._station_id_to_name.get(stale_id)
            if not station_name:
                return None
        # Apple rotates station ids: refresh through the mixin cache so the fresh payload
        # (which rebuilds the maps) is also what rows/items serve afterwards.
        await self.provider.refresh_recommendation_payload()
        return self._station_name_to_id.get(station_name)

    async def browse_stations(self) -> list[ItemMapping | Radio]:
        """Return recommended radio stations from personal recommendations."""
        return [
            item
            for folder in await self.provider.get_recommendation_payload()
            for item in folder.items
            if isinstance(item, Radio)
        ]

    def _parse_recommended_item(
        self, item: dict[str, Any], fetched: dict[str, Radio]
    ) -> Radio | Album | Playlist | None:
        """
        Parse one content item of the personal recommendations, or None to leave it out.

        :param item: The raw station, album or playlist object.
        :param fetched: The looked-up stations for the stations that came without a name.
        """
        item_id = item["id"]
        if item["type"] == "stations":
            if item.get("attributes", {}).get("name"):
                radio = parse_station(self.provider, item)
            elif (fetched_radio := fetched.get(item_id)) is not None:
                radio = fetched_radio
            else:
                return None
            if radio.name and radio.name != item_id:
                self._station_id_to_name[item_id] = radio.name
                self._station_name_to_id[radio.name] = item_id
            return radio
        try:
            if item["type"] == "albums":
                album = parse_album(self.provider, item)
                # an album without attributes only parses to a mapping without a name
                return album if isinstance(album, Album) else None
            return parse_playlist(self.provider, item)
        except (KeyError, TypeError, ValueError) as err:
            self.logger.debug("Skipping recommended %s %s: %s", item["type"], item_id, err)
            return None

    async def _get_stations(self, station_ids: list[str]) -> dict[str, Radio]:
        """
        Return the named stations among the given ids, keyed by station id.

        :param station_ids: The station ids to look up.
        """
        stations: dict[str, Radio] = {}
        for i in range(0, len(station_ids), _STATION_BATCH_SIZE):
            batch = station_ids[i : i + _STATION_BATCH_SIZE]
            try:
                response = await self.api.get_data(
                    f"catalog/{self.provider.storefront}/stations", ids=",".join(batch)
                )
            except MediaNotFoundError:
                response = {}
            answered: set[str] = set()
            for station_obj in response.get("data", []):
                if (station_id := station_obj.get("id")) not in batch:
                    continue
                answered.add(station_id)
                if not station_obj.get("attributes", {}).get("isLive", False):
                    stations[station_id] = parse_station(self.provider, station_obj)
            # a station Apple answers for under another id is looked up on its own, cached
            for station_id in batch:
                if station_id not in answered:
                    stations[station_id] = await self.get_station(station_id)
        return {
            station_id: radio
            for station_id, radio in stations.items()
            if radio.name and radio.name != station_id
        }

    def _populate_station_maps(self, folders: list[RecommendationFolder]) -> None:
        """
        Populate the station name maps from payload folders, if they are empty.

        After a process restart the payload may be served from the persistent cache
        without running get_personal_recommendations, leaving the maps empty; the
        folder items (stations parsed as radios) carry the id/name pairs.
        """
        if self._station_id_to_name:
            return
        for folder in folders:
            for item in folder.items:
                if isinstance(item, Radio) and item.name and item.name != item.item_id:
                    self._station_id_to_name[item.item_id] = item.name
                    self._station_name_to_id[item.name] = item.item_id
