"""Manage MediaItems of type Playlist."""
from __future__ import annotations

from time import time
from typing import List, Optional

from databases import Database as Db

from music_assistant.helpers.database import TABLE_PLAYLISTS
from music_assistant.helpers.json import json_serializer
from music_assistant.helpers.uri import create_uri
from music_assistant.models.enums import EventType, MediaType, ProviderType
from music_assistant.models.errors import InvalidDataError, MediaNotFoundError
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
        if provider == ProviderType.DATABASE or provider_id == "database":
            playlist = await self.get_db_item(item_id)
            prov = next(x for x in playlist.provider_ids)
            item_id = prov.item_id
            provider_id = prov.prov_id

        provider = self.mass.music.get_provider(provider_id or provider)
        if not provider:
            return []

        return await provider.get_playlist_tracks(item_id)

    async def add(self, item: Playlist) -> Playlist:
        """Add playlist to local db and return the new database item."""
        item.metadata.last_refresh = int(time())
        await self.mass.metadata.get_playlist_metadata(item)
        return await self.add_db_item(item)

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
        for item in await self.tracks(playlist_prov.item_id, playlist_prov.prov_id):
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
                track_prov.prov_id == playlist_prov.prov_id
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
            if track_version.prov_id == playlist_prov.prov_id:
                track_id_to_add = track_version.item_id
                break
        if not track_id_to_add:
            raise MediaNotFoundError(
                f"Track is not available on provider {playlist_prov.prov_type}"
            )
        # actually add the tracks to the playlist on the provider
        provider = self.mass.music.get_provider(playlist_prov.prov_id)
        await provider.add_playlist_tracks(playlist_prov.item_id, [track_id_to_add])
        # update local db entry
        self.mass.signal_event(
            MassEvent(
                type=EventType.MEDIA_ITEM_UPDATED,
                object_id=db_playlist_id,
                data=playlist,
            )
        )

    async def remove_playlist_tracks(
        self, db_playlist_id: str, positions: List[int]
    ) -> None:
        """Remove multiple tracks from playlist."""
        playlist = await self.get_db_item(db_playlist_id)
        if not playlist:
            raise MediaNotFoundError(f"Playlist with id {db_playlist_id} not found")
        if not playlist.is_editable:
            raise InvalidDataError(f"Playlist {playlist.name} is not editable")
        for prov in playlist.provider_ids:
            track_ids_to_remove = []
            for playlist_track in await self.tracks(prov.item_id, prov.prov_id):
                if playlist_track.position not in positions:
                    continue
                track_ids_to_remove.append(playlist_track.item_id)
            # actually remove the tracks from the playlist on the provider
            # TODO: send positions to provider to delete
            if track_ids_to_remove:
                provider = self.mass.music.get_provider(prov.prov_id)
                await provider.remove_playlist_tracks(prov.item_id, track_ids_to_remove)
        self.mass.signal_event(
            MassEvent(
                type=EventType.MEDIA_ITEM_UPDATED,
                object_id=db_playlist_id,
                data=playlist,
            )
        )

    async def add_db_item(
        self, playlist: Playlist, db: Optional[Db] = None
    ) -> Playlist:
        """Add a new playlist record to the database."""
        async with self.mass.database.get_db(db) as db:
            match = {"name": playlist.name, "owner": playlist.owner}
            if cur_item := await self.mass.database.get_row(
                self.db_table, match, db=db
            ):
                # update existing
                return await self.update_db_item(cur_item["item_id"], playlist, db=db)

            # insert new playlist
            new_item = await self.mass.database.insert(
                self.db_table, playlist.to_db_row(), db=db
            )
            item_id = new_item["item_id"]
            self.logger.debug("added %s to database", playlist.name)
            # return created object
            db_item = await self.get_db_item(item_id, db=db)
            self.mass.signal_event(
                MassEvent(
                    EventType.MEDIA_ITEM_ADDED, object_id=db_item.uri, data=db_item
                )
            )
            return db_item

    async def update_db_item(
        self,
        item_id: int,
        playlist: Playlist,
        overwrite: bool = False,
        db: Optional[Db] = None,
    ) -> Playlist:
        """Update Playlist record in the database."""
        async with self.mass.database.get_db(db) as db:

            cur_item = await self.get_db_item(item_id, db=db)
            if overwrite:
                metadata = playlist.metadata
                provider_ids = playlist.provider_ids
            else:
                metadata = cur_item.metadata.update(playlist.metadata)
                provider_ids = {*cur_item.provider_ids, *playlist.provider_ids}

            await self.mass.database.update(
                self.db_table,
                {"item_id": item_id},
                {
                    "name": playlist.name,
                    "sort_name": playlist.sort_name,
                    "owner": playlist.owner,
                    "is_editable": playlist.is_editable,
                    "metadata": json_serializer(metadata),
                    "provider_ids": json_serializer(provider_ids),
                },
                db=db,
            )
            self.logger.debug("updated %s in database: %s", playlist.name, item_id)
            db_item = await self.get_db_item(item_id, db=db)
            self.mass.signal_event(
                MassEvent(
                    EventType.MEDIA_ITEM_UPDATED, object_id=db_item.uri, data=db_item
                )
            )
            return db_item
