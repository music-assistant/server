"""Manage MediaItems of type Playlist."""
from __future__ import annotations

from time import time
from typing import List

from music_assistant.constants import EventType, MassEvent
from music_assistant.helpers.database import TABLE_PLAYLISTS
from music_assistant.helpers.json import json_serializer
from music_assistant.helpers.util import create_sort_name
from music_assistant.models.errors import InvalidDataError, MediaNotFoundError
from music_assistant.models.media_controller import MediaControllerBase
from music_assistant.models.media_items import MediaType, Playlist, Track


class PlaylistController(MediaControllerBase[Playlist]):
    """Controller managing MediaItems of type Playlist."""

    db_table = TABLE_PLAYLISTS
    media_type = MediaType.PLAYLIST
    item_cls = Playlist

    async def get_playlist_by_name(self, name: str) -> Playlist | None:
        """Get in-library playlist by name."""
        return await self.mass.database.get_row(self.db_table, {"name": name})

    async def tracks(self, item_id: str, provider_id: str) -> List[Track]:
        """Return playlist tracks for the given provider playlist id."""
        playlist = await self.get(item_id, provider_id)
        # simply return the tracks from the first provider
        for prov in playlist.provider_ids:
            if tracks := await self.get_provider_playlist_tracks(
                prov.item_id, prov.provider
            ):
                return tracks
        return []

    async def add(self, item: Playlist) -> Playlist:
        """Add playlist to local db and return the new database item."""
        item.metadata.last_refresh = int(time())
        await self.mass.metadata.get_playlist_metadata(item)
        db_item = await self.add_db_item(item)
        self.mass.signal_event(
            MassEvent(EventType.PLAYLIST_ADDED, object_id=db_item.uri, data=db_item)
        )
        return db_item

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
        for item in await self.tracks(playlist_prov.item_id, playlist_prov.provider):
            count += 1
            cur_playlist_track_ids.update(
                {
                    i.item_id
                    for i in item.provider_ids
                    if i.provider == playlist_prov.provider
                }
            )
        # check for duplicates
        for track_prov in track.provider_ids:
            if (
                track_prov.provider == playlist_prov.provider
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
            if track_version.provider == playlist_prov.provider:
                track_id_to_add = track_version.item_id
                break
            if playlist_prov.provider == "file":
                # the file provider can handle uri's from all providers so simply add the uri
                track_id_to_add = track.uri
                break
        if not track_id_to_add:
            raise MediaNotFoundError(
                "Track is not available on provider {playlist_prov.provider}"
            )
        # actually add the tracks to the playlist on the provider
        provider = self.mass.music.get_provider(playlist_prov.provider)
        await provider.add_playlist_tracks(playlist_prov.item_id, [track_id_to_add])
        # update local db entry
        self.mass.signal_event(
            MassEvent(
                type=EventType.PLAYLIST_UPDATED, object_id=db_playlist_id, data=playlist
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
            for playlist_track in await self.get_provider_playlist_tracks(
                prov.item_id, prov.provider
            ):
                if playlist_track.position not in positions:
                    continue
                track_ids_to_remove.append(playlist_track.item_id)
            # actually remove the tracks from the playlist on the provider
            if track_ids_to_remove:
                provider = self.mass.music.get_provider(prov.provider)
                await provider.remove_playlist_tracks(prov.item_id, track_ids_to_remove)
        self.mass.signal_event(
            MassEvent(
                type=EventType.PLAYLIST_UPDATED, object_id=db_playlist_id, data=playlist
            )
        )

    async def get_provider_playlist_tracks(
        self, item_id: str, provider_id: str
    ) -> List[Track]:
        """Return playlist tracks for the given provider playlist id."""
        provider = self.mass.music.get_provider(provider_id)
        if not provider:
            return []

        # we need to make sure that position is set on the track
        def playlist_track_with_position(track: Track, index: int):
            if track.position is None:
                track.position = index
            return track

        tracks = await provider.get_playlist_tracks(item_id)
        return [
            playlist_track_with_position(track, index)
            for index, track in enumerate(tracks)
        ]

    async def add_db_item(self, playlist: Playlist) -> Playlist:
        """Add a new playlist record to the database."""
        async with self.mass.database.get_db() as _db:
            match = {"name": playlist.name, "owner": playlist.owner}
            if cur_item := await self.mass.database.get_row(
                self.db_table, match, db=_db
            ):
                # update existing
                return await self.update_db_item(cur_item["item_id"], playlist)

            # insert new playlist
            new_item = await self.mass.database.insert_or_replace(
                self.db_table, playlist.to_db_row(), db=_db
            )
            item_id = new_item["item_id"]
            # store provider mappings
            await self.mass.music.set_provider_mappings(
                item_id, MediaType.PLAYLIST, playlist.provider_ids, db=_db
            )
            self.logger.debug("added %s to database", playlist.name)
            # return created object
            return await self.get_db_item(item_id, db=_db)

    async def update_db_item(
        self, item_id: int, playlist: Playlist, overwrite: bool = False
    ) -> Playlist:
        """Update Playlist record in the database."""
        cur_item = await self.get_db_item(item_id)
        if overwrite:
            metadata = playlist.metadata
            provider_ids = playlist.provider_ids
        else:
            metadata = cur_item.metadata.update(playlist.metadata)
            provider_ids = {*cur_item.provider_ids, *playlist.provider_ids}
        if not playlist.sort_name:
            playlist.sort_name = create_sort_name(playlist.name)

        async with self.mass.database.get_db() as _db:
            await self.mass.database.update(
                self.db_table,
                {"item_id": item_id},
                {
                    "name": playlist.name,
                    "sort_name": playlist.sort_name,
                    "owner": playlist.owner,
                    "is_editable": playlist.is_editable,
                    "checksum": playlist.checksum,
                    "metadata": json_serializer(metadata),
                    "provider_ids": json_serializer(provider_ids),
                },
                db=_db,
            )
            await self.mass.music.set_provider_mappings(
                item_id, MediaType.PLAYLIST, provider_ids, db=_db
            )
            self.logger.debug("updated %s in database: %s", playlist.name, item_id)
            db_item = await self.get_db_item(item_id, db=_db)
            self.mass.signal_event(
                MassEvent(
                    type=EventType.PLAYLIST_UPDATED, object_id=item_id, data=playlist
                )
            )
            return db_item
