"""Manage MediaItems of type Track."""
from __future__ import annotations

import asyncio
from typing import List

from music_assistant.constants import EventType, MassEvent
from music_assistant.helpers.compare import (
    compare_artists,
    compare_strings,
    compare_track,
)
from music_assistant.helpers.database import TABLE_TRACKS
from music_assistant.helpers.json import json_serializer
from music_assistant.helpers.util import create_sort_name
from music_assistant.models.media_controller import MediaControllerBase
from music_assistant.models.media_items import ItemMapping, MediaType, Track


class TracksController(MediaControllerBase[Track]):
    """Controller managing MediaItems of type Track."""

    db_table = TABLE_TRACKS
    media_type = MediaType.TRACK
    item_cls = Track

    async def get(self, *args, **kwargs) -> Track:
        """Return (full) details for a single media item."""
        track = await super().get(*args, **kwargs)
        # append full album details to full track item
        if track.album:
            track.album = await self.mass.music.albums.get(
                track.album.item_id, track.album.provider
            )
        # append full artist details to full track item
        full_artists = []
        for artist in track.artists:
            full_artists.append(
                await self.mass.music.artists.get(artist.item_id, artist.provider)
            )
        track.artists = full_artists
        return track

    async def add(self, item: Track) -> Track:
        """Add track to local db and return the new database item."""
        # make sure we have artists
        assert item.artists
        # grab additional metadata
        await self.mass.metadata.get_track_metadata(item)
        db_item = await self.add_db_item(item)
        # also fetch same track on all providers (will also get other quality versions)
        await self._match(db_item)
        db_item = await self.get_db_item(db_item.item_id)
        self.mass.signal_event(
            MassEvent(EventType.TRACK_ADDED, object_id=db_item.uri, data=db_item)
        )
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
            if provider.id == "filesystem":
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
                        await self.update_db_item(db_track.item_id, search_result_item)
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
                                await self.mass.music.artists.update_db_item(
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
        assert track.provider_ids
        if not track.sort_name:
            track.sort_name = create_sort_name(track.name)
        cur_item = None
        async with self.mass.database.get_db() as _db:
            track_album = track.album
            if track_album and not isinstance(track.album, ItemMapping):
                track_album = ItemMapping.from_item(
                    await self.get_db_item_by_prov_id(
                        track.album.provider, track.album.item_id, db=_db
                    )
                    or await self.mass.music.albums.add_db_item(track.album)
                )
            # always try to grab existing item by external_id
            if track.musicbrainz_id:
                match = {"musicbrainz_id": track.musicbrainz_id}
                cur_item = await self.mass.database.get_row(
                    self.db_table, match, db=_db
                )
            if not cur_item and track.isrc:
                match = {"isrc": track.isrc}
                cur_item = await self.mass.database.get_row(
                    self.db_table, match, db=_db
                )
            if not cur_item:
                # fallback to matching
                match = {"sort_name": track.sort_name}
                for row in await self.mass.database.get_rows(
                    self.db_table, match, db=_db
                ):
                    row_track = Track.from_db_row(row)
                    if compare_track(row_track, track):
                        cur_item = row_track
                        break
            if cur_item:
                # update existing
                return await self.update_db_item(cur_item.item_id, track)

            # no existing match found: insert new track
            track_artists = await self._get_track_artists(track)
            new_item = await self.mass.database.insert_or_replace(
                self.db_table,
                {
                    **track.to_db_row(),
                    "artists": json_serializer(track_artists),
                    "album": json_serializer(track_album),
                },
                db=_db,
            )
            item_id = new_item["item_id"]
            # store provider mappings
            await self.mass.music.set_provider_mappings(
                item_id, MediaType.TRACK, track.provider_ids, db=_db
            )

            # return created object
            return await self.get_db_item(item_id, db=_db)

    async def update_db_item(
        self, item_id: int, track: Track, overwrite: bool = False
    ) -> Track:
        """Update Track record in the database, merging data."""
        async with self.mass.database.get_db() as _db:
            cur_item = await self.get_db_item(item_id, db=_db)
            if overwrite:
                provider_ids = track.provider_ids
                track_artists = track.artists
                track_album = track.album or cur_item.album
            else:
                provider_ids = {*cur_item.provider_ids, *track.provider_ids}
                track_artists = cur_item.artists + track.artists
                track_album = cur_item.album or track.album
            metadata = cur_item.metadata.update(track.metadata, overwrite)
            if track_album and not isinstance(track_album, ItemMapping):
                track_album = ItemMapping.from_item(
                    await self.get_db_item_by_prov_id(
                        track_album.provider, track_album.item_id, db=_db
                    )
                    or await self.mass.music.albums.add_db_item(track_album)
                )

            # we store a mapping to artists on the track for easier access/listings
            track_artists = await self._get_track_artists(track, track_artists)
            await self.mass.database.update(
                self.db_table,
                {"item_id": item_id},
                {
                    "name": track.name if overwrite else cur_item.name,
                    "sort_name": track.sort_name if overwrite else cur_item.sort_name,
                    "version": track.version if overwrite else cur_item.version,
                    "duration": track.duration if overwrite else cur_item.duration,
                    "artists": json_serializer(track_artists),
                    "album": json_serializer(track_album),
                    "metadata": json_serializer(metadata),
                    "provider_ids": json_serializer(provider_ids),
                    "isrc": track.isrc or cur_item.isrc,
                },
                db=_db,
            )
            await self.mass.music.set_provider_mappings(
                item_id, MediaType.TRACK, provider_ids, db=_db
            )

            self.logger.debug("updated %s in database: %s", track.name, item_id)
            return await self.get_db_item(item_id, db=_db)

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
