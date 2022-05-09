"""Manage MediaItems of type Album."""
from __future__ import annotations

import asyncio
from typing import List

from music_assistant.constants import EventType, MassEvent
from music_assistant.helpers.compare import compare_album, compare_strings
from music_assistant.helpers.database import TABLE_ALBUMS
from music_assistant.helpers.json import json_serializer
from music_assistant.helpers.util import create_sort_name
from music_assistant.models.media_controller import MediaControllerBase
from music_assistant.models.media_items import (
    Album,
    AlbumType,
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
        album.artist = await self.mass.music.artists.get(
            album.artist.item_id, album.artist.provider
        )
        return album

    async def tracks(self, item_id: str, provider_id: str) -> List[Track]:
        """Return album tracks for the given provider album id."""
        album = await self.get(item_id, provider_id)
        # simply return the tracks from the first provider
        for prov in album.provider_ids:
            if tracks := await self.get_provider_album_tracks(
                prov.item_id, prov.provider
            ):
                return tracks
        return []

    async def versions(self, item_id: str, provider_id: str) -> List[Album]:
        """Return all versions of an album we can find on all providers."""
        album = await self.get(item_id, provider_id)
        provider_ids = {item.id for item in self.mass.music.providers}
        search_query = f"{album.artist.name} {album.name}"
        return [
            prov_item
            for prov_items in await asyncio.gather(
                *[self.search(search_query, prov_id) for prov_id in provider_ids]
            )
            for prov_item in prov_items
            if compare_strings(prov_item.artist.name, album.artist.name)
        ]

    async def add(self, item: Album) -> Album:
        """Add album to local db and return the database item."""
        # make sure we have an artist
        assert item.artist
        # grab additional metadata
        await self.mass.metadata.get_album_metadata(item)
        db_item = await self.add_db_item(item)
        # also fetch same album on all providers
        await self._match(db_item)
        db_item = await self.get_db_item(db_item.item_id)
        self.mass.signal_event(
            MassEvent(EventType.ALBUM_ADDED, object_id=db_item.uri, data=db_item)
        )
        return db_item

    async def get_provider_album_tracks(
        self, item_id: str, provider_id: str
    ) -> List[Track]:
        """Return album tracks for the given provider album id."""
        provider = self.mass.music.get_provider(provider_id)
        if not provider:
            return []
        return await provider.get_album_tracks(item_id)

    async def add_db_item(self, album: Album) -> Album:
        """Add a new album record to the database."""
        cur_item = None
        if not album.sort_name:
            album.sort_name = create_sort_name(album.name)
        assert album.provider_ids
        async with self.mass.database.get_db() as _db:
            # always try to grab existing item by external_id
            if album.musicbrainz_id:
                match = {"musicbrainz_id": album.musicbrainz_id}
                cur_item = await self.mass.database.get_row(
                    self.db_table, match, db=_db
                )
            if not cur_item and album.upc:
                match = {"upc": album.upc}
                cur_item = await self.mass.database.get_row(
                    self.db_table, match, db=_db
                )
            if not cur_item:
                # fallback to matching
                match = {"sort_name": album.sort_name}
                for row in await self.mass.database.get_rows(
                    self.db_table, match, db=_db
                ):
                    row_album = Album.from_db_row(row)
                    if compare_album(row_album, album):
                        cur_item = row_album
                        break
            if cur_item:
                # update existing
                return await self.update_db_item(cur_item.item_id, album)

            # insert new album
            if album.artist.musicbrainz_id and album.artist.provider != "database":
                album_artist = await self.mass.music.artists.add_db_item(album.artist)
            else:
                album_artist = (
                    await self.mass.music.artists.get_db_item_by_prov_id(
                        album.artist.provider, album.artist.item_id, db=_db
                    )
                    or album.artist
                )
            new_item = await self.mass.database.insert_or_replace(
                self.db_table,
                {
                    **album.to_db_row(),
                    "artist": json_serializer(ItemMapping.from_item(album_artist)),
                },
                db=_db,
            )
            item_id = new_item["item_id"]
            # store provider mappings
            await self.mass.music.set_provider_mappings(
                item_id, MediaType.ALBUM, album.provider_ids, db=_db
            )
            self.logger.debug("added %s to database", album.name)
            # return created object
            return await self.get_db_item(item_id, db=_db)

    async def update_db_item(
        self, item_id: int, album: Album, overwrite: bool = False
    ) -> Album:
        """Update Album record in the database."""
        async with self.mass.database.get_db() as _db:
            cur_item = await self.get_db_item(item_id)
            if (
                not isinstance(album.artist, ItemMapping)
                and album.artist.musicbrainz_id
                and album.artist.provider != "database"
            ):
                album_artist = await self.mass.music.artists.add_db_item(album.artist)
            else:
                album_artist = (
                    await self.mass.music.artists.get_db_item_by_prov_id(
                        album.artist.provider, album.artist.item_id, db=_db
                    )
                    or album.artist
                )
            if overwrite:
                metadata = album.metadata
                provider_ids = album.provider_ids
            else:
                metadata = cur_item.metadata.update(album.metadata)
                provider_ids = {*cur_item.provider_ids, *album.provider_ids}

            if album.album_type != AlbumType.UNKNOWN:
                album_type = album.album_type
            else:
                album_type = cur_item.album_type

            await self.mass.database.update(
                self.db_table,
                {"item_id": item_id},
                {
                    "name": album.name if overwrite else cur_item.name,
                    "sort_name": album.sort_name if overwrite else cur_item.sort_name,
                    "version": album.version if overwrite else cur_item.version,
                    "year": album.year or cur_item.year,
                    "upc": album.upc or cur_item.upc,
                    "album_type": album_type.value,
                    "artist": json_serializer(ItemMapping.from_item(album_artist)),
                    "metadata": json_serializer(metadata),
                    "provider_ids": json_serializer(provider_ids),
                },
                db=_db,
            )
            await self.mass.music.set_provider_mappings(
                item_id, MediaType.ALBUM, provider_ids, db=_db
            )
            self.logger.debug("updated %s in database: %s", album.name, item_id)
            return await self.get_db_item(item_id)

    async def _match(self, db_album: Album) -> None:
        """
        Try to find matching album on all providers for the provided (database) album.

        This is used to link objects of different providers/qualities together.
        """
        if db_album.provider != "database":
            return  # Matching only supported for database items

        async def find_prov_match(provider: MusicProvider):
            self.logger.debug(
                "Trying to match album %s on provider %s", db_album.name, provider.name
            )
            match_found = False
            searchstr = f"{db_album.artist.name} {db_album.name}"
            if db_album.version:
                searchstr += " " + db_album.version
            search_result = await self.search(searchstr, provider.id)
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
                    # while we're here, also match the artist
                    if db_album.artist.provider == "database":
                        await self.mass.music.artists.update_db_item(
                            db_album.artist.item_id, prov_album.artist
                        )

            # no match found
            if not match_found:
                self.logger.debug(
                    "Could not find match for Album %s on provider %s",
                    db_album.name,
                    provider.name,
                )

        # try to find match on all providers
        for provider in self.mass.music.providers:
            if provider.id == "filesystem":
                continue
            if MediaType.ALBUM in provider.supported_mediatypes:
                await find_prov_match(provider)
