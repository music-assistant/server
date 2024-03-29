"""Manage MediaItems of type Playlist."""

from __future__ import annotations

import asyncio
import random
import time
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
from music_assistant.common.models.media_items import (
    ItemMapping,
    Playlist,
    PlaylistTrack,
    Track,
)
from music_assistant.constants import DB_TABLE_PLAYLISTS
from music_assistant.server.helpers.compare import compare_strings

from .base import MediaControllerBase


class PlaylistController(MediaControllerBase[Playlist]):
    """Controller managing MediaItems of type Playlist."""

    db_table = DB_TABLE_PLAYLISTS
    media_type = MediaType.PLAYLIST
    item_cls = Playlist

    def __init__(self, *args, **kwargs) -> None:
        """Initialize class."""
        super().__init__(*args, **kwargs)
        self._db_add_lock = asyncio.Lock()
        # register api handlers
        self.mass.register_api_command("music/playlists/library_items", self.library_items)
        self.mass.register_api_command(
            "music/playlists/update_item_in_library", self.update_item_in_library
        )
        self.mass.register_api_command(
            "music/playlists/remove_item_from_library", self.remove_item_from_library
        )
        self.mass.register_api_command("music/playlists/create_playlist", self.create_playlist)

        self.mass.register_api_command("music/playlists/get_playlist", self.get)
        self.mass.register_api_command("music/playlists/playlist_tracks", self.tracks)
        self.mass.register_api_command(
            "music/playlists/add_playlist_tracks", self.add_playlist_tracks
        )
        self.mass.register_api_command(
            "music/playlists/remove_playlist_tracks", self.remove_playlist_tracks
        )

    async def add_item_to_library(self, item: Playlist, metadata_lookup: bool = True) -> Playlist:
        """Add playlist to library and return the new database item."""
        if isinstance(item, ItemMapping):
            metadata_lookup = False
            item = Playlist.from_item_mapping(item)
        if not isinstance(item, Playlist):
            msg = "Not a valid Playlist object (ItemMapping can not be added to db)"
            raise InvalidDataError(msg)
        if not item.provider_mappings:
            msg = "Playlist is missing provider mapping(s)"
            raise InvalidDataError(msg)
        # check for existing item first
        library_item = None
        if cur_item := await self.get_library_item_by_prov_id(item.item_id, item.provider):
            # existing item match by provider id
            library_item = await self.update_item_in_library(cur_item.item_id, item)
        elif cur_item := await self.get_library_item_by_external_ids(item.external_ids):
            # existing item match by external id
            library_item = await self.update_item_in_library(cur_item.item_id, item)
        else:
            # search by name
            async for db_item in self.iter_library_items(search=item.name):
                if compare_strings(db_item.name, item.name):
                    # existing item found: update it
                    library_item = await self.update_item_in_library(db_item.item_id, item)
                    break
        if not library_item:
            # actually add a new item in the library db
            # use the lock to prevent a race condition of the same item being added twice
            async with self._db_add_lock:
                library_item = await self._add_library_item(item)
        # preload playlist tracks listing (do not load them in the db)
        async for _ in self.tracks(item.item_id, item.provider):
            pass
        # metadata lookup we need to do after adding it to the db
        if metadata_lookup:
            await self.mass.metadata.get_playlist_metadata(library_item)
            library_item = await self.update_item_in_library(library_item.item_id, library_item)
        self.mass.signal_event(
            EventType.MEDIA_ITEM_ADDED,
            library_item.uri,
            library_item,
        )
        return library_item

    async def update_item_in_library(
        self, item_id: int, update: Playlist, overwrite: bool = False
    ) -> Playlist:
        """Update existing record in the database."""
        db_id = int(item_id)  # ensure integer
        cur_item = await self.get_library_item(db_id)
        metadata = cur_item.metadata.update(getattr(update, "metadata", None), overwrite)
        provider_mappings = self._get_provider_mappings(cur_item, update, overwrite)
        cur_item.external_ids.update(update.external_ids)
        await self.mass.music.database.update(
            self.db_table,
            {"item_id": db_id},
            {
                # always prefer name/owner from updated item here
                "name": update.name or cur_item.name,
                "sort_name": update.sort_name or cur_item.sort_name,
                "owner": update.owner or cur_item.sort_name,
                "is_editable": update.is_editable,
                "metadata": serialize_to_json(metadata),
                "provider_mappings": serialize_to_json(provider_mappings),
                "external_ids": serialize_to_json(
                    update.external_ids if overwrite else cur_item.external_ids
                ),
                "timestamp_modified": int(utc_timestamp()),
            },
        )
        # update/set provider_mappings table
        await self._set_provider_mappings(db_id, provider_mappings)
        self.logger.debug("updated %s in database: %s", update.name, db_id)
        # get full created object
        library_item = await self.get_library_item(db_id)
        self.mass.signal_event(
            EventType.MEDIA_ITEM_UPDATED,
            library_item.uri,
            library_item,
        )
        # return the full item we just updated
        return library_item

    async def tracks(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
        force_refresh: bool = False,
    ) -> AsyncGenerator[PlaylistTrack, None]:
        """Return playlist tracks for the given provider playlist id."""
        playlist = await self.get(
            item_id, provider_instance_id_or_domain, force_refresh=force_refresh
        )
        prov = next(x for x in playlist.provider_mappings)
        async for track in self._get_provider_playlist_tracks(
            prov.item_id,
            prov.provider_instance,
            cache_checksum=(str(time.time()) if force_refresh else playlist.metadata.checksum),
        ):
            yield track

    async def create_playlist(
        self, name: str, provider_instance_or_domain: str | None = None
    ) -> Playlist:
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
            msg = "No provider available which allows playlists creation."
            raise ProviderUnavailableError(msg)

        # create playlist on the provider
        playlist = await provider.create_playlist(name)
        # add the new playlist to the library
        return await self.add_item_to_library(playlist, True)

    async def add_playlist_tracks(self, db_playlist_id: str | int, uris: list[str]) -> None:
        """Add multiple tracks to playlist. Creates background tasks to process the action."""
        db_id = int(db_playlist_id)  # ensure integer
        playlist = await self.get_library_item(db_id)
        if not playlist:
            msg = f"Playlist with id {db_id} not found"
            raise MediaNotFoundError(msg)
        if not playlist.is_editable:
            msg = f"Playlist {playlist.name} is not editable"
            raise InvalidDataError(msg)
        for uri in uris:
            self.mass.create_task(self.add_playlist_track(db_id, uri))

    async def add_playlist_track(self, db_playlist_id: str | int, track_uri: str) -> None:
        """Add track to playlist - make sure we dont add duplicates."""
        db_id = int(db_playlist_id)  # ensure integer
        # we can only edit playlists that are in the database (marked as editable)
        playlist = await self.get_library_item(db_id)
        if not playlist:
            msg = f"Playlist with id {db_id} not found"
            raise MediaNotFoundError(msg)
        if not playlist.is_editable:
            msg = f"Playlist {playlist.name} is not editable"
            raise InvalidDataError(msg)
        # make sure we have recent full track details
        track = await self.mass.music.get_item_by_uri(track_uri)
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
                msg = "Track already exists in playlist {playlist.name}"
                raise InvalidDataError(msg)
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
            msg = f"Track is not available on provider {playlist_prov.provider_domain}"
            raise MediaNotFoundError(msg)
        # actually add the tracks to the playlist on the provider
        provider = self.mass.get_provider(playlist_prov.provider_instance)
        await provider.add_playlist_tracks(playlist_prov.item_id, [track_id_to_add])
        # invalidate cache by updating the checksum
        await self.get(db_id, "library", force_refresh=True)

    async def remove_playlist_tracks(
        self, db_playlist_id: str | int, positions_to_remove: tuple[int, ...]
    ) -> None:
        """Remove multiple tracks from playlist."""
        db_id = int(db_playlist_id)  # ensure integer
        playlist = await self.get_library_item(db_id)
        if not playlist:
            msg = f"Playlist with id {db_id} not found"
            raise MediaNotFoundError(msg)
        if not playlist.is_editable:
            msg = f"Playlist {playlist.name} is not editable"
            raise InvalidDataError(msg)
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
        await self.get(db_id, "library", force_refresh=True)

    async def _add_library_item(self, item: Playlist) -> Playlist:
        """Add a new record to the database."""
        item.timestamp_added = int(utc_timestamp())
        item.timestamp_modified = int(utc_timestamp())
        new_item = await self.mass.music.database.insert(
            self.db_table,
            {
                "name": item.name,
                "sort_name": item.sort_name,
                "owner": item.owner,
                "is_editable": item.is_editable,
                "favorite": item.favorite,
                "metadata": serialize_to_json(item.metadata),
                "provider_mappings": serialize_to_json(item.provider_mappings),
                "external_ids": serialize_to_json(item.external_ids),
                "timestamp_added": int(utc_timestamp()),
                "timestamp_modified": int(utc_timestamp()),
            },
        )
        db_id = new_item["item_id"]
        # update/set provider_mappings table
        await self._set_provider_mappings(db_id, item.provider_mappings)
        self.logger.debug("added %s to database", item.name)
        # return the full item we just added
        return await self.get_library_item(db_id)

    async def _get_provider_playlist_tracks(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
        cache_checksum: Any = None,
    ) -> AsyncGenerator[PlaylistTrack, None]:
        """Return album tracks for the given provider album id."""
        assert provider_instance_id_or_domain != "library"
        provider = self.mass.get_provider(provider_instance_id_or_domain)
        if not provider:
            return
        # prefer cache items (if any)
        cache_key = f"{provider.instance_id}.playlist.{item_id}.tracks"
        if cache := await self.mass.cache.get(cache_key, checksum=cache_checksum):
            for track_dict in cache:
                yield PlaylistTrack.from_dict(track_dict)
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
        assert provider_instance_id_or_domain != "library"
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
        self,
        media_item: Playlist,
        limit: int = 25,
    ) -> list[Track]:
        """Get dynamic list of tracks for given item, fallback/default implementation."""
        # TODO: query metadata provider(s) to get similar tracks (or tracks from similar artists)
        msg = "No Music Provider found that supports requesting similar tracks."
        raise UnsupportedFeaturedException(msg)
