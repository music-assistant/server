"""Manage MediaItems of type Track."""
from __future__ import annotations

import asyncio
from typing import List

from music_assistant.constants import EventType
from music_assistant.helpers.compare import (
    compare_artists,
    compare_strings,
    compare_track,
)
from music_assistant.helpers.json import json_serializer
from music_assistant.helpers.util import create_sort_name, merge_dict, merge_list
from music_assistant.models.media_controller import MediaControllerBase
from music_assistant.models.media_items import ItemMapping, MediaType, Track


class TracksController(MediaControllerBase[Track]):
    """Controller managing MediaItems of type Track."""

    db_table = "tracks"
    media_type = MediaType.TRACK
    item_cls = Track

    async def setup(self) -> None:
        """Async initialize of module."""
        # prepare database
        async with self.mass.database.get_db() as _db:
            await _db.execute(
                f"""CREATE TABLE IF NOT EXISTS {self.db_table}(
                        item_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL,
                        sort_name TEXT NOT NULL,
                        version TEXT,
                        duration INTEGER,
                        in_library BOOLEAN DEFAULT 0,
                        isrc TEXT,
                        artists json,
                        metadata json,
                        provider_ids json
                    );"""
            )

    async def add(self, item: Track) -> Track:
        """Add track to local db and return the new database item."""
        # make sure we have artists
        assert item.artists
        db_item = await self.add_db_item(item)
        # also fetch same track on all providers (will also get other quality versions)
        await self._match(db_item)
        db_item = await self.get_db_item(db_item.item_id)
        self.mass.signal_event(EventType.TRACK_ADDED, db_item)
        return db_item

    async def versions(self, item_id: str, provider_id: str) -> List[Track]:
        """Return all versions of a track we can find on all providers."""
        track = await self.get(item_id, provider_id)
        provider_ids = {item.id for item in self.mass.music.providers}
        first_artist = next(iter(track.artists))
        search_query = f"{first_artist.name} {track.name}"
        return [
            prov_item
            for prov_items in await asyncio.gather(
                *[self.search(search_query, prov_id) for prov_id in provider_ids]
            )
            for prov_item in prov_items
            if compare_artists(prov_item.artists, track.artists)
        ]

    async def _match(self, db_track: Track) -> None:
        """
        Try to find matching track on all providers for the provided (database) track_id.

        This is used to link objects of different providers/qualities together.
        """
        if db_track.provider != "database":
            return  # Matching only supported for database items
        if isinstance(db_track.album, ItemMapping):
            # matching only works if we have a full track object
            db_track = await self.get_db_item(db_track.item_id)
        for provider in self.mass.music.providers:
            if MediaType.TRACK not in provider.supported_mediatypes:
                continue
            self.logger.debug(
                "Trying to match track %s on provider %s", db_track.name, provider.name
            )
            match_found = False
            for db_track_artist in db_track.artists:
                if match_found:
                    break
                searchstr = f"{db_track_artist.name} {db_track.name}"
                if db_track.version:
                    searchstr += " " + db_track.version
                search_result = await self.search(searchstr, provider.id)
                for search_result_item in search_result:
                    if not search_result_item.available:
                        continue
                    if compare_track(search_result_item, db_track):
                        # 100% match, we can simply update the db with additional provider ids
                        match_found = True
                        await self.update_db_track(db_track.item_id, search_result_item)
                        # while we're here, also match the artist
                        if db_track_artist.provider == "database":
                            for artist in search_result_item.artists:
                                if not compare_strings(
                                    db_track_artist.name, artist.name
                                ):
                                    continue
                                prov_artist = (
                                    await self.mass.music.artists.get_provider_item(
                                        artist.item_id, artist.provider
                                    )
                                )
                                await self.mass.music.artists.update_db_artist(
                                    db_track_artist.item_id, prov_artist
                                )

            if not match_found:
                self.logger.debug(
                    "Could not find match for Track %s on provider %s",
                    db_track.name,
                    provider.name,
                )

    async def add_db_item(self, track: Track) -> Track:
        """Add a new track record to the database."""
        assert track.artists, "Track is missing artist(s)"
        if not track.sort_name:
            track.sort_name = create_sort_name(track.name)
        cur_item = None
        # always try to grab existing item by external_id
        if track.isrc:
            match = {"isrc": track.isrc}
            cur_item = await self.mass.database.get_row(self.db_table, match)
        if not cur_item:
            # fallback to matching
            match = {"sort_name": track.sort_name}
            for row in await self.mass.database.get_rows(self.db_table, match):
                row_track = Track.from_db_row(row)
                if compare_track(row_track, track):
                    cur_item = row_track
                    break
        if cur_item:
            # update existing
            return await self.update_db_track(cur_item.item_id, track)

        # no existing match found: insert new track
        track_artists = await self._get_track_artists(track)
        new_item = await self.mass.database.insert_or_replace(
            self.db_table,
            {
                **track.to_db_row(),
                "artists": json_serializer(track_artists),
            },
        )
        item_id = new_item["item_id"]
        # store provider mappings
        await self.mass.music.add_provider_mappings(
            item_id, MediaType.TRACK, track.provider_ids
        )

        # add track to album_tracks
        if track.album is not None:
            album = await self.get_db_item_by_prov_id(
                track.album.provider, track.album.item_id
            ) or await self.mass.music.albums.add_db_item(track.album)
            if (
                album
                and track.disc_number is not None
                and track.track_number is not None
            ):
                await self.mass.music.albums.add_db_album_track(
                    album.item_id, item_id, track.disc_number, track.track_number
                )
        # return created object
        return await self.get_db_item(item_id)

    async def update_db_track(self, item_id: int, track: Track) -> Track:
        """Update Track record in the database."""
        cur_item = await self.get_db_item(item_id)
        metadata = merge_dict(cur_item.metadata, track.metadata)
        provider_ids = merge_list(cur_item.provider_ids, track.provider_ids)
        # we store a mapping to artists on the track for easier access/listings
        track_artists = await self._get_track_artists(track, cur_item.artists)
        match = {"item_id": item_id}
        await self.mass.database.update(
            self.db_table,
            match,
            {
                "artists": json_serializer(track_artists),
                "metadata": json_serializer(metadata),
                "provider_ids": json_serializer(provider_ids),
                "isrc": cur_item.isrc or track.isrc,
                "duration": cur_item.duration or track.duration,
            },
        )
        await self.mass.music.add_provider_mappings(
            item_id, MediaType.TRACK, track.provider_ids
        )
        # add track to album_tracks
        if (
            track.album is not None
            and track.disc_number is not None
            and track.track_number is not None
        ):
            album = await self.get_db_item_by_prov_id(
                track.album.provider, track.album.item_id
            ) or await self.mass.music.albums.add_db_item(track.album)
            if album:
                await self.mass.music.albums.add_db_album_track(
                    album.item_id, item_id, track.disc_number, track.track_number
                )
        self.logger.debug("updated %s in database: %s", track.name, item_id)
        return await self.get_db_item(item_id)

    async def _get_track_artists(
        self, track: Track, cur_artists: List[ItemMapping] | None = None
    ) -> List[ItemMapping]:
        """Extract all (unique) artists of track as ItemMapping."""
        if cur_artists is None:
            cur_artists = []
        cur_artists.extend(track.artists)
        track_artists: List[ItemMapping] = []
        for item in cur_artists:
            cur_names = {x.name for x in track_artists}
            cur_ids = {x.item_id for x in track_artists}
            track_artist = (
                await self.mass.music.artists.get_db_item_by_prov_id(
                    item.provider, item.item_id
                )
                or item
            )
            if (
                track_artist.name not in cur_names
                and track_artist.item_id not in cur_ids
            ):
                track_artists.append(ItemMapping.from_item(track_artist))
        return track_artists
