"""Manage MediaItems of type Playlist."""
from __future__ import annotations

from ctypes import Union
from time import time
from typing import Any, List, Optional, Tuple

from music_assistant.helpers.database import TABLE_PLAYLISTS
from music_assistant.helpers.json import json_serializer
from music_assistant.helpers.uri import create_uri
from music_assistant.models.enums import (
    EventType,
    MediaType,
    MusicProviderFeature,
    ProviderType,
)
from music_assistant.models.errors import (
    InvalidDataError,
    MediaNotFoundError,
    ProviderUnavailableError,
)
from music_assistant.models.event import MassEvent
from music_assistant.models.media_controller import MediaControllerBase
from music_assistant.models.media_items import Playlist, Track


class PlaylistController(MediaControllerBase[Playlist]):
    """Controller managing MediaItems of type Playlist."""

    db_table = TABLE_PLAYLISTS
    media_type = MediaType.PLAYLIST
    item_cls = Playlist

    async def get_playlist_by_name(self, name: str) -> Playlist | None:
        """Get in-library playlist by name."""
        return await self.mass.database.get_row(self.db_table, {"name": name})

    async def tracks(
        self,
        item_id: str,
        provider: Optional[ProviderType] = None,
        provider_id: Optional[str] = None,
    ) -> List[Track]:
        """Return playlist tracks for the given provider playlist id."""
        playlist = await self.get(item_id, provider, provider_id)
        prov = next(x for x in playlist.provider_ids)
        return await self.get_provider_playlist_tracks(
            prov.item_id,
            provider=prov.prov_type,
            provider_id=prov.prov_id,
            cache_checksum=playlist.metadata.checksum,
        )

    async def get_provider_playlist_tracks(
        self,
        item_id: str,
        provider: Optional[ProviderType] = None,
        provider_id: Optional[str] = None,
        cache_checksum: Any = None,
    ) -> List[Track]:
        """Return album tracks for the given provider album id."""
        prov = self.mass.music.get_provider(provider_id or provider)
        if not prov:
            return []
        # prefer cache items (if any)
        cache_key = f"{prov.id}.playlist.{item_id}.tracks"
        if cache := await self.mass.cache.get(cache_key, checksum=cache_checksum):
            return [Track.from_dict(x) for x in cache]
        # no items in cache - get listing from provider
        items = await prov.get_playlist_tracks(item_id)
        # double check if position set
        if items:
            assert (
                items[0].position is not None
            ), "Playlist items require position to be set"
        # store (serializable items) in cache
        self.mass.create_task(
            self.mass.cache.set(
                cache_key, [x.to_dict() for x in items], checksum=cache_checksum
            )
        )
        return items

    async def add(self, item: Playlist, overwrite_existing: bool = False) -> Playlist:
        """Add playlist to local db and return the new database item."""
        item.metadata.last_refresh = int(time())
        await self.mass.metadata.get_playlist_metadata(item)
        return await self.add_db_item(item, overwrite_existing)

    async def create(
        self, name: str, prov_id: Union[ProviderType, str, None] = None
    ) -> Playlist:
        """Create new playlist."""
        # if prov_id is omitted, prefer file
        if prov_id:
            provider = self.mass.music.get_provider(prov_id)
        else:
            try:
                provider = self.mass.music.get_provider(ProviderType.FILESYSTEM_LOCAL)
            except ProviderUnavailableError:
                provider = next(
                    (
                        x
                        for x in self.mass.music.providers
                        if MusicProviderFeature.PLAYLIST_CREATE in x.supported_features
                    ),
                    None,
                )
            if provider is None:
                raise ProviderUnavailableError(
                    "No provider available which allows playlists creation."
                )

        return await provider.create_playlist(name)

    async def add_playlist_tracks(self, db_playlist_id: str, uris: List[str]) -> None:
        """Add multiple tracks to playlist. Creates background tasks to process the action."""
        playlist = await self.get_db_item(db_playlist_id)
        if not playlist:
            raise MediaNotFoundError(f"Playlist with id {db_playlist_id} not found")
        if not playlist.is_editable:
            raise InvalidDataError(f"Playlist {playlist.name} is not editable")
        for uri in uris:
            job_desc = f"Add track {uri} to playlist {playlist.name}"
            self.mass.add_job(self.add_playlist_track(db_playlist_id, uri), job_desc)

    async def add_playlist_track(self, db_playlist_id: str, track_uri: str) -> None:
        """Add track to playlist - make sure we dont add duplicates."""
        # we can only edit playlists that are in the database (marked as editable)
        playlist = await self.get_db_item(db_playlist_id)
        if not playlist:
            raise MediaNotFoundError(f"Playlist with id {db_playlist_id} not found")
        if not playlist.is_editable:
            raise InvalidDataError(f"Playlist {playlist.name} is not editable")
        # make sure we have recent full track details
        track = await self.mass.music.get_item_by_uri(track_uri, lazy=False)
        assert track.media_type == MediaType.TRACK
        # a playlist can only have one provider (for now)
        playlist_prov = next(iter(playlist.provider_ids))
        # grab all existing track ids in the playlist so we can check for duplicates
        cur_playlist_track_ids = set()
        count = 0
        for item in await self.tracks(playlist_prov.item_id, playlist_prov.prov_type):
            count += 1
            cur_playlist_track_ids.update(
                {
                    i.item_id
                    for i in item.provider_ids
                    if i.prov_id == playlist_prov.prov_id
                }
            )
        # check for duplicates
        for track_prov in track.provider_ids:
            if (
                track_prov.prov_type == playlist_prov.prov_type
                and track_prov.item_id in cur_playlist_track_ids
            ):
                raise InvalidDataError(
                    "Track already exists in playlist {playlist.name}"
                )
        # add track to playlist
        # we can only add a track to a provider playlist if track is available on that provider
        # a track can contain multiple versions on the same provider
        # simply sort by quality and just add the first one (assuming track is still available)
        track_id_to_add = None
        for track_version in sorted(
            track.provider_ids, key=lambda x: x.quality, reverse=True
        ):
            if not track.available:
                continue
            if playlist_prov.prov_type.is_file():
                # the file provider can handle uri's from all providers so simply add the uri
                track_id_to_add = track_version.url or create_uri(
                    MediaType.TRACK,
                    track_version.prov_type,
                    track_version.item_id,
                )
                break
            if track_version.prov_type == playlist_prov.prov_type:
                track_id_to_add = track_version.item_id
                break
        if not track_id_to_add:
            raise MediaNotFoundError(
                f"Track is not available on provider {playlist_prov.prov_type}"
            )
        # actually add the tracks to the playlist on the provider
        provider = self.mass.music.get_provider(playlist_prov.prov_id)
        await provider.add_playlist_tracks(playlist_prov.item_id, [track_id_to_add])
        # invalidate cache by updating the checksum
        await self.get(
            db_playlist_id, provider=ProviderType.DATABASE, force_refresh=True
        )

    async def remove_playlist_tracks(
        self, db_playlist_id: str, positions_to_remove: Tuple[int]
    ) -> None:
        """Remove multiple tracks from playlist."""
        playlist = await self.get_db_item(db_playlist_id)
        if not playlist:
            raise MediaNotFoundError(f"Playlist with id {db_playlist_id} not found")
        if not playlist.is_editable:
            raise InvalidDataError(f"Playlist {playlist.name} is not editable")
        for prov in playlist.provider_ids:
            provider = self.mass.music.get_provider(prov.prov_id)
            if (
                MusicProviderFeature.PLAYLIST_TRACKS_EDIT
                not in provider.supported_features
            ):
                self.logger.warning(
                    "Provider %s does not support editing playlists",
                    prov.prov_type.value,
                )
                continue
            await provider.remove_playlist_tracks(prov.item_id, positions_to_remove)
        # invalidate cache by updating the checksum
        await self.get(
            db_playlist_id, provider=ProviderType.DATABASE, force_refresh=True
        )

    async def add_db_item(
        self, item: Playlist, overwrite_existing: bool = False
    ) -> Playlist:
        """Add a new record to the database."""
        async with self._db_add_lock:
            match = {"name": item.name, "owner": item.owner}
            if cur_item := await self.mass.database.get_row(self.db_table, match):
                # update existing
                return await self.update_db_item(
                    cur_item["item_id"], item, overwrite=overwrite_existing
                )

            # insert new item
            new_item = await self.mass.database.insert(self.db_table, item.to_db_row())
            item_id = new_item["item_id"]
            self.logger.debug("added %s to database", item.name)
            # return created object
            db_item = await self.get_db_item(item_id)
            self.mass.signal_event(
                MassEvent(EventType.MEDIA_ITEM_ADDED, db_item.uri, db_item)
            )
            return db_item

    async def update_db_item(
        self,
        item_id: int,
        item: Playlist,
        overwrite: bool = False,
    ) -> Playlist:
        """Update Playlist record in the database."""
        cur_item = await self.get_db_item(item_id)
        if overwrite:
            metadata = item.metadata
            provider_ids = item.provider_ids
        else:
            metadata = cur_item.metadata.update(item.metadata)
            provider_ids = {*cur_item.provider_ids, *item.provider_ids}

        await self.mass.database.update(
            self.db_table,
            {"item_id": item_id},
            {
                "name": item.name,
                "sort_name": item.sort_name,
                "owner": item.owner,
                "is_editable": item.is_editable,
                "metadata": json_serializer(metadata),
                "provider_ids": json_serializer(provider_ids),
            },
        )
        self.logger.debug("updated %s in database: %s", item.name, item_id)
        db_item = await self.get_db_item(item_id)
        self.mass.signal_event(
            MassEvent(EventType.MEDIA_ITEM_UPDATED, db_item.uri, db_item)
        )
        return db_item
