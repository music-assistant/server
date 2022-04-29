"""Manage MediaItems of type Artist."""

import asyncio
import itertools
from typing import List

from music_assistant.constants import EventType, MassEvent
from music_assistant.helpers.compare import (
    compare_album,
    compare_strings,
    compare_track,
)
from music_assistant.helpers.database import TABLE_ARTISTS
from music_assistant.helpers.json import json_serializer
from music_assistant.helpers.util import create_sort_name
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

    async def toptracks(self, item_id: str, provider_id: str) -> List[Track]:
        """Return top tracks for an artist."""
        artist = await self.get(item_id, provider_id)
        # get results from all providers
        # TODO: add db results
        return itertools.chain.from_iterable(
            await asyncio.gather(
                *[
                    self.get_provider_artist_toptracks(item.item_id, item.provider)
                    for item in artist.provider_ids
                ]
            )
        )

    async def albums(self, item_id: str, provider_id: str) -> List[Album]:
        """Return (all/most popular) albums for an artist."""
        artist = await self.get(item_id, provider_id)
        # get results from all providers
        # TODO: add db results
        return itertools.chain.from_iterable(
            await asyncio.gather(
                *[
                    self.get_provider_artist_albums(item.item_id, item.provider)
                    for item in artist.provider_ids
                ]
            )
        )

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
            db_artist.provider == "database"
        ), "Matching only supported for database items!"
        cur_providers = {item.provider for item in db_artist.provider_ids}
        for provider in self.mass.music.providers:
            if provider.id in cur_providers:
                continue
            if provider.id == "filesystem":
                continue
            if MediaType.ARTIST not in provider.supported_mediatypes:
                continue
            if not await self._match(db_artist, provider):
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

    async def add_db_item(self, artist: Artist) -> Artist:
        """Add a new artist record to the database."""
        assert artist.musicbrainz_id
        assert artist.name
        assert artist.provider_ids
        match = {"musicbrainz_id": artist.musicbrainz_id}
        if cur_item := await self.mass.database.get_row(self.db_table, match):
            # update existing
            return await self.update_db_item(cur_item["item_id"], artist)
        # insert artist
        async with self.mass.database.get_db() as _db:
            if not artist.sort_name:
                artist.sort_name = create_sort_name(artist.name)
            new_item = await self.mass.database.insert_or_replace(
                self.db_table, artist.to_db_row(), db=_db
            )
            item_id = new_item["item_id"]
            # store provider mappings
            await self.mass.music.set_provider_mappings(
                item_id, MediaType.ARTIST, artist.provider_ids, db=_db
            )
            self.logger.debug("added %s to database", artist.name)
            # return created object
            return await self.get_db_item(item_id, db=_db)

    async def update_db_item(
        self, item_id: int, artist: Artist, overwrite: bool = False
    ) -> Artist:
        """Update Artist record in the database."""
        cur_item = await self.get_db_item(item_id)
        if overwrite:
            metadata = artist.metadata
            provider_ids = artist.provider_ids
        else:
            metadata = cur_item.metadata.update(artist.metadata)
            provider_ids = {*cur_item.provider_ids, *artist.provider_ids}

        async with self.mass.database.get_db() as _db:
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
                db=_db,
            )
            await self.mass.music.set_provider_mappings(
                item_id, MediaType.ARTIST, provider_ids, db=_db
            )
            self.logger.debug("updated %s in database: %s", artist.name, item_id)
            return await self.get_db_item(item_id, db=_db)

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
            searchstr = f"{db_artist.name} {ref_track.name}"
            search_results = await self.mass.music.tracks.search(searchstr, provider.id)
            for search_result_item in search_results:
                if compare_track(search_result_item, ref_track):
                    # get matching artist from track
                    for search_item_artist in search_result_item.artists:
                        if compare_strings(db_artist.name, search_item_artist.name):
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
            searchstr = f"{db_artist.name} {ref_album.name}"
            search_result = await self.mass.music.albums.search(searchstr, provider.id)
            for search_result_item in search_result:
                # artist must match 100%
                if not compare_strings(db_artist.name, search_result_item.artist.name):
                    continue
                if compare_album(search_result_item, ref_album):
                    # 100% album match
                    # get full artist details so we have all metadata
                    prov_artist = await self.get_provider_item(
                        search_result_item.artist.item_id,
                        search_result_item.artist.provider,
                    )
                    await self.update_db_item(db_artist.item_id, prov_artist)
                    return True
        return False
