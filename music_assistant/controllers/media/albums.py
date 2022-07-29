"""Manage MediaItems of type Album."""
from __future__ import annotations

import asyncio
from random import choice, random
from typing import TYPE_CHECKING, List, Optional, Union

from music_assistant.constants import VARIOUS_ARTISTS
from music_assistant.controllers.database import TABLE_ALBUMS, TABLE_TRACKS
from music_assistant.controllers.media.base import MediaControllerBase
from music_assistant.helpers.compare import compare_album, loose_compare_strings
from music_assistant.helpers.json import json_serializer
from music_assistant.models.enums import EventType, MusicProviderFeature, ProviderType
from music_assistant.models.errors import (
    MediaNotFoundError,
    UnsupportedFeaturedException,
)
from music_assistant.models.event import MassEvent
from music_assistant.models.media_items import (
    Album,
    AlbumType,
    Artist,
    ItemMapping,
    MediaType,
    Track,
)

if TYPE_CHECKING:
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
        assert provider or provider_id, "Provider type or ID must be specified"
        album = await self.get(item_id, provider, provider_id)
        # perform a search on all provider(types) to collect all versions/variants
        prov_types = {item.type for item in self.mass.music.providers}
        search_query = f"{album.artist.name} - {album.name}"
        all_versions = {
            prov_item.item_id: prov_item
            for prov_items in await asyncio.gather(
                *[self.search(search_query, prov_type) for prov_type in prov_types]
            )
            for prov_item in prov_items
            if loose_compare_strings(album.name, prov_item.name)
        }
        # make sure that the 'base' version is included
        for prov_version in album.provider_ids:
            if prov_version.item_id in all_versions:
                continue
            album_copy = Album.from_dict(album.to_dict())
            album_copy.item_id = prov_version.item_id
            album_copy.provider = prov_version.prov_type
            album_copy.provider_ids = {prov_version}
            all_versions[prov_version.item_id] = album_copy

        # return the aggregated result
        return all_versions.values()

    async def add(self, item: Album) -> Album:
        """Add album to local db and return the database item."""
        # grab additional metadata
        await self.mass.metadata.get_album_metadata(item)
        existing = await self.get_db_item_by_prov_id(item.item_id, item.provider)
        if existing:
            db_item = await self.update_db_item(existing.item_id, item)
        else:
            db_item = await self.add_db_item(item)
        # also fetch same album on all providers
        await self._match(db_item)
        # return final db_item after all match/metadata actions
        db_item = await self.get_db_item(db_item.item_id)
        # dump album tracks in db
        for prov in db_item.provider_ids:
            for track in await self._get_provider_album_tracks(
                prov.item_id, prov.prov_id
            ):
                await self.mass.music.tracks.add_db_item(track)
        self.mass.signal_event(
            MassEvent(
                EventType.MEDIA_ITEM_UPDATED
                if existing
                else EventType.MEDIA_ITEM_ADDED,
                db_item.uri,
                db_item,
            )
        )
        return db_item

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
            return await self.get_db_item(item_id)

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
            metadata = cur_item.metadata.update(item.metadata, item.provider.is_file())
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
        return await self.get_db_item(item_id)

    async def delete_db_item(self, item_id: int, recursive: bool = False) -> None:
        """Delete record from the database."""
        # check album tracks
        db_rows = await self.mass.database.get_rows_from_query(
            f"SELECT item_id FROM {TABLE_TRACKS} WHERE albums LIKE '%\"{item_id}\"%'",
            limit=5000,
        )
        assert not (db_rows and not recursive), "Tracks attached to album"
        for db_row in db_rows:
            try:
                await self.mass.music.albums.delete_db_item(
                    db_row["item_id"], recursive
                )
            except MediaNotFoundError:
                pass

        # delete the album itself from db
        await super().delete_db_item(item_id)

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
        full_album = await self.get_provider_item(item_id, provider_id or provider)
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

    async def _get_provider_dynamic_tracks(
        self,
        item_id: str,
        provider: Optional[ProviderType] = None,
        provider_id: Optional[str] = None,
        limit: int = 25,
    ):
        """Generate a dynamic list of tracks based on the album content."""
        prov = self.mass.music.get_provider(provider_id or provider)
        if (
            not prov
            or MusicProviderFeature.SIMILAR_TRACKS not in prov.supported_features
        ):
            return []
        album_tracks = await self._get_provider_album_tracks(
            item_id=item_id, provider=provider, provider_id=provider_id
        )
        # Grab a random track from the album that we use to obtain similar tracks for
        track = choice(album_tracks)
        # Calculate no of songs to grab from each list at a 10/90 ratio
        total_no_of_tracks = limit + limit % 2
        no_of_album_tracks = int(total_no_of_tracks * 10 / 100)
        no_of_similar_tracks = int(total_no_of_tracks * 90 / 100)
        # Grab similar tracks from the music provider
        similar_tracks = await prov.get_similar_tracks(
            prov_track_id=track.item_id, limit=no_of_similar_tracks
        )
        # Merge album content with similar tracks
        dynamic_playlist = [
            *sorted(album_tracks, key=lambda n: random())[:no_of_album_tracks],
            *sorted(similar_tracks, key=lambda n: random())[:no_of_similar_tracks],
        ]
        return sorted(dynamic_playlist, key=lambda n: random())

    async def _get_dynamic_tracks(self, media_item: Album, limit=25) -> List[Track]:
        """Get dynamic list of tracks for given item, fallback/default implementation."""
        # TODO: query metadata provider(s) to get similar tracks (or tracks from similar artists)
        raise UnsupportedFeaturedException(
            "No Music Provider found that supports requesting similar tracks."
        )

    async def _get_db_album_tracks(
        self,
        item_id: str,
    ) -> List[Track]:
        """Return in-database album tracks for the given database album."""
        db_album = await self.get_db_item(item_id)
        # simply grab all tracks in the db that are linked to this album
        # TODO: adjust to json query instead of text search?
        query = f"SELECT * FROM tracks WHERE albums LIKE '%\"{item_id}\"%'"
        result = []
        for track in await self.mass.music.tracks.get_db_items_by_query(query):
            if album_mapping := next(
                (x for x in track.albums if x.item_id == db_album.item_id), None
            ):
                # make sure that the full album is set on the track and prefer the album's images
                track.album = db_album
                if db_album.metadata.images:
                    track.metadata.images = db_album.metadata.images
                # apply the disc and track number from the mapping
                track.disc_number = album_mapping.disc_number
                track.track_number = album_mapping.track_number
                result.append(track)
        return sorted(result, key=lambda x: (x.disc_number or 0, x.track_number or 0))

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
            album_artists = {x for x in album_artists if (x.name != VARIOUS_ARTISTS)}
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
