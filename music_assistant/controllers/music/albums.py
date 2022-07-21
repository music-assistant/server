"""Manage MediaItems of type Album."""
from __future__ import annotations

import asyncio
from typing import List, Optional, Union

from music_assistant.helpers.compare import compare_album, compare_artist
from music_assistant.helpers.database import TABLE_ALBUMS, TABLE_TRACKS
from music_assistant.helpers.json import json_serializer
from music_assistant.helpers.tags import FALLBACK_ARTIST
from music_assistant.models.enums import EventType, MusicProviderFeature, ProviderType
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
from music_assistant.models.music_provider import MusicProvider


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

        if not (provider == ProviderType.DATABASE or provider_id == "database"):
            # return provider album tracks
            return await self._get_provider_album_tracks(item_id, provider, provider_id)

        # db_album requested: get results from first (non-file) provider
        return await self._get_db_album_tracks(item_id)

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

    async def add(self, item: Album, overwrite_existing: bool = False) -> Album:
        """Add album to local db and return the database item."""
        # grab additional metadata
        await self.mass.metadata.get_album_metadata(item)
        db_item = await self.add_db_item(item, overwrite_existing)
        # also fetch same album on all providers
        await self._match(db_item)
        db_item = await self.get_db_item(db_item.item_id)
        return db_item

    async def _get_provider_album_tracks(
        self,
        item_id: str,
        provider: Optional[ProviderType] = None,
        provider_id: Optional[str] = None,
    ) -> List[Track]:
        """Return album tracks for the given provider album id."""
        prov = self.mass.music.get_provider(provider_id or provider)
        if not prov:
            return []
        full_album = await self.get(item_id, provider, provider_id)
        # prefer cache items (if any)
        cache_key = f"{prov.type.value}.albumtracks.{item_id}"
        cache_checksum = full_album.metadata.checksum
        if cache := await self.mass.cache.get(cache_key, checksum=cache_checksum):
            return [Track.from_dict(x) for x in cache]
        # no items in cache - get listing from provider
        items = []
        for track in await prov.get_album_tracks(item_id):
            # make sure that the (full) album is stored on the tracks
            track.album = full_album
            if full_album.metadata.images:
                track.metadata.images = full_album.metadata.images
            items.append(track)
        # store (serializable items) in cache
        self.mass.create_task(
            self.mass.cache.set(
                cache_key, [x.to_dict() for x in items], checksum=cache_checksum
            )
        )
        return items

    async def _get_db_album_tracks(
        self,
        item_id: str,
    ) -> List[Track]:
        """Return in-database album tracks for the given database album."""
        album_tracks = []
        db_album = await self.get_db_item(item_id)
        # combine the info we have in the db with the full listing from a streaming provider
        for prov in db_album.provider_ids:
            for prov_track in await self._get_provider_album_tracks(
                prov.item_id, prov.prov_type, prov.prov_id
            ):
                if db_track := await self.mass.music.tracks.get_db_item_by_prov_id(
                    prov_track.item_id, prov_track.provider
                ):
                    if album_mapping := next(
                        (x for x in db_track.albums if x.item_id == db_album.item_id),
                        None,
                    ):
                        db_track.disc_number = album_mapping.disc_number
                        db_track.track_number = album_mapping.track_number
                    prov_track = db_track
                # make sure that the (db) album is stored on the tracks
                prov_track.album = db_album
                prov_track.metadata.images = db_album.metadata.images
                album_tracks.append(prov_track)
            # once we have the details from one streaming provider,
            # there is no need to iterate them all (if there are multiple)
            # for the same album
            if not prov.prov_type.is_file():
                break

        return album_tracks

    async def add_db_item(self, item: Album, overwrite_existing: bool = False) -> Album:
        """Add a new record to the database."""
        assert item.provider_ids, f"Album {item.name} is missing provider id(s)"
        assert item.artist, f"Album {item.name} is missing artist"
        async with self._db_add_lock:
            cur_item = None
            # always try to grab existing item by musicbrainz_id/upc
            if item.musicbrainz_id:
                match = {"musicbrainz_id": item.musicbrainz_id}
                cur_item = await self.mass.database.get_row(self.db_table, match)
            if not cur_item and item.upc:
                match = {"upc": item.upc}
                cur_item = await self.mass.database.get_row(self.db_table, match)
            if not cur_item:
                # fallback to search and match
                for row in await self.mass.database.search(self.db_table, item.name):
                    row_album = Album.from_db_row(row)
                    if compare_album(row_album, item):
                        cur_item = row_album
                        break
            if cur_item:
                # update existing
                return await self.update_db_item(
                    cur_item.item_id, item, overwrite=overwrite_existing
                )

            # insert new item
            album_artists = await self._get_album_artists(item, cur_item)
            if album_artists:
                sort_artist = album_artists[0].sort_name
            else:
                sort_artist = ""
            new_item = await self.mass.database.insert(
                self.db_table,
                {
                    **item.to_db_row(),
                    "artists": json_serializer(album_artists) or None,
                    "sort_artist": sort_artist,
                },
            )
            item_id = new_item["item_id"]
            self.logger.debug("added %s to database", item.name)
            # return created object
            db_item = await self.get_db_item(item_id)
            self.mass.signal_event(
                MassEvent(EventType.MEDIA_ITEM_ADDED, db_item.uri, db_item)
            )
            return db_item

    async def update_db_item(
        self,
        item_id: int,
        item: Album,
        overwrite: bool = False,
    ) -> Album:
        """Update Album record in the database."""
        assert item.provider_ids, f"Album {item.name} is missing provider id(s)"
        assert item.artist, f"Album {item.name} is missing artist"
        cur_item = await self.get_db_item(item_id)

        if overwrite:
            metadata = item.metadata
            metadata.last_refresh = None
            provider_ids = item.provider_ids
            album_artists = await self._get_album_artists(item, overwrite=True)
        else:
            metadata = cur_item.metadata.update(item.metadata)
            provider_ids = {*cur_item.provider_ids, *item.provider_ids}
            album_artists = await self._get_album_artists(item, cur_item)

        if item.album_type != AlbumType.UNKNOWN:
            album_type = item.album_type
        else:
            album_type = cur_item.album_type

        if album_artists:
            sort_artist = album_artists[0].sort_name
        else:
            sort_artist = ""

        await self.mass.database.update(
            self.db_table,
            {"item_id": item_id},
            {
                "name": item.name if overwrite else cur_item.name,
                "sort_name": item.sort_name if overwrite else cur_item.sort_name,
                "sort_artist": sort_artist,
                "version": item.version if overwrite else cur_item.version,
                "year": item.year or cur_item.year,
                "upc": item.upc or cur_item.upc,
                "album_type": album_type.value,
                "artists": json_serializer(album_artists) or None,
                "metadata": json_serializer(metadata),
                "provider_ids": json_serializer(provider_ids),
                "musicbrainz_id": item.musicbrainz_id or cur_item.musicbrainz_id,
            },
        )
        self.logger.debug("updated %s in database: %s", item.name, item_id)
        db_item = await self.get_db_item(item_id)
        self.mass.signal_event(
            MassEvent(EventType.MEDIA_ITEM_UPDATED, db_item.uri, db_item)
        )
        return db_item

    async def delete_db_item(self, item_id: int, recursive: bool = False) -> None:
        """Delete record from the database."""
        # check album tracks
        db_rows = await self.mass.database.get_rows_from_query(
            f"SELECT item_id FROM {TABLE_TRACKS} WHERE albums LIKE '%\"{item_id}\"%'",
            limit=5000,
        )
        assert not (db_rows and not recursive), "Tracks attached to album"
        for db_row in db_rows:
            await self.mass.music.albums.delete_db_item(db_row["item_id"], recursive)

        # delete the album itself from db
        await super().delete_db_item(item_id)

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
            if MusicProviderFeature.SEARCH not in provider.supported_features:
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
        overwrite: bool = False,
    ) -> List[ItemMapping]:
        """Extract (database) album artist(s) as ItemMapping."""
        album_artists = set()
        for album in (updated_album, db_album):
            if not album:
                continue
            for artist in album.artists:
                album_artists.add(await self._get_artist_mapping(artist, overwrite))
        # use intermediate set to prevent duplicates
        # filter various artists if multiple artists
        if len(album_artists) > 1:
            album_artists = {x for x in album_artists if x.name != FALLBACK_ARTIST}
        return list(album_artists)

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
