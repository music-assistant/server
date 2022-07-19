"""Manage MediaItems of type Track."""
from __future__ import annotations

import asyncio
from typing import List, Optional, Union

from music_assistant.helpers.compare import compare_artists, compare_track
from music_assistant.helpers.database import TABLE_TRACKS
from music_assistant.helpers.json import json_serializer
from music_assistant.models.enums import (
    EventType,
    MediaType,
    MusicProviderFeature,
    ProviderType,
)
from music_assistant.models.event import MassEvent
from music_assistant.models.media_controller import MediaControllerBase
from music_assistant.models.media_items import (
    Album,
    Artist,
    ItemMapping,
    Track,
    TrackAlbumMapping,
)


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

    async def add(self, item: Track, overwrite_existing: bool = False) -> Track:
        """Add track to local db and return the new database item."""
        # make sure we have artists
        assert item.artists
        # grab additional metadata
        await self.mass.metadata.get_track_metadata(item)
        db_item = await self.add_db_item(item, overwrite_existing)
        # also fetch same track on all providers (will also get other quality versions)
        await self._match(db_item)
        return await self.get_db_item(db_item.item_id)

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
            if MusicProviderFeature.SEARCH not in provider.supported_features:
                continue
            self.logger.debug(
                "Trying to match track %s on provider %s", db_track.name, provider.name
            )
            match_found = False
            for search_str in (
                db_track.name,
                f"{db_track.artists[0].name} - {db_track.name}",
                f"{db_track.artists[0].name} {db_track.name}",
            ):
                if match_found:
                    break
                search_result = await self.search(search_str, provider.type)
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

    async def add_db_item(self, item: Track, overwrite_existing: bool = False) -> Track:
        """Add a new item record to the database."""
        assert item.artists, "Track is missing artist(s)"
        assert item.provider_ids, "Track is missing provider id(s)"
        async with self._db_add_lock:
            cur_item = None

            # always try to grab existing item by external_id
            if item.musicbrainz_id:
                match = {"musicbrainz_id": item.musicbrainz_id}
                cur_item = await self.mass.database.get_row(self.db_table, match)
            for isrc in item.isrcs:
                match = {"isrc": isrc}
                cur_item = await self.mass.database.get_row(self.db_table, match)
            if not cur_item:
                # fallback to matching
                match = {"sort_name": item.sort_name}
                for row in await self.mass.database.get_rows(self.db_table, match):
                    row_track = Track.from_db_row(row)
                    if compare_track(row_track, item):
                        cur_item = row_track
                        break
            if cur_item:
                # update existing
                return await self.update_db_item(
                    cur_item.item_id, item, overwrite=overwrite_existing
                )

            # no existing match found: insert new item
            track_artists = await self._get_track_artists(item)
            track_albums = await self._get_track_albums(
                item, overwrite=overwrite_existing
            )
            if track_artists:
                sort_artist = track_artists[0].sort_name
            else:
                sort_artist = ""
            if track_albums:
                sort_album = track_albums[0].sort_name
            else:
                sort_album = ""
            new_item = await self.mass.database.insert(
                self.db_table,
                {
                    **item.to_db_row(),
                    "artists": json_serializer(track_artists),
                    "albums": json_serializer(track_albums),
                    "sort_artist": sort_artist,
                    "sort_album": sort_album,
                },
            )
            item_id = new_item["item_id"]
            # return created object
            self.logger.debug("added %s to database: %s", item.name, item_id)
            db_item = await self.get_db_item(item_id)
            self.mass.signal_event(
                MassEvent(EventType.MEDIA_ITEM_ADDED, db_item.uri, db_item)
            )
            return db_item

    async def update_db_item(
        self,
        item_id: int,
        item: Track,
        overwrite: bool = False,
    ) -> Track:
        """Update Track record in the database, merging data."""
        cur_item = await self.get_db_item(item_id)

        if overwrite:
            metadata = item.metadata
            provider_ids = item.provider_ids
            metadata.last_refresh = None
            # we store a mapping to artists/albums on the item for easier access/listings
            track_artists = await self._get_track_artists(item)
            track_albums = await self._get_track_albums(item, overwrite=True)
        else:
            metadata = cur_item.metadata.update(item.metadata, overwrite)
            provider_ids = {*cur_item.provider_ids, *item.provider_ids}
            track_artists = await self._get_track_artists(cur_item, item)
            track_albums = await self._get_track_albums(cur_item, item)

        await self.mass.database.update(
            self.db_table,
            {"item_id": item_id},
            {
                "name": item.name if overwrite else cur_item.name,
                "sort_name": item.sort_name if overwrite else cur_item.sort_name,
                "version": item.version if overwrite else cur_item.version,
                "duration": item.duration if overwrite else cur_item.duration,
                "artists": json_serializer(track_artists),
                "albums": json_serializer(track_albums),
                "metadata": json_serializer(metadata),
                "provider_ids": json_serializer(provider_ids),
                "isrc": item.isrc or cur_item.isrc,
            },
        )
        self.logger.debug("updated %s in database: %s", item.name, item_id)
        db_item = await self.get_db_item(item_id)
        self.mass.signal_event(
            MassEvent(EventType.MEDIA_ITEM_UPDATED, db_item.uri, db_item)
        )
        return db_item

    async def _get_track_artists(
        self,
        base_track: Track,
        upd_track: Optional[Track] = None,
    ) -> List[ItemMapping]:
        """Extract all (unique) artists of track as ItemMapping."""
        if upd_track and upd_track.artists:
            track_artists = upd_track.artists
        else:
            track_artists = base_track.artists
        # use intermediate set to clear out duplicates
        return list({await self._get_artist_mapping(x) for x in track_artists})

    async def _get_track_albums(
        self,
        base_track: Track,
        upd_track: Optional[Track] = None,
        overwrite: bool = False,
    ) -> List[TrackAlbumMapping]:
        """Extract all (unique) albums of track as TrackAlbumMapping."""
        track_albums: List[TrackAlbumMapping] = []
        # existing TrackAlbumMappings are starting point
        if upd_track and upd_track.albums:
            track_albums = upd_track.albums
        elif base_track.albums:
            track_albums = base_track.albums
        # append update item album if needed
        if upd_track and upd_track.album:
            mapping = await self._get_album_mapping(
                upd_track.album, overwrite=overwrite
            )
            mapping = TrackAlbumMapping.from_dict(
                {
                    **mapping.to_dict(),
                    "disc_number": upd_track.disc_number,
                    "track_number": upd_track.track_number,
                }
            )
            if mapping not in track_albums:
                track_albums.append(mapping)
        # append base item album if needed
        elif base_track and base_track.album:
            mapping = await self._get_album_mapping(
                base_track.album, overwrite=overwrite
            )
            mapping = TrackAlbumMapping.from_dict(
                {
                    **mapping.to_dict(),
                    "disc_number": base_track.disc_number,
                    "track_number": base_track.track_number,
                }
            )
            if mapping not in track_albums:
                track_albums.append(mapping)

        return track_albums

    async def _get_album_mapping(
        self,
        album: Union[Album, ItemMapping],
        overwrite: bool = False,
    ) -> ItemMapping:
        """Extract (database) album as ItemMapping."""
        if overwrite:
            db_album = await self.mass.music.albums.add_db_item(
                album, overwrite_existing=True
            )

        if album.provider == ProviderType.DATABASE:
            if isinstance(album, ItemMapping):
                return album
            return ItemMapping.from_item(album)

        if db_album := await self.mass.music.albums.get_db_item_by_prov_id(
            album.item_id, provider=album.provider
        ):
            return ItemMapping.from_item(db_album)

        db_album = await self.mass.music.albums.add_db_item(
            album, overwrite_existing=overwrite
        )
        return ItemMapping.from_item(db_album)

    async def _get_artist_mapping(
        self, artist: Union[Artist, ItemMapping], overwrite: bool = False
    ) -> ItemMapping:
        """Extract (database) track artist as ItemMapping."""
        if overwrite:
            artist = await self.mass.music.artists.add_db_item(
                artist, overwrite_existing=True
            )
        if artist.provider == ProviderType.DATABASE:
            if isinstance(artist, ItemMapping):
                return artist
            return ItemMapping.from_item(artist)

        if db_artist := await self.mass.music.artists.get_db_item_by_prov_id(
            artist.item_id, provider=artist.provider
        ):
            return ItemMapping.from_item(db_artist)

        db_artist = await self.mass.music.artists.add_db_item(artist)
        return ItemMapping.from_item(db_artist)
