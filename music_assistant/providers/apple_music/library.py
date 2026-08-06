"""Library management for Apple Music."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from music_assistant_models.enums import MediaType
from music_assistant_models.errors import MusicAssistantError
from music_assistant_models.media_items import Track

from .helpers.utils import is_catalog_id, is_library_id, translate_media_type_to_apple_type
from .parsers import parse_album, parse_artist, parse_playlist, parse_track

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from music_assistant_models.media_items import Album, Artist, MediaItemType, Playlist

    from .provider import AppleMusicProvider

# 100 is the maximum Apple accepts; the heavy includes only cost ~20% latency per page.
_TRACK_PAGE_SIZE = 100

# Detail lookups for weak-mapped library tracks go out in batches of this many ids.
_DETAIL_BATCH_SIZE = 100

# Catalog enrichment batch size: 300 (the documented max) returns a 504, so cap at 150. This also
# bounds the in-flight window, keeping a ~100k library from being materialized at once.
_TRACK_SYNC_WINDOW = 150

# Limit search fallback attempts per window to avoid rate limits/latency when many IDs are deprecated.
_MAX_SEARCH_FALLBACK_PER_WINDOW = 10


class AppleMusicLibraryManager:
    """Manages Apple Music library operations."""

    def __init__(self, provider: AppleMusicProvider) -> None:
        """Initialize library manager."""
        self.provider = provider
        self.api = provider.api_client
        self.logger = provider.logger

    async def get_library_artists(self) -> AsyncGenerator[Artist]:
        """Retrieve library artists from the provider."""
        endpoint = "me/library/artists"
        for item in await self.api.get_all_items(
            endpoint, include="catalog", extend="editorialNotes"
        ):
            if item and item["id"]:
                yield cast("Artist", parse_artist(self.provider, item))

    async def get_library_albums(self) -> AsyncGenerator[Album]:
        """Retrieve library albums from the provider."""
        endpoint = "me/library/albums"
        album_items = await self.api.get_all_items(
            endpoint, include="catalog,artists", extend="editorialNotes"
        )
        album_catalog_item_ids = [
            item["id"]
            for item in album_items
            if item and item["id"] and not is_library_id(item["id"])
        ]
        album_library_item_ids = [
            item["id"] for item in album_items if item and item["id"] and is_library_id(item["id"])
        ]
        rating_catalog_response = await self.api.get_ratings(
            album_catalog_item_ids, MediaType.ALBUM
        )
        rating_library_response = await self.api.get_ratings(
            album_library_item_ids, MediaType.ALBUM
        )
        for item in album_items:
            if item and item["id"]:
                is_favourite = (
                    rating_catalog_response.get(item["id"])
                    if not is_library_id(item["id"])
                    else rating_library_response.get(item["id"])
                )
                album = parse_album(self.provider, item, is_favourite)
                if album:
                    yield cast("Album", album)

    async def get_library_tracks(self) -> AsyncGenerator[Track]:
        """Retrieve library tracks from the provider."""
        # Enrich and yield in bounded windows so the full library is never held in memory at once.
        catalog_items: dict[str, dict[str, Any]] = {}
        library_only_items: list[dict[str, Any]] = []
        async for item in self.api.iter_all_items(
            "me/library/songs", include="catalog,albums,artists", page_size=_TRACK_PAGE_SIZE
        ):
            catalog_id = item.get("attributes", {}).get("playParams", {}).get("catalogId")
            if not catalog_id:
                library_only_items.append(item)
            else:
                catalog_items[catalog_id] = item
            if len(catalog_items) >= _TRACK_SYNC_WINDOW:
                async for track in self._flush_catalog_tracks(catalog_items):
                    yield track
                catalog_items = {}
            if len(library_only_items) >= _TRACK_SYNC_WINDOW:
                async for track in self._flush_library_only_tracks(library_only_items):
                    yield track
                library_only_items = []
        async for track in self._flush_catalog_tracks(catalog_items):
            yield track
        async for track in self._flush_library_only_tracks(library_only_items):
            yield track

    def _track_has_weak_album_mapping(self, track: Track) -> bool:
        """Return True for missing or name-only album mapping."""
        if not track.album:
            return True
        album_item_id = track.album.item_id
        return (
            album_item_id == track.album.name
            and not is_library_id(album_item_id)
            and not is_catalog_id(album_item_id)
        )

    def _apply_album_detail(
        self,
        item: dict[str, Any],
        parsed_track: Track,
        detail: dict[str, Any],
        is_favourite: bool | None,
    ) -> Track:
        """Return the detail-based track when it resolves the album the listing lacked."""
        detailed_track = parse_track(self.provider, detail, is_favourite)
        if self._track_has_weak_album_mapping(detailed_track):
            # Keep detail album fallback if list had no album.
            if not parsed_track.album and detailed_track.album:
                return detailed_track
            self.logger.debug(
                "Library song %s still has no resolvable album mapping after detail fetch",
                item["id"],
            )
            return parsed_track
        return detailed_track

    async def get_library_playlists(self) -> AsyncGenerator[Playlist]:
        """Retrieve playlists from the provider."""
        endpoint = "me/library/playlists"
        playlist_items = await self.api.get_all_items(endpoint)
        playlist_library_item_ids = [
            item["id"]
            for item in playlist_items
            if item and item["id"] and is_library_id(item["id"])
        ]
        rating_library_response = await self.api.get_ratings(
            playlist_library_item_ids, MediaType.PLAYLIST
        )
        for item in playlist_items:
            is_favourite = rating_library_response.get(item["id"], False)
            # Fetch catalog metadata, but keep library ID for write operations.
            if item["attributes"]["hasCatalog"]:
                yield await self.provider.media_manager.get_playlist(
                    item["attributes"]["playParams"]["globalId"],
                    is_favourite,
                    can_edit_hint=item["attributes"].get("canEdit"),
                    library_id_override=item["id"] if is_library_id(item["id"]) else None,
                )
            elif item and item["id"]:
                yield parse_playlist(self.provider, item, is_favourite)

    async def library_add(self, item: MediaItemType) -> None:
        """Add item to library."""
        if item.media_type == MediaType.ARTIST:
            # The POST /v1/me/library endpoint does not support ids[artists];
            # artists appear in the library implicitly via their albums/songs.
            self.logger.debug(
                "Skipping library_add for artist %s: Apple Music does not support "
                "adding artists directly via the API.",
                item.name,
            )
            return
        item_type = translate_media_type_to_apple_type(item.media_type)
        kwargs = {f"ids[{item_type}]": item.item_id}
        await self.api.post_data("me/library", **kwargs)

    async def library_remove(self, prov_item_id: str, media_type: MediaType) -> None:
        """Remove item from library."""
        self.logger.debug(
            "Deleting items from your library is not yet supported by the Apple Music API. "
            f"Skipping deletion of {media_type} - {prov_item_id}."
        )

    async def add_playlist_tracks(self, prov_playlist_id: str, prov_track_ids: list[str]) -> None:
        """Add track(s) to playlist."""
        endpoint = f"me/library/playlists/{prov_playlist_id}/tracks"
        data = {
            "data": [
                {
                    "id": track_id,
                    "type": "library-songs" if is_library_id(track_id) else "songs",
                }
                for track_id in prov_track_ids
            ]
        }
        await self.api.post_data(endpoint, data=data)

    async def remove_playlist_tracks(
        self, prov_playlist_id: str, positions_to_remove: tuple[int, ...]
    ) -> None:
        """Remove track(s) from playlist."""
        message = (
            "Removing tracks from playlists is not supported by the Apple Music "
            "API. Make sure to delete them using the Apple Music app."
        )
        raise MusicAssistantError(message)

    async def set_favorite(self, prov_item_id: str, media_type: MediaType, favorite: bool) -> None:
        """Set the favorite status of an item."""
        data = {
            "type": "ratings",
            "attributes": {"value": 1 if favorite else -1},
        }
        item_type = translate_media_type_to_apple_type(media_type)
        if is_catalog_id(prov_item_id):
            endpoint = f"me/ratings/{item_type}/{prov_item_id}"
        else:
            endpoint = f"me/ratings/library-{item_type}/{prov_item_id}"
        await self.api.put_data(endpoint, data=data)

    async def _flush_catalog_tracks(
        self, library_items_by_catalog_id: dict[str, dict[str, Any]]
    ) -> AsyncGenerator[Track]:
        """Enrich one window of catalog-backed library tracks with catalog detail and yield them."""
        if not library_items_by_catalog_id:
            return
        catalog_ids = list(library_items_by_catalog_id)
        catalog_endpoint = f"catalog/{self.provider._storefront}/songs"
        response = await self.api.get_data(
            catalog_endpoint, ids=",".join(catalog_ids), include="artists,albums"
        )
        rating_response = await self.api.get_ratings(catalog_ids, MediaType.TRACK)
        returned_catalog_ids: set[str] = set()
        for item in response.get("data", []):
            returned_catalog_ids.add(item["id"])
            is_favourite = rating_response.get(item["id"])
            parsed_track = parse_track(self.provider, item, is_favourite)
            if self._track_has_weak_album_mapping(parsed_track) and (
                library_item := library_items_by_catalog_id.get(item["id"])
            ):
                parsed_library_track = parse_track(self.provider, library_item, is_favourite)
                if parsed_library_track.album and not self._track_has_weak_album_mapping(
                    parsed_library_track
                ):
                    parsed_track.album = parsed_library_track.album
            yield parsed_track
        # Handle deprecated catalog IDs: search replacement with per-window limit
        search_attempts = 0
        for missing_catalog_id in (cid for cid in catalog_ids if cid not in returned_catalog_ids):
            if library_item := library_items_by_catalog_id.get(missing_catalog_id):
                library_item_id = library_item.get("id")
                is_favourite = rating_response.get(missing_catalog_id)

                # Limit search attempts per window to avoid API rate limits
                if search_attempts >= _MAX_SEARCH_FALLBACK_PER_WINDOW:
                    # Mark remaining as unavailable without attempting search
                    parsed_track = parse_track(self.provider, library_item, is_favourite)
                    for mapping in parsed_track.provider_mappings:
                        if mapping.provider_instance == self.provider.instance_id:
                            mapping.available = False
                    self.logger.debug(
                        "Skipping search fallback for %s (reached window limit of %d searches)",
                        library_item_id,
                        _MAX_SEARCH_FALLBACK_PER_WINDOW,
                    )
                    yield parsed_track
                    continue

                search_attempts += 1

                # Try to find current catalog version via search
                replacement_track = await self._try_search_replacement_for_deprecated_track(
                    library_item, is_favourite
                )

                if replacement_track:
                    yield replacement_track
                else:
                    # No replacement found - yield library-only track but mark unavailable
                    # (these often have corrupt streams from Apple's deprecated catalog versions)
                    parsed_track = parse_track(self.provider, library_item, is_favourite)
                    for mapping in parsed_track.provider_mappings:
                        if mapping.provider_instance == self.provider.instance_id:
                            mapping.available = False
                    self.logger.debug(
                        "Library track %s references deprecated catalog ID %s - marked unavailable",
                        library_item_id,
                        missing_catalog_id,
                    )
                    yield parsed_track

    async def _try_search_replacement_for_deprecated_track(
        self, library_item: dict[str, Any], is_favourite: bool | None
    ) -> Track | None:
        """
        Try to find a current catalog version for a deprecated library track via search.

        Returns the replacement track if found, None otherwise.
        """
        attributes = library_item.get("attributes", {})
        track_name = attributes.get("name")
        artist_name = attributes.get("artistName")
        album_name = attributes.get("albumName")

        if not track_name or not artist_name:
            return None

        # Search for track: "Artist Track"
        search_query = f"{artist_name} {track_name}"
        try:
            search_results = await self.provider.media_manager.search(
                search_query, [MediaType.TRACK], limit=10
            )

            if not search_results.tracks:
                return None

            # Try to find exact match (case-insensitive)
            track_name_lower = track_name.lower()
            artist_name_lower = artist_name.lower()
            album_name_lower = album_name.lower() if album_name else None

            for track in search_results.tracks:
                # Skip ItemMapping entries (only interested in full Track objects)
                if not isinstance(track, Track):
                    continue

                # Check track name match
                if track.name.lower() != track_name_lower:
                    continue

                # Check artist match
                if not any(a.name.lower() == artist_name_lower for a in track.artists):
                    continue

                # If we have album info, require album match
                if album_name_lower:
                    if not track.album or track.album.name.lower() != album_name_lower:
                        # Album mismatch or missing - might be a different version/remaster
                        continue

                # Found a match! Update favorite status and return
                track.favorite = is_favourite or False
                self.logger.debug(
                    "Found replacement catalog track %s for deprecated library track %s",
                    track.item_id,
                    library_item.get("id"),
                )
                return track

            return None

        except Exception as err:
            self.logger.debug(
                "Search fallback failed for track '%s' by '%s': %s",
                track_name,
                artist_name,
                err,
                exc_info=True,
            )
            return None

    async def _flush_library_only_tracks(
        self, library_only_items: list[dict[str, Any]]
    ) -> AsyncGenerator[Track]:
        """Enrich one window of library-only tracks (no catalog id) and yield them."""
        if not library_only_items:
            return
        library_ids = [item["id"] for item in library_only_items if item and item["id"]]
        rating_response = await self.api.get_ratings(library_ids, MediaType.TRACK)
        parsed_tracks = [
            (item, parse_track(self.provider, item, rating_response.get(item["id"])))
            for item in library_only_items
        ]
        details = await self._fetch_library_song_details(
            [
                item["id"]
                for item, track in parsed_tracks
                if self._track_has_weak_album_mapping(track)
            ]
        )
        for item, parsed_track in parsed_tracks:
            if (detail := details.get(item["id"])) is None:
                yield parsed_track
                continue
            yield self._apply_album_detail(
                item, parsed_track, detail, rating_response.get(item["id"])
            )

    async def _fetch_library_song_details(
        self, library_ids: list[str]
    ) -> dict[str, dict[str, Any]]:
        """Return the detailed library-song items for the given ids, keyed by id."""
        details: dict[str, dict[str, Any]] = {}
        for offset in range(0, len(library_ids), _DETAIL_BATCH_SIZE):
            batch = library_ids[offset : offset + _DETAIL_BATCH_SIZE]
            try:
                response = await self.api.get_data(
                    "me/library/songs", ids=",".join(batch), include="catalog,albums,artists"
                )
            except MusicAssistantError as err:
                # the listing parse stays usable, so a failed batch only costs album detail
                self.logger.warning(
                    "Unable to fetch library song details for %s tracks: %s", len(batch), err
                )
                continue
            details.update(
                {item["id"]: item for item in response.get("data", []) if item.get("id")}
            )
        return details
