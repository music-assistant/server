"""Manage MediaItems of type Playlist."""
from __future__ import annotations
import time

from typing import List

from music_assistant.constants import EventType
from music_assistant.helpers.cache import cached
from music_assistant.helpers.errors import InvalidDataError, MediaNotFoundError
from music_assistant.helpers.util import merge_dict, merge_list
from music_assistant.helpers.web import json_serializer
from music_assistant.music.models import MediaControllerBase, MediaType, Playlist, Track
from music_assistant.helpers.tasks import TaskInfo


class PlaylistController(MediaControllerBase[Playlist]):
    """Controller managing MediaItems of type Playlist."""

    db_table = "playlists"
    media_type = MediaType.PLAYLIST
    item_cls = Playlist

    async def setup(self):
        """Async initialize of module."""
        # prepare database
        await self.mass.database.execute(
            f"""CREATE TABLE IF NOT EXISTS {self.db_table}(
                    item_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    sort_name TEXT,
                    owner TEXT NOT NULL,
                    is_editable BOOLEAN NOT NULL,
                    checksum TEXT NOT NULL,
                    in_library BOOLEAN DEFAULT 0,
                    metadata json,
                    provider_ids json,
                    UNIQUE(name, owner)
                );"""
        )
        await self.mass.database.execute(
            """CREATE TABLE IF NOT EXISTS playlist_tracks(
                    playlist_id INTEGER,
                    track_id INTEGER,
                    position INTEGER,
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
        for prov in playlist.provider_ids:
            # playlist tracks are not stored in db, we always fetch them (cached) from the provider.
            provider = self.mass.music.get_provider(prov.provider)
            cache_checksum = playlist.checksum
            cache_key = f"{prov.provider}.playlist_tracks.{item_id}"
            return await cached(
                self.mass.cache,
                cache_key,
                provider.get_playlist_tracks,
                item_id,
                checksum=cache_checksum,
            )

    async def add(self, item: Playlist) -> Playlist:
        """Add playlist to local db and return the new database item."""
        db_item = await self.add_db_playlist(item)
        self.mass.signal_event(EventType.PLAYLIST_ADDED, db_item)
        return db_item

    async def add_playlist_tracks(
        self, item_id: str, provider_id: str, tracks: List[Track]
    ) -> List[TaskInfo]:
        """Add multiple tracks to playlist. Creates background tasks to process the action."""
        result = []
        playlist = await self.get(item_id, provider_id)
        if not playlist:
            raise MediaNotFoundError(f"Playlist {item_id} not found")
        if not playlist.is_editable:
            raise InvalidDataError(f"Playlist {playlist.name} is not editable")
        for track in tracks:
            job_desc = f"Add track {track.uri} to playlist {playlist.uri}"
            result.append(
                self.mass.tasks.add(
                    job_desc, self.add_playlist_track, item_id, provider_id, track
                )
            )
        return result

    async def add_playlist_track(
        self, item_id: str, provider_id: str, track: Track
    ) -> None:
        """Add track to playlist - make sure we dont add duplicates."""
        # we can only edit playlists that are in the database (marked as editable)
        playlist = await self.get(item_id, provider_id)
        if not playlist:
            raise MediaNotFoundError(f"Playlist {item_id} not found")
        if not playlist.is_editable:
            raise InvalidDataError(f"Playlist {playlist.name} is not editable")
        # make sure we have recent full track details
        track = await self.mass.music.tracks.get(
            track.item_id, track.provider, refresh=True, lazy=False
        )
        # a playlist can only have one provider (for now)
        playlist_prov = next(iter(playlist.provider_ids))
        # grab all existing track ids in the playlist so we can check for duplicates
        cur_playlist_track_ids = set()
        for item in await self.tracks(playlist_prov.item_id, playlist_prov.provider):
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
        # invalidate cache
        playlist.checksum = str(time.time())
        await self.update_db_playlist(playlist.item_id, playlist)
        # return result of the action on the provider
        provider = self.mass.music.get_provider(playlist_prov.provider)
        return await provider.add_playlist_tracks(
            playlist_prov.item_id, [track_id_to_add]
        )

    async def remove_playlist_tracks(
        self, item_id: str, provider_id: str, tracks: List[Track]
    ) -> List[TaskInfo]:
        """Remove multiple tracks from playlist. Creates background tasks to process the action."""
        result = []
        playlist = await self.get(item_id, provider_id)
        if not playlist:
            raise MediaNotFoundError(f"Playlist {item_id} not found")
        if not playlist.is_editable:
            raise InvalidDataError(f"Playlist {playlist.name} is not editable")
        for track in tracks:
            job_desc = f"Remove track {track.uri} from playlist {playlist.uri}"
            result.append(
                self.mass.tasks.add(
                    job_desc, self.remove_playlist_track, item_id, provider_id, track
                )
            )
        return result

    async def remove_playlist_track(
        self, item_id: str, provider_id: str, track: Track
    ) -> None:
        """Remove track from playlist."""
        # we can only edit playlists that are in the database (marked as editable)
        playlist = await self.get(item_id, provider_id)
        if not playlist:
            raise MediaNotFoundError(f"Playlist {item_id} not found")
        if not playlist.is_editable:
            raise InvalidDataError(f"Playlist {playlist.name} is not editable")
        # playlist can only have one provider (for now)
        prov_playlist = next(iter(playlist.provider_ids))
        track_ids_to_remove = set()
        # a track can contain multiple versions on the same provider, remove all
        for track_provider in track.provider_ids:
            if track_provider.provider == prov_playlist.provider:
                track_ids_to_remove.add(track_provider.item_id)
        # actually remove the tracks from the playlist on the provider
        if track_ids_to_remove:
            # invalidate cache
            playlist.checksum = str(time.time())
            await self.update_db_playlist(playlist.item_id, playlist)
            provider = self.mass.music.get_provider(prov_playlist.provider)
            return await provider.remove_playlist_tracks(
                prov_playlist.item_id, track_ids_to_remove
            )

    async def add_db_playlist(self, playlist: Playlist) -> Playlist:
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
        return await self.get_db_item(item_id)

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
