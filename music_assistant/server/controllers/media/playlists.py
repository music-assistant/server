"""Manage MediaItems of type Playlist."""
from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncGenerator
from typing import Any

from music_assistant.common.helpers.datetime import utc_timestamp
from music_assistant.common.helpers.json import serialize_to_json
from music_assistant.common.helpers.uri import create_uri
from music_assistant.common.models.enums import EventType, MediaType, ProviderFeature
from music_assistant.common.models.errors import (
    InvalidDataError,
    MediaNotFoundError,
    ProviderUnavailableError,
    UnsupportedFeaturedException,
)
from music_assistant.common.models.media_items import Playlist, Track
from music_assistant.constants import DB_TABLE_PLAYLISTS

from .base import MediaControllerBase


class PlaylistController(MediaControllerBase[Playlist]):
    """Controller managing MediaItems of type Playlist."""

    db_table = DB_TABLE_PLAYLISTS
    media_type = MediaType.PLAYLIST
    item_cls = Playlist
    _db_add_lock = asyncio.Lock()

    def __init__(self, *args, **kwargs):
        """Initialize class."""
        super().__init__(*args, **kwargs)
        # register api handlers
        self.mass.register_api_command("music/playlists", self.db_items)
        self.mass.register_api_command("music/playlist", self.get)
        self.mass.register_api_command("music/playlist/tracks", self.tracks)
        self.mass.register_api_command("music/playlist/tracks/add", self.add_playlist_tracks)
        self.mass.register_api_command("music/playlist/tracks/remove", self.remove_playlist_tracks)
        self.mass.register_api_command("music/playlist/update", self._update_db_item)
        self.mass.register_api_command("music/playlist/delete", self.delete)
        self.mass.register_api_command("music/playlist/create", self.create)

    async def add(self, item: Playlist, skip_metadata_lookup: bool = False) -> Playlist:
        """Add playlist to local db and return the new database item."""
        if item.provider == "database":
            db_item = await self._update_db_item(item.item_id, item)
        else:
            # use the lock to prevent a race condition of the same item being added twice
            async with self._db_add_lock:
                db_item = await self._add_db_item(item)
        # preload playlist tracks listing (do not load them in the db)
        async for _ in self.tracks(item.item_id, item.provider):
            pass
        # metadata lookup we need to do after adding it to the db
        if not skip_metadata_lookup:
            await self.mass.metadata.get_playlist_metadata(db_item)
            db_item = await self._update_db_item(db_item.item_id, db_item)
        return db_item

    async def update(self, item_id: int, update: Playlist, overwrite: bool = False) -> Playlist:
        """Update existing record in the database."""
        return await self._update_db_item(item_id=item_id, item=update, overwrite=overwrite)

    async def tracks(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
    ) -> AsyncGenerator[Track, None]:
        """Return playlist tracks for the given provider playlist id."""
        playlist = await self.get(item_id, provider_instance_id_or_domain)
        prov = next(x for x in playlist.provider_mappings)
        async for track in self._get_provider_playlist_tracks(
            prov.item_id,
            prov.provider_instance,
            cache_checksum=playlist.metadata.checksum,
        ):
            yield track

    async def create(self, name: str, provider_instance_or_domain: str | None = None) -> Playlist:
        """Create new playlist."""
        # if provider is omitted, just pick first provider
        if provider_instance_or_domain:
            provider = self.mass.get_provider(provider_instance_or_domain)
        else:
            provider = next(
                (
                    x
                    for x in self.mass.music.providers
                    if ProviderFeature.PLAYLIST_CREATE in x.supported_features
                ),
                None,
            )
        if provider is None:
            raise ProviderUnavailableError("No provider available which allows playlists creation.")

        # create playlist on the provider
        prov_playlist = await provider.create_playlist(name)
        prov_playlist.in_library = True
        # return db playlist
        return await self.add(prov_playlist, True)

    async def add_playlist_tracks(self, db_playlist_id: str | int, uris: list[str]) -> None:
        """Add multiple tracks to playlist. Creates background tasks to process the action."""
        db_id = int(db_playlist_id)  # ensure integer
        playlist = await self.get_db_item(db_id)
        if not playlist:
            raise MediaNotFoundError(f"Playlist with id {db_id} not found")
        if not playlist.is_editable:
            raise InvalidDataError(f"Playlist {playlist.name} is not editable")
        for uri in uris:
            self.mass.create_task(self.add_playlist_track(db_id, uri))

    async def add_playlist_track(self, db_playlist_id: str | int, track_uri: str) -> None:
        """Add track to playlist - make sure we dont add duplicates."""
        db_id = int(db_playlist_id)  # ensure integer
        # we can only edit playlists that are in the database (marked as editable)
        playlist = await self.get_db_item(db_id)
        if not playlist:
            raise MediaNotFoundError(f"Playlist with id {db_id} not found")
        if not playlist.is_editable:
            raise InvalidDataError(f"Playlist {playlist.name} is not editable")
        # make sure we have recent full track details
        track = await self.mass.music.get_item_by_uri(track_uri, lazy=False)
        assert track.media_type == MediaType.TRACK
        # a playlist can only have one provider (for now)
        playlist_prov = next(iter(playlist.provider_mappings))
        # grab all existing track ids in the playlist so we can check for duplicates
        cur_playlist_track_ids = set()
        count = 0
        async for item in self.tracks(playlist_prov.item_id, playlist_prov.provider_instance):
            count += 1
            cur_playlist_track_ids.update(
                {
                    i.item_id
                    for i in item.provider_mappings
                    if i.provider_instance == playlist_prov.provider_instance
                }
            )
        # check for duplicates
        for track_prov in track.provider_mappings:
            if (
                track_prov.provider_domain == playlist_prov.provider_domain
                and track_prov.item_id in cur_playlist_track_ids
            ):
                raise InvalidDataError("Track already exists in playlist {playlist.name}")
        # add track to playlist
        # we can only add a track to a provider playlist if track is available on that provider
        # a track can contain multiple versions on the same provider
        # simply sort by quality and just add the first one (assuming track is still available)
        track_id_to_add = None
        for track_version in sorted(track.provider_mappings, key=lambda x: x.quality, reverse=True):
            if not track.available:
                continue
            if playlist_prov.provider_domain.startswith("filesystem"):
                # the file provider can handle uri's from all providers so simply add the uri
                track_id_to_add = track_version.url or create_uri(
                    MediaType.TRACK,
                    track_version.provider_instance,
                    track_version.item_id,
                )
                break
            if track_version.provider_domain == playlist_prov.provider_domain:
                track_id_to_add = track_version.item_id
                break
        if not track_id_to_add:
            raise MediaNotFoundError(
                f"Track is not available on provider {playlist_prov.provider_domain}"
            )
        # actually add the tracks to the playlist on the provider
        provider = self.mass.get_provider(playlist_prov.provider_instance)
        await provider.add_playlist_tracks(playlist_prov.item_id, [track_id_to_add])
        # invalidate cache by updating the checksum
        await self.get(db_id, "database", force_refresh=True)

    async def remove_playlist_tracks(
        self, db_playlist_id: str | int, positions_to_remove: tuple[int, ...]
    ) -> None:
        """Remove multiple tracks from playlist."""
        db_id = int(db_playlist_id)  # ensure integer
        playlist = await self.get_db_item(db_id)
        if not playlist:
            raise MediaNotFoundError(f"Playlist with id {db_id} not found")
        if not playlist.is_editable:
            raise InvalidDataError(f"Playlist {playlist.name} is not editable")
        for prov_mapping in playlist.provider_mappings:
            provider = self.mass.get_provider(prov_mapping.provider_instance)
            if ProviderFeature.PLAYLIST_TRACKS_EDIT not in provider.supported_features:
                self.logger.warning(
                    "Provider %s does not support editing playlists",
                    prov_mapping.provider_domain,
                )
                continue
            await provider.remove_playlist_tracks(prov_mapping.item_id, positions_to_remove)
        # invalidate cache by updating the checksum
        await self.get(db_id, "database", force_refresh=True)

    async def _add_db_item(self, item: Playlist) -> Playlist:
        """Add a new record to the database."""
        assert item.provider_mappings, "Item is missing provider mapping(s)"
        # safety guard: check for existing item first
        if cur_item := await self.get_db_item_by_prov_mappings(item.provider_mappings):
            # existing item found: update it
            return await self._update_db_item(cur_item.item_id, item)
        # try name matching
        match = {"name": item.name, "owner": item.owner}
        if db_row := await self.mass.music.database.get_row(self.db_table, match):
            cur_item = Playlist.from_db_row(db_row)
            # existing item found: update it
            return await self._update_db_item(cur_item.item_id, item)
        # insert new item
        item.timestamp_added = int(utc_timestamp())
        item.timestamp_modified = int(utc_timestamp())
        new_item = await self.mass.music.database.insert(self.db_table, item.to_db_row())
        db_id = new_item["item_id"]
        # update/set provider_mappings table
        await self._set_provider_mappings(db_id, item.provider_mappings)
        self.logger.debug("added %s to database", item.name)
        # get full created object
        db_item = await self.get_db_item(db_id)
        # only signal event if we're not running a sync (to prevent a floodstorm of events)
        if not self.mass.music.get_running_sync_tasks():
            self.mass.signal_event(
                EventType.MEDIA_ITEM_ADDED,
                db_item.uri,
                db_item,
            )
        # return the full item we just added
        return db_item

    async def _update_db_item(
        self, item_id: str | int, item: Playlist, overwrite: bool = False
    ) -> Playlist:
        """Update Playlist record in the database."""
        db_id = int(item_id)  # ensure integer
        cur_item = await self.get_db_item(db_id)
        metadata = cur_item.metadata.update(getattr(item, "metadata", None), overwrite)
        provider_mappings = self._get_provider_mappings(cur_item, item, overwrite)
        await self.mass.music.database.update(
            self.db_table,
            {"item_id": db_id},
            {
                # always prefer name/owner from updated item here
                "name": item.name or cur_item.name,
                "sort_name": item.sort_name or cur_item.sort_name,
                "owner": item.owner or cur_item.sort_name,
                "is_editable": item.is_editable,
                "metadata": serialize_to_json(metadata),
                "provider_mappings": serialize_to_json(provider_mappings),
                "timestamp_modified": int(utc_timestamp()),
            },
        )
        # update/set provider_mappings table
        await self._set_provider_mappings(db_id, provider_mappings)
        self.logger.debug("updated %s in database: %s", item.name, db_id)
        # get full created object
        db_item = await self.get_db_item(db_id)
        # only signal event if we're not running a sync (to prevent a floodstorm of events)
        if not self.mass.music.get_running_sync_tasks():
            self.mass.signal_event(
                EventType.MEDIA_ITEM_UPDATED,
                db_item.uri,
                db_item,
            )
        # return the full item we just updated
        return db_item

    async def _get_provider_playlist_tracks(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
        cache_checksum: Any = None,
    ) -> AsyncGenerator[Track, None]:
        """Return album tracks for the given provider album id."""
        assert provider_instance_id_or_domain != "database"
        provider = self.mass.get_provider(provider_instance_id_or_domain)
        if not provider:
            return
        # prefer cache items (if any)
        cache_key = f"{provider.instance_id}.playlist.{item_id}.tracks"
        if cache := await self.mass.cache.get(cache_key, checksum=cache_checksum):
            for track_dict in cache:
                yield Track.from_dict(track_dict)
            return
        # no items in cache - get listing from provider
        all_items = []
        async for item in provider.get_playlist_tracks(item_id):
            # double check if position set
            assert item.position is not None, "Playlist items require position to be set"
            yield item
            all_items.append(item)
        # store (serializable items) in cache
        self.mass.create_task(
            self.mass.cache.set(
                cache_key, [x.to_dict() for x in all_items], checksum=cache_checksum
            )
        )

    async def _get_provider_dynamic_tracks(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
        limit: int = 25,
    ):
        """Generate a dynamic list of tracks based on the playlist content."""
        assert provider_instance_id_or_domain != "database"
        provider = self.mass.get_provider(provider_instance_id_or_domain)
        if not provider or ProviderFeature.SIMILAR_TRACKS not in provider.supported_features:
            return []
        playlist_tracks = [
            x
            async for x in self._get_provider_playlist_tracks(
                item_id, provider_instance_id_or_domain
            )
            # filter out unavailable tracks
            if x.available
        ]
        limit = min(limit, len(playlist_tracks))
        # use set to prevent duplicates
        final_items = []
        # to account for playlists with mixed content we grab suggestions from a few
        # random playlist tracks to prevent getting too many tracks of one of the
        # source playlist's genres.
        while len(final_items) < limit:
            # grab 5 random tracks from the playlist
            base_tracks = random.sample(playlist_tracks, 5)
            # add the source/base playlist tracks to the final list...
            final_items.extend(base_tracks)
            # get 5 suggestions for one of the base tracks
            base_track = next(x for x in base_tracks if x.available)
            similar_tracks = await provider.get_similar_tracks(
                prov_track_id=base_track.item_id, limit=5
            )
            final_items.extend(x for x in similar_tracks if x.available)
        # Remove duplicate tracks
        radio_items = {track.sort_name: track for track in final_items}.values()
        # NOTE: In theory we can return a few more items than limit here
        # Shuffle the final items list
        return random.sample(list(radio_items), len(radio_items))

    async def _get_dynamic_tracks(
        self, media_item: Playlist, limit: int = 25  # noqa: ARG002
    ) -> list[Track]:
        """Get dynamic list of tracks for given item, fallback/default implementation."""
        # TODO: query metadata provider(s) to get similar tracks (or tracks from similar artists)
        raise UnsupportedFeaturedException(
            "No Music Provider found that supports requesting similar tracks."
        )
