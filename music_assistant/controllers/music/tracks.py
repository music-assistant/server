"""Manage MediaItems of type Track."""
from __future__ import annotations

import asyncio
from typing import List, Optional, Union

from databases import Database as Db

from music_assistant.helpers.compare import compare_artists, compare_track
from music_assistant.helpers.database import TABLE_TRACKS
from music_assistant.helpers.json import json_serializer
from music_assistant.models.enums import EventType, MediaType, ProviderType
from music_assistant.models.event import MassEvent
from music_assistant.models.media_controller import MediaControllerBase
from music_assistant.models.media_items import Album, Artist, ItemMapping, Track


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

    async def versions(
        self,
        item_id: str,
        provider: Optional[ProviderType] = None,
        provider_id: Optional[str] = None,
    ) -> List[Track]:
        """Return all versions of a track we can find on all providers."""
        track = await self.get(item_id, provider, provider_id)
        prov_types = {item.type for item in self.mass.music.providers}
        return [
            prov_item
            for prov_items in await asyncio.gather(
                *[self.search(track.name, prov_type) for prov_type in prov_types]
            )
            for prov_item in prov_items
            if compare_artists(prov_item.artists, track.artists)
        ]

    async def _match(self, db_track: Track) -> None:
        """
        Try to find matching track on all providers for the provided (database) track_id.

        This is used to link objects of different providers/qualities together.
        """
        if db_track.provider != ProviderType.DATABASE:
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
            search_result = await self.search(db_track.name, provider.type)
            for search_result_item in search_result:
                if not search_result_item.available:
                    continue
                if compare_track(search_result_item, db_track):
                    # 100% match, we can simply update the db with additional provider ids
                    match_found = True
                    await self.update_db_item(db_track.item_id, search_result_item)

            if not match_found:
                self.logger.debug(
                    "Could not find match for Track %s on provider %s",
                    db_track.name,
                    provider.name,
                )

    async def add_db_item(self, track: Track, db: Optional[Db] = None) -> Track:
        """Add a new track record to the database."""
        assert track.artists, "Track is missing artist(s)"
        assert track.provider_ids, "Track is missing provider id(s)"
        cur_item = None
        async with self.mass.database.get_db(db) as db:

            # always try to grab existing item by external_id
            if track.musicbrainz_id:
                match = {"musicbrainz_id": track.musicbrainz_id}
                cur_item = await self.mass.database.get_row(self.db_table, match, db=db)
            if not cur_item and track.isrc:
                match = {"isrc": track.isrc}
                cur_item = await self.mass.database.get_row(self.db_table, match, db=db)
            if not cur_item:
                # fallback to matching
                match = {"sort_name": track.sort_name}
                for row in await self.mass.database.get_rows(
                    self.db_table, match, db=db
                ):
                    row_track = Track.from_db_row(row)
                    if compare_track(row_track, track):
                        cur_item = row_track
                        break
            if cur_item:
                # update existing
                return await self.update_db_item(cur_item.item_id, track, db=db)

            # no existing match found: insert new track
            track_artists = await self._get_track_artists(track, db=db)
            track_albums = await self._get_track_albums(track, db=db)
            new_item = await self.mass.database.insert(
                self.db_table,
                {
                    **track.to_db_row(),
                    "artists": json_serializer(track_artists),
                    "albums": json_serializer(track_albums),
                },
                db=db,
            )
            item_id = new_item["item_id"]
            # return created object
            self.logger.debug("added %s to database: %s", track.name, item_id)
            return await self.get_db_item(item_id, db=db)

    async def update_db_item(
        self,
        item_id: int,
        track: Track,
        overwrite: bool = False,
        db: Optional[Db] = None,
    ) -> Track:
        """Update Track record in the database, merging data."""
        async with self.mass.database.get_db(db) as db:
            cur_item = await self.get_db_item(item_id, db=db)
            if overwrite:
                provider_ids = track.provider_ids
            else:
                provider_ids = {*cur_item.provider_ids, *track.provider_ids}
            metadata = cur_item.metadata.update(track.metadata, overwrite)

            # we store a mapping to artists/albums on the track for easier access/listings
            track_artists = await self._get_track_artists(cur_item, track, db=db)
            track_albums = await self._get_track_albums(cur_item, track, db=db)
            await self.mass.database.update(
                self.db_table,
                {"item_id": item_id},
                {
                    "name": track.name if overwrite else cur_item.name,
                    "sort_name": track.sort_name if overwrite else cur_item.sort_name,
                    "version": track.version if overwrite else cur_item.version,
                    "duration": track.duration if overwrite else cur_item.duration,
                    "artists": json_serializer(track_artists),
                    "albums": json_serializer(track_albums),
                    "metadata": json_serializer(metadata),
                    "provider_ids": json_serializer(provider_ids),
                    "isrc": track.isrc or cur_item.isrc,
                    "disc_number": track.disc_number or cur_item.disc_number,
                    "track_number": track.track_number or cur_item.track_number,
                },
                db=db,
            )
            self.logger.debug("updated %s in database: %s", track.name, item_id)
            return await self.get_db_item(item_id, db=db)

    async def _get_track_artists(
        self,
        base_track: Track,
        upd_track: Optional[Track] = None,
        db: Optional[Db] = None,
    ) -> List[ItemMapping]:
        """Extract all (unique) artists of track as ItemMapping."""
        if upd_track and upd_track.artists:
            track_artists = upd_track.artists
        else:
            track_artists = base_track.artists
        # use intermediate set to clear out duplicates
        return list({await self._get_artist_mapping(x, db=db) for x in track_artists})

    async def _get_track_albums(
        self,
        base_track: Track,
        upd_track: Optional[Track] = None,
        db: Optional[Db] = None,
    ) -> List[ItemMapping]:
        """Extract all (unique) artists of track as ItemMapping."""
        track_albums = []
        if upd_track and upd_track.album:
            track_albums.append(upd_track.album)
        if base_track.album and base_track.album not in track_albums:
            track_albums.append(base_track.album)
        for item in base_track.albums:
            if item not in track_albums:
                track_albums.append(item)
        if upd_track:
            for item in upd_track.albums:
                if item not in track_albums:
                    track_albums.append(item)
        # use intermediate set to clear out duplicates
        return [await self._get_album_mapping(x, db=db) for x in track_albums]

    async def _get_album_mapping(
        self, album: Union[Album, ItemMapping], db: Optional[Db] = None
    ) -> ItemMapping:
        """Extract (database) album as ItemMapping."""
        if album.provider == ProviderType.DATABASE:
            if isinstance(album, ItemMapping):
                return album
            return ItemMapping.from_item(album)

        if db_artist := await self.mass.music.albums.get_db_item_by_prov_id(
            album.item_id, provider=album.provider, db=db
        ):
            return ItemMapping.from_item(db_artist)

        db_artist = await self.mass.music.albums.add_db_item(album, db=db)
        return ItemMapping.from_item(db_artist)

    async def _get_artist_mapping(
        self, artist: Union[Artist, ItemMapping], db: Optional[Db] = None
    ) -> ItemMapping:
        """Extract (database) track artist as ItemMapping."""
        if artist.provider == ProviderType.DATABASE:
            if isinstance(artist, ItemMapping):
                return artist
            return ItemMapping.from_item(artist)

        if db_artist := await self.mass.music.artists.get_db_item_by_prov_id(
            artist.item_id, provider=artist.provider, db=db
        ):
            return ItemMapping.from_item(db_artist)

        db_artist = await self.mass.music.artists.add_db_item(artist, db=db)
        return ItemMapping.from_item(db_artist)
