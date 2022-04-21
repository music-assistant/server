"""Manage MediaItems of type Playlist."""
from __future__ import annotations

from typing import List, Optional

from music_assistant.constants import EventType, MassEvent
from music_assistant.helpers.cache import cached
from music_assistant.helpers.json import json_serializer
from music_assistant.helpers.util import create_sort_name, merge_dict, merge_list
from music_assistant.models.errors import InvalidDataError, MediaNotFoundError
from music_assistant.models.media_controller import MediaControllerBase
from music_assistant.models.media_items import MediaType, Playlist, Track


class PlaylistController(MediaControllerBase[Playlist]):
    """Controller managing MediaItems of type Playlist."""

    db_table = "playlists"
    media_type = MediaType.PLAYLIST
    item_cls = Playlist

    async def setup(self):
        """Async initialize of module."""
        # prepare database
        async with self.mass.database.get_db() as _db:
            await _db.execute(
                f"""CREATE TABLE IF NOT EXISTS {self.db_table}(
                        item_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL,
                        sort_name TEXT NOT NULL,
                        owner TEXT NOT NULL,
                        is_editable BOOLEAN NOT NULL,
                        checksum TEXT NOT NULL,
                        in_library BOOLEAN DEFAULT 0,
                        metadata json,
                        provider_ids json,
                        UNIQUE(name, owner)
                    );"""
            )
            await _db.execute(
                """CREATE TABLE IF NOT EXISTS playlist_tracks(
                        playlist_id INTEGER NOT NULL,
                        track_id INTEGER NOT NULL,
                        position INTEGER NOT NULL,
                        UNIQUE(playlist_id, position)
                    );"""
            )

    async def get_playlist_by_name(self, name: str) -> Playlist | None:
        """Get in-library playlist by name."""
        return await self.mass.database.get_row(self.db_table, {"name": name})

    async def tracks(self, item_id: str, provider_id: str) -> List[Track]:
        """Return playlist tracks for the given provider playlist id."""
        playlist = await self.get(item_id, provider_id)
        if playlist.in_library and playlist.provider == "database":
            # for in-library playlists we have the tracks in db
            return await self.get_db_playlist_tracks(playlist.item_id)
        # else: simply return the tracks from the first provider
        for prov in playlist.provider_ids:
            if tracks := await self.get_provider_playlist_tracks(
                prov.item_id, prov.provider
            ):
                return tracks
        return []

    async def add(self, item: Playlist) -> Playlist:
        """Add playlist to local db and return the new database item."""
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
        await self.add_db_playlist_track(db_playlist_id, track.item_id, count + 1)
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
        # update db
        for pos in positions:
            await self.remove_db_playlist_track(db_playlist_id, position=pos)
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
        playlist = await provider.get_playlist(item_id)
        cache_key = f"{provider_id}.playlisttracks.{item_id}"

        # we need to make sure that position is set on the track
        def playlist_track_with_position(track: Track, index: int):
            if track.position is None:
                track.position = index
            return track

        tracks = await cached(
            self.mass.cache,
            cache_key,
            provider.get_playlist_tracks,
            item_id,
            checksum=playlist.checksum,
        )

        return [
            playlist_track_with_position(track, index)
            for index, track in enumerate(tracks)
        ]

    async def add_db_item(self, playlist: Playlist) -> Playlist:
        """Add a new playlist record to the database."""
        match = {"name": playlist.name, "owner": playlist.owner}
        if cur_item := await self.mass.database.get_row(self.db_table, match):
            # update existing
            return await self.update_db_playlist(cur_item["item_id"], playlist)

        # insert new playlist
        new_item = await self.mass.database.insert_or_replace(
            self.db_table,
            playlist.to_db_row(),
        )
        item_id = new_item["item_id"]
        # store provider mappings
        await self.mass.music.add_provider_mappings(
            item_id, MediaType.PLAYLIST, playlist.provider_ids
        )
        self.logger.debug("added %s to database", playlist.name)
        # return created object
        return await self.get_db_item(item_id)

    async def update_db_playlist(self, item_id: int, playlist: Playlist) -> Playlist:
        """Update Playlist record in the database."""
        cur_item = await self.get_db_item(item_id)
        metadata = merge_dict(cur_item.metadata, playlist.metadata)
        provider_ids = merge_list(cur_item.provider_ids, playlist.provider_ids)
        if not playlist.sort_name:
            playlist.sort_name = create_sort_name(playlist.name)

        match = {"item_id": item_id}
        await self.mass.database.update(
            self.db_table,
            match,
            {
                "name": playlist.name,
                "sort_name": playlist.sort_name,
                "owner": playlist.owner,
                "is_editable": playlist.is_editable,
                "checksum": playlist.checksum,
                "metadata": json_serializer(metadata),
                "provider_ids": json_serializer(provider_ids),
            },
        )
        await self.mass.music.add_provider_mappings(
            item_id, MediaType.PLAYLIST, playlist.provider_ids
        )
        self.logger.debug("updated %s in database: %s", playlist.name, item_id)
        db_item = await self.get_db_item(item_id)
        self.mass.signal_event(
            MassEvent(type=EventType.PLAYLIST_UPDATED, object_id=item_id, data=playlist)
        )
        return db_item

    async def get_db_playlist_tracks(self, item_id) -> List[Track]:
        """Get playlist tracks for an in-library playlist."""
        query = (
            "SELECT TRACKS.*, PLAYLISTTRACKS.position "
            "FROM [tracks] TRACKS "
            "JOIN playlist_tracks PLAYLISTTRACKS ON TRACKS.item_id = PLAYLISTTRACKS.track_id "
            f"WHERE PLAYLISTTRACKS.playlist_id = {item_id}"
        )
        return await self.mass.music.tracks.get_db_items(query)

    async def add_db_playlist_track(
        self, playlist_id: int, track_id: int, position: int
    ) -> None:
        """Add playlist track for an in-library playlist."""
        return await self.mass.database.insert_or_replace(
            "playlist_tracks",
            {"playlist_id": playlist_id, "track_id": track_id, "position": position},
        )

    async def remove_db_playlist_track(
        self,
        playlist_id: int,
        track_id: Optional[int] = None,
        position: Optional[int] = None,
    ) -> None:
        """Remove playlist track from an in-library playlist."""
        match = {"playlist_id": playlist_id}
        if track_id is not None:
            match["track_id"] = track_id
        if position is not None:
            match["position"] = position
        return await self.mass.database.delete(
            "playlist_tracks",
            match,
        )
