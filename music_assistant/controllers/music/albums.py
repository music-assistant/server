"""Manage MediaItems of type Album."""
from __future__ import annotations

import asyncio
import itertools
from typing import Dict, List, Optional, Union

from databases import Database as Db

from music_assistant.helpers.compare import compare_album, compare_artist
from music_assistant.helpers.database import TABLE_ALBUMS, TABLE_TRACKS
from music_assistant.helpers.json import json_serializer
from music_assistant.models.enums import EventType, ProviderType
from music_assistant.models.event import MassEvent
from music_assistant.models.media_controller import MediaControllerBase
from music_assistant.models.media_items import (
    Album,
    AlbumType,
    Artist,
    ItemMapping,
    MediaType,
    Track,
)
from music_assistant.models.provider import MusicProvider


class AlbumsController(MediaControllerBase[Album]):
    """Controller managing MediaItems of type Album."""

    db_table = TABLE_ALBUMS
    media_type = MediaType.ALBUM
    item_cls = Album

    async def get(self, *args, **kwargs) -> Album:
        """Return (full) details for a single media item."""
        album = await super().get(*args, **kwargs)
        # append full artist details to full album item
        if album.artist:
            album.artist = await self.mass.music.artists.get(
                album.artist.item_id, album.artist.provider
            )
        return album

    async def tracks(
        self,
        item_id: str,
        provider: Optional[ProviderType] = None,
        provider_id: Optional[str] = None,
    ) -> List[Track]:
        """Return album tracks for the given provider album id."""
        album = await self.get(item_id, provider, provider_id)
        # get results from all providers
        coros = [
            self.get_provider_album_tracks(item.item_id, item.prov_id)
            for item in album.provider_ids
        ]
        tracks = itertools.chain.from_iterable(await asyncio.gather(*coros))
        # merge duplicates using a dict
        final_items: Dict[str, Track] = {}
        for track in tracks:
            key = f".{track.name}.{track.version}.{track.disc_number}.{track.track_number}"
            if key in final_items:
                final_items[key].provider_ids.update(track.provider_ids)
            else:
                track.album = album
                final_items[key] = track
            if album.in_library:
                final_items[key].in_library = True
        return list(final_items.values())

    async def versions(
        self,
        item_id: str,
        provider: Optional[ProviderType] = None,
        provider_id: Optional[str] = None,
    ) -> List[Album]:
        """Return all versions of an album we can find on all providers."""
        album = await self.get(item_id, provider, provider_id)
        prov_types = {item.type for item in self.mass.music.providers}
        return [
            prov_item
            for prov_items in await asyncio.gather(
                *[self.search(album.name, prov_type) for prov_type in prov_types]
            )
            for prov_item in prov_items
            if prov_item.sort_name == album.sort_name
            and compare_artist(prov_item.artist, album.artist)
        ]

    async def add(self, item: Album) -> Album:
        """Add album to local db and return the database item."""
        # grab additional metadata
        await self.mass.metadata.get_album_metadata(item)
        db_item = await self.add_db_item(item)
        # also fetch same album on all providers
        await self._match(db_item)
        db_item = await self.get_db_item(db_item.item_id)
        return db_item

    async def get_provider_album_tracks(
        self,
        item_id: str,
        provider: Optional[ProviderType] = None,
        provider_id: Optional[str] = None,
    ) -> List[Track]:
        """Return album tracks for the given provider album id."""
        prov = self.mass.music.get_provider(provider_id or provider)
        if not prov:
            return []
        return await prov.get_album_tracks(item_id)

    async def add_db_item(self, item: Album, db: Optional[Db] = None) -> Album:
        """Add a new record to the database."""
        assert item.provider_ids, f"Album {item.name} is missing provider id(s)"
        assert item.artist, f"Album {item.name} is missing artist"
        cur_item = None
        async with self.mass.database.get_db(db) as db:
            # always try to grab existing item by musicbrainz_id
            if item.musicbrainz_id:
                match = {"musicbrainz_id": item.musicbrainz_id}
                cur_item = await self.mass.database.get_row(self.db_table, match, db=db)
            if not cur_item and item.upc:
                match = {"upc": item.upc}
                cur_item = await self.mass.database.get_row(self.db_table, match, db=db)
            if not cur_item:
                # fallback to matching
                match = {"sort_name": item.sort_name}
                for row in await self.mass.database.get_rows(
                    self.db_table, match, db=db
                ):
                    row_album = Album.from_db_row(row)
                    if compare_album(row_album, item):
                        cur_item = row_album
                        break
            if cur_item:
                # update existing
                return await self.update_db_item(cur_item.item_id, item, db=db)

            # insert new item
            album_artists = await self._get_album_artists(item, cur_item, db=db)
            new_item = await self.mass.database.insert(
                self.db_table,
                {
                    **item.to_db_row(),
                    "artists": json_serializer(album_artists) or None,
                },
                db=db,
            )
            item_id = new_item["item_id"]
            self.logger.debug("added %s to database", item.name)
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
        item: Album,
        overwrite: bool = False,
        db: Optional[Db] = None,
    ) -> Album:
        """Update Album record in the database."""
        assert item.provider_ids, f"Album {item.name} is missing provider id(s)"
        assert item.artist, f"Album {item.name} is missing artist"
        async with self.mass.database.get_db(db) as db:
            cur_item = await self.get_db_item(item_id)
            album_artists = await self._get_album_artists(item, cur_item, db=db)
            if overwrite:
                metadata = item.metadata
                provider_ids = item.provider_ids
            else:
                metadata = cur_item.metadata.update(item.metadata)
                provider_ids = {*cur_item.provider_ids, *item.provider_ids}

            if item.album_type != AlbumType.UNKNOWN:
                album_type = item.album_type
            else:
                album_type = cur_item.album_type

            await self.mass.database.update(
                self.db_table,
                {"item_id": item_id},
                {
                    "name": item.name if overwrite else cur_item.name,
                    "sort_name": item.sort_name if overwrite else cur_item.sort_name,
                    "version": item.version if overwrite else cur_item.version,
                    "year": item.year or cur_item.year,
                    "upc": item.upc or cur_item.upc,
                    "album_type": album_type.value,
                    "artists": json_serializer(album_artists) or None,
                    "metadata": json_serializer(metadata),
                    "provider_ids": json_serializer(provider_ids),
                },
                db=db,
            )
            self.logger.debug("updated %s in database: %s", item.name, item_id)
            db_item = await self.get_db_item(item_id, db=db)
            self.mass.signal_event(
                MassEvent(
                    EventType.MEDIA_ITEM_UPDATED, object_id=db_item.uri, data=db_item
                )
            )
            return db_item

    async def delete_db_item(self, item_id: int, db: Optional[Db] = None) -> None:
        """Delete record from the database."""

        # delete tracks connected to this album
        async with self.mass.database.get_db(db) as db:
            await self.mass.database.delete_where_query(
                TABLE_TRACKS, f"albums LIKE '%\"{item_id}\"%'", db=db
            )
        # delete the album itself from db
        await super().delete_db_item(item_id, db)

        self.logger.debug("deleted item with id %s from database", item_id)

    async def _match(self, db_album: Album) -> None:
        """
        Try to find matching album on all providers for the provided (database) album.

        This is used to link objects of different providers/qualities together.
        """
        if db_album.provider != ProviderType.DATABASE:
            return  # Matching only supported for database items

        async def find_prov_match(provider: MusicProvider):
            self.logger.debug(
                "Trying to match album %s on provider %s", db_album.name, provider.name
            )
            match_found = False
            for search_str in (
                db_album.name,
                f"{db_album.artist.name} - {db_album.name}",
                f"{db_album.artist.name} {db_album.name}",
            ):
                if match_found:
                    break
                search_result = await self.search(search_str, provider.id)
                for search_result_item in search_result:
                    if not search_result_item.available:
                        continue
                    if not compare_album(search_result_item, db_album):
                        continue
                    # we must fetch the full album version, search results are simplified objects
                    prov_album = await self.get_provider_item(
                        search_result_item.item_id, search_result_item.provider
                    )
                    if compare_album(prov_album, db_album):
                        # 100% match, we can simply update the db with additional provider ids
                        await self.update_db_item(db_album.item_id, prov_album)
                        match_found = True
            return match_found

        # try to find match on all providers
        cur_prov_types = {x.prov_type for x in db_album.provider_ids}
        for provider in self.mass.music.providers:
            if provider.type in cur_prov_types:
                continue
            if MediaType.ALBUM not in provider.supported_mediatypes:
                continue
            if await find_prov_match(provider):
                cur_prov_types.add(provider.type)
            else:
                self.logger.debug(
                    "Could not find match for Album %s on provider %s",
                    db_album.name,
                    provider.name,
                )

    async def _get_album_artists(
        self,
        db_album: Album,
        updated_album: Optional[Album] = None,
        db: Optional[Db] = None,
    ) -> List[ItemMapping]:
        """Extract (database) album artist(s) as ItemMapping."""
        album_artists = set()
        for album in (updated_album, db_album):
            if not album:
                continue
            for artist in album.artists:
                album_artists.add(await self._get_artist_mapping(artist, db=db))
        # use intermediate set to prevent duplicates
        return list(album_artists)

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
