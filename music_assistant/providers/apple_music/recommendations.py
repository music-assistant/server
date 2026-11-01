"""Recommendations and station helpers for Apple Music."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any, cast

from aiohttp import ClientResponseError
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import (
    Artist,
    ItemMapping,
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

    @use_cache(3600 * 24, cache_checksum=PARSED_ITEM_CACHE_CHECKSUM)
    async def get_station(self, station_id: str) -> Radio:
        """Fetch name and artwork for a radio station and return it as a dynamic Radio."""
        try:
            station_response = await self.api.get_data(
                f"catalog/{self.provider._storefront}/stations/{station_id}"
            )
            station_obj = station_response["data"][0]
            station_obj["id"] = station_id
            return parse_station(self.provider, station_obj)
        except MediaNotFoundError, KeyError, IndexError:
            return parse_station(self.provider, {"id": station_id})

    async def get_personal_recommendations(self) -> list[RecommendationFolder]:
        """Expose Apple's supported recommendation media types in provider order.

        Stations retain their separate batched name-resolution path. Missing
        metadata never results in guessed titles or unbounded item lookups.
        """
        response = await self.api.get_data(
            "me/recommendations?include[personal-recommendation]=contents"
        )
        entries: list[tuple[str, dict[str, Any]]] = []
        seen: set[tuple[str, str, str]] = set()
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
                if not item_id or item_type not in {
                    "artists", "albums", "songs", "playlists", "stations"
                }:
                    continue
                key = (title, item_type, str(item_id))
                if key in seen:
                    continue
                attrs = item.get("attributes") or {}
                if item_type == "stations" and attrs.get("isLive", False):
                    # Live broadcasts require Widevine DRM, as before.
                    continue
                if item_type != "stations" and (
                    not isinstance(attrs, dict) or not attrs.get("name")
                ):
                    continue
                seen.add(key)
                entries.append((title, item))

        unnamed = list(dict.fromkeys(
            item["id"] for _, item in entries
            if item["type"] == "stations" and not item.get("attributes", {}).get("name")
        ))
        fetched = await self._get_stations(unnamed)
        folders: dict[str, RecommendationFolder] = {}
        for title, item in entries:
            item_type, item_id = item["type"], item["id"]
            if item_type == "stations":
                if item.get("attributes", {}).get("name"):
                    media = parse_station(self.provider, item)
                else:
                    media = fetched.get(item_id)
                if media is None or not media.name or media.name == item_id:
                    continue
                self._station_id_to_name[item_id] = media.name
                self._station_name_to_id[media.name] = item_id
            else:
                try:
                    if item_type == "artists":
                        media = parse_artist(self.provider, item)
                    elif item_type == "albums":
                        media = parse_album(self.provider, item)
                    elif item_type == "songs":
                        media = parse_track(self.provider, item)
                    else:
                        media = parse_playlist(self.provider, item)
                except (KeyError, TypeError, ValueError) as err:
                    self.logger.debug(
                        "Skipping incomplete Apple recommendation %s: %s",
                        item_type, type(err).__name__,
                    )
                    continue
                if media is None or not media.name:
                    continue

            if title not in folders:
                folders[title] = RecommendationFolder(
                    item_id=_slugify_title(title),
                    provider=self.provider.instance_id,
                    name=title,
                )
            folders[title].items.append(media)
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

    async def browse_stations(self) -> list[ItemMapping | Radio]:
        """Return recommended radio stations from personal recommendations."""
        return cast(
            "list[ItemMapping | Radio]",
            [
                item
                for folder in await self.provider._recommendation_payload()
                for item in folder.items
                if isinstance(item, Radio)
            ],
        )

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
                    f"catalog/{self.provider._storefront}/stations", ids=",".join(batch)
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
                # Only Radio items participate in station ID recovery.
                if isinstance(item, Radio) and item.name and item.name != item.item_id:
                    self._station_id_to_name[item.item_id] = item.name
                    self._station_name_to_id[item.name] = item.item_id
