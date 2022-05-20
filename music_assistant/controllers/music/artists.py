"""Manage MediaItems of type Artist."""

import asyncio
import itertools
from typing import List, Optional

from databases import Database as Db

from music_assistant.helpers.compare import (
    compare_album,
    compare_artist,
    compare_strings,
    compare_track,
)
from music_assistant.helpers.database import TABLE_ARTISTS
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


class ArtistsController(MediaControllerBase[Artist]):
    """Controller managing MediaItems of type Artist."""

    db_table = TABLE_ARTISTS
    media_type = MediaType.ARTIST
    item_cls = Artist

    async def toptracks(
        self,
        item_id: str,
        provider: Optional[ProviderType] = None,
        provider_id: Optional[str] = None,
    ) -> List[Track]:
        """Return top tracks for an artist."""
        artist = await self.get(item_id, provider, provider_id)
        # get results from all providers
        coros = [
            self.get_provider_artist_toptracks(item.item_id, item.prov_id)
            for item in artist.provider_ids
        ]
        # use intermediate set to remove (some) duplicates
        return list(set(itertools.chain.from_iterable(await asyncio.gather(*coros))))

    async def albums(
        self,
        item_id: str,
        provider: Optional[ProviderType] = None,
        provider_id: Optional[str] = None,
    ) -> List[Album]:
        """Return (all/most popular) albums for an artist."""
        artist = await self.get(item_id, provider, provider_id)
        # get results from all providers
        coros = [
            self.get_provider_artist_albums(item.item_id, item.prov_id)
            for item in artist.provider_ids
        ]
        # use intermediate set to remove (some) duplicates
        return list(set(itertools.chain.from_iterable(await asyncio.gather(*coros))))

    async def add(self, item: Artist) -> Artist:
        """Add artist to local db and return the database item."""
        # grab musicbrainz id and additional metadata
        await self.mass.metadata.get_artist_metadata(item)
        db_item = await self.add_db_item(item)
        # also fetch same artist on all providers
        await self.match_artist(db_item)
        db_item = await self.get_db_item(db_item.item_id)
        self.mass.signal_event(
            MassEvent(EventType.ARTIST_ADDED, object_id=db_item.uri, data=db_item)
        )
        return db_item

    async def match_artist(self, db_artist: Artist):
        """
        Try to find matching artists on all providers for the provided (database) item_id.

        This is used to link objects of different providers together.
        """
        assert (
            db_artist.provider == ProviderType.DATABASE
        ), "Matching only supported for database items!"
        cur_prov_types = {x.prov_type for x in db_artist.provider_ids}
        for provider in self.mass.music.providers:
            if provider.type in cur_prov_types:
                continue
            if MediaType.ARTIST not in provider.supported_mediatypes:
                continue
            if await self._match(db_artist, provider):
                cur_prov_types.add(provider.type)
            else:
                self.logger.debug(
                    "Could not find match for Artist %s on provider %s",
                    db_artist.name,
                    provider.name,
                )

    async def get_provider_artist_toptracks(
        self, item_id: str, provider_id: str
    ) -> List[Track]:
        """Return top tracks for an artist on given provider."""
        provider = self.mass.music.get_provider(provider_id)
        if not provider:
            return []
        return await provider.get_artist_toptracks(item_id)

    async def get_provider_artist_albums(
        self, item_id: str, provider_id: str
    ) -> List[Album]:
        """Return albums for an artist on given provider."""
        provider = self.mass.music.get_provider(provider_id)
        if not provider:
            return []
        return await provider.get_artist_albums(item_id)

    async def add_db_item(self, artist: Artist, db: Optional[Db] = None) -> Artist:
        """Add a new artist record to the database."""
        assert artist.provider_ids, "Album is missing provider id(s)"
        async with self.mass.database.get_db(db) as db:
            # always try to grab existing item by musicbrainz_id
            cur_item = None
            if artist.musicbrainz_id:
                match = {"musicbrainz_id": artist.musicbrainz_id}
                cur_item = await self.mass.database.get_row(self.db_table, match, db=db)
            if not cur_item:
                # fallback to matching
                # NOTE: we match an artist by name which could theoretically lead to collisions
                # but the chance is so small it is not worth the additional overhead of grabbing
                # the musicbrainz id upfront
                match = {"sort_name": artist.sort_name}
                for row in await self.mass.database.get_rows(
                    self.db_table, match, db=db
                ):
                    row_artist = Artist.from_db_row(row)
                    if compare_strings(row_artist.sort_name, artist.sort_name):
                        cur_item = row_artist
                        break
            if cur_item:
                # update existing
                return await self.update_db_item(cur_item.item_id, artist, db=db)

            # insert artist
            new_item = await self.mass.database.insert(
                self.db_table, artist.to_db_row(), db=db
            )
            item_id = new_item["item_id"]
            self.logger.debug("added %s to database", artist.name)
            # return created object
            return await self.get_db_item(item_id, db=db)

    async def update_db_item(
        self,
        item_id: int,
        artist: Artist,
        overwrite: bool = False,
        db: Optional[Db] = None,
    ) -> Artist:
        """Update Artist record in the database."""
        cur_item = await self.get_db_item(item_id)
        if overwrite:
            metadata = artist.metadata
            provider_ids = artist.provider_ids
        else:
            metadata = cur_item.metadata.update(artist.metadata)
            provider_ids = {*cur_item.provider_ids, *artist.provider_ids}

        async with self.mass.database.get_db(db) as db:
            await self.mass.database.update(
                self.db_table,
                {"item_id": item_id},
                {
                    "name": artist.name if overwrite else cur_item.name,
                    "sort_name": artist.sort_name if overwrite else cur_item.sort_name,
                    "musicbrainz_id": artist.musicbrainz_id or cur_item.musicbrainz_id,
                    "metadata": json_serializer(metadata),
                    "provider_ids": json_serializer(provider_ids),
                },
                db=db,
            )
            self.logger.debug("updated %s in database: %s", artist.name, item_id)
            return await self.get_db_item(item_id, db=db)

    async def _match(self, db_artist: Artist, provider: MusicProvider) -> bool:
        """Try to find matching artists on given provider for the provided (database) artist."""
        self.logger.debug(
            "Trying to match artist %s on provider %s", db_artist.name, provider.name
        )
        # try to get a match with some reference tracks of this artist
        for ref_track in await self.toptracks(db_artist.item_id, db_artist.provider):
            # make sure we have a full track
            if isinstance(ref_track.album, ItemMapping):
                ref_track = await self.mass.music.tracks.get(
                    ref_track.item_id, ref_track.provider
                )
            search_results = await self.mass.music.tracks.search(
                ref_track.name, provider.type
            )
            for search_result_item in search_results:
                if not compare_track(search_result_item, ref_track):
                    continue
                # get matching artist from track
                for search_item_artist in search_result_item.artists:
                    if not compare_artist(db_artist, search_item_artist):
                        continue
                    # 100% album match
                    # get full artist details so we have all metadata
                    prov_artist = await self.get_provider_item(
                        search_item_artist.item_id, search_item_artist.provider
                    )
                    await self.update_db_item(db_artist.item_id, prov_artist)
                    return True
        # try to get a match with some reference albums of this artist
        artist_albums = await self.albums(db_artist.item_id, db_artist.provider)
        for ref_album in artist_albums:
            if ref_album.album_type == AlbumType.COMPILATION:
                continue
            search_result = await self.mass.music.albums.search(
                ref_album.name, provider.type
            )
            for search_result_item in search_result:
                # artist must match 100%
                if not compare_artist(db_artist, search_result_item.artist):
                    continue
                if not compare_album(search_result_item, ref_album):
                    continue
                # 100% match
                # get full artist details so we have all metadata
                prov_artist = await self.get_provider_item(
                    search_result_item.artist.item_id,
                    search_result_item.artist.provider,
                )
                await self.update_db_item(db_artist.item_id, prov_artist)
                return True
        return False
