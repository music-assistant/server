"""Manage MediaItems of type Album."""

from __future__ import annotations

import asyncio
import contextlib
from random import choice, random
from typing import TYPE_CHECKING

from music_assistant.common.helpers.datetime import utc_timestamp
from music_assistant.common.helpers.json import serialize_to_json
from music_assistant.common.models.enums import EventType, ProviderFeature
from music_assistant.common.models.errors import (
    InvalidDataError,
    MediaNotFoundError,
    UnsupportedFeaturedException,
)
from music_assistant.common.models.media_items import (
    Album,
    AlbumTrack,
    AlbumType,
    ItemMapping,
    MediaType,
    Track,
)
from music_assistant.constants import DB_TABLE_ALBUM_TRACKS, DB_TABLE_ALBUMS, DB_TABLE_TRACKS
from music_assistant.server.controllers.media.base import MediaControllerBase
from music_assistant.server.helpers.compare import (
    compare_album,
    compare_artists,
    loose_compare_strings,
)

if TYPE_CHECKING:
    from music_assistant.server.models.music_provider import MusicProvider


class AlbumsController(MediaControllerBase[Album]):
    """Controller managing MediaItems of type Album."""

    db_table = DB_TABLE_ALBUMS
    media_type = MediaType.ALBUM
    item_cls = Album

    def __init__(self, *args, **kwargs) -> None:
        """Initialize class."""
        super().__init__(*args, **kwargs)
        self._db_add_lock = asyncio.Lock()
        # register api handlers
        self.mass.register_api_command("music/albums/library_items", self.library_items)
        self.mass.register_api_command(
            "music/albums/update_item_in_library", self.update_item_in_library
        )
        self.mass.register_api_command(
            "music/albums/remove_item_from_library", self.remove_item_from_library
        )
        self.mass.register_api_command("music/albums/get_album", self.get)
        self.mass.register_api_command("music/albums/album_tracks", self.tracks)
        self.mass.register_api_command("music/albums/album_versions", self.versions)

    async def get(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
        force_refresh: bool = False,
        lazy: bool = True,
        details: Album | ItemMapping = None,
        add_to_library: bool = False,
    ) -> Album:
        """Return (full) details for a single media item."""
        album = await super().get(
            item_id,
            provider_instance_id_or_domain,
            force_refresh=force_refresh,
            lazy=lazy,
            details=details,
            add_to_library=add_to_library,
        )
        # append full artist details to full album item
        album.artists = [
            await self.mass.music.artists.get(
                item.item_id,
                item.provider,
                lazy=lazy,
                details=item,
                add_to_library=add_to_library,
            )
            for item in album.artists
        ]
        return album

    async def add_item_to_library(
        self,
        item: Album,
        metadata_lookup: bool = True,
        add_album_tracks: bool = False,
    ) -> Album:
        """Add album to library and return the database item."""
        if not isinstance(item, Album):
            msg = "Not a valid Album object (ItemMapping can not be added to db)"
            raise InvalidDataError(msg)
        if not item.provider_mappings:
            msg = "Album is missing provider mapping(s)"
            raise InvalidDataError(msg)
        # grab additional metadata
        if metadata_lookup:
            await self.mass.metadata.get_album_metadata(item)
        # check for existing item first
        library_item = None
        if cur_item := await self.get_library_item_by_prov_id(item.item_id, item.provider):
            # existing item match by provider id
            library_item = await self.update_item_in_library(cur_item.item_id, item)
        elif cur_item := await self.get_library_item_by_external_ids(item.external_ids):
            # existing item match by external id
            library_item = await self.update_item_in_library(cur_item.item_id, item)
        else:
            # search by name
            async for db_item in self.iter_library_items(search=item.name):
                if compare_album(db_item, item):
                    # existing item found: update it
                    library_item = await self.update_item_in_library(db_item.item_id, item)
                    break
        if not library_item:
            # actually add a new item in the library db
            # use the lock to prevent a race condition of the same item being added twice
            async with self._db_add_lock:
                library_item = await self._add_library_item(item)
        # also fetch the same album on all providers
        if metadata_lookup:
            await self._match(library_item)
            library_item = await self.get_library_item(library_item.item_id)
        # also add album tracks
        if add_album_tracks and item.provider != "library":
            async with asyncio.TaskGroup() as tg:
                for track in await self._get_provider_album_tracks(item.item_id, item.provider):
                    track.album = library_item
                    tg.create_task(
                        self.mass.music.tracks.add_item_to_library(track, metadata_lookup=False)
                    )
        self.mass.signal_event(
            EventType.MEDIA_ITEM_ADDED,
            library_item.uri,
            library_item,
        )
        return library_item

    async def update_item_in_library(
        self, item_id: str | int, update: Album, overwrite: bool = False
    ) -> Album:
        """Update existing record in the database."""
        db_id = int(item_id)  # ensure integer
        cur_item = await self.get_library_item(db_id)
        metadata = cur_item.metadata.update(getattr(update, "metadata", None), overwrite)
        provider_mappings = self._get_provider_mappings(cur_item, update, overwrite)
        album_artists = await self._get_artist_mappings(cur_item, update, overwrite)
        if getattr(update, "album_type", AlbumType.UNKNOWN) != AlbumType.UNKNOWN:
            album_type = update.album_type
        else:
            album_type = cur_item.album_type
        sort_artist = album_artists[0].sort_name
        cur_item.external_ids.update(update.external_ids)
        await self.mass.music.database.update(
            self.db_table,
            {"item_id": db_id},
            {
                "name": update.name if overwrite else cur_item.name,
                "sort_name": update.sort_name if overwrite else cur_item.sort_name,
                "sort_artist": sort_artist,
                "version": update.version if overwrite else cur_item.version,
                "year": update.year if overwrite else cur_item.year or update.year,
                "album_type": album_type.value,
                "artists": serialize_to_json(album_artists),
                "metadata": serialize_to_json(metadata),
                "provider_mappings": serialize_to_json(provider_mappings),
                "external_ids": serialize_to_json(
                    update.external_ids if overwrite else cur_item.external_ids
                ),
                "timestamp_modified": int(utc_timestamp()),
            },
        )
        # update/set provider_mappings table
        await self._set_provider_mappings(db_id, provider_mappings)
        self.logger.debug("updated %s in database: %s", update.name, db_id)
        # get full created object
        library_item = await self.get_library_item(db_id)
        self.mass.signal_event(
            EventType.MEDIA_ITEM_UPDATED,
            library_item.uri,
            library_item,
        )
        # return the full item we just updated
        return library_item

    async def remove_item_from_library(self, item_id: str | int) -> None:
        """Delete record from the database."""
        db_id = int(item_id)  # ensure integer
        # recursively also remove album tracks
        for db_track in await self._get_db_album_tracks(db_id):
            with contextlib.suppress(MediaNotFoundError):
                await self.mass.music.tracks.remove_item_from_library(db_track.item_id)
        # delete entry(s) from albumtracks table
        await self.mass.music.database.delete(DB_TABLE_ALBUM_TRACKS, {"album_id": db_id})
        # delete the album itself from db
        await super().remove_item_from_library(item_id)

    async def tracks(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
    ) -> list[Track]:
        """Return album tracks for the given provider album id."""
        if provider_instance_id_or_domain == "library":
            return await self._get_db_album_tracks(item_id)
        # return provider album tracks
        return await self._get_provider_album_tracks(item_id, provider_instance_id_or_domain)

    async def versions(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
    ) -> list[Album]:
        """Return all versions of an album we can find on all providers."""
        album = await self.get(item_id, provider_instance_id_or_domain, add_to_library=False)
        search_query = f"{album.artists[0].name} - {album.name}"
        result: list[Album] = []
        for provider_id in self.mass.music.get_unique_providers():
            provider = self.mass.get_provider(provider_id)
            if not provider:
                continue
            if not provider.library_supported(MediaType.ALBUM):
                continue
            result += [
                prov_item
                for prov_item in await self.search(search_query, provider_id)
                if loose_compare_strings(album.name, prov_item.name)
                and compare_artists(prov_item.artists, album.artists, any_match=True)
                # make sure that the 'base' version is NOT included
                and prov_item.item_id != item_id
            ]
        return result

    async def _add_library_item(self, item: Album) -> Album:
        """Add a new record to the database."""
        album_artists = await self._get_artist_mappings(item)
        sort_artist = album_artists[0].sort_name if album_artists else ""
        new_item = await self.mass.music.database.insert(
            self.db_table,
            {
                "name": item.name,
                "sort_name": item.sort_name,
                "version": item.version,
                "favorite": item.favorite,
                "album_type": item.album_type,
                "year": item.year,
                "metadata": serialize_to_json(item.metadata),
                "provider_mappings": serialize_to_json(item.provider_mappings),
                "artists": serialize_to_json(album_artists),
                "sort_artist": sort_artist,
                "external_ids": serialize_to_json(item.external_ids),
                "timestamp_added": int(utc_timestamp()),
                "timestamp_modified": int(utc_timestamp()),
            },
        )
        db_id = new_item["item_id"]
        # update/set provider_mappings table
        await self._set_provider_mappings(db_id, item.provider_mappings)
        self.logger.debug("added %s to database", item.name)
        # return the full item we just added
        return await self.get_library_item(db_id)

    async def _get_provider_album_tracks(
        self, item_id: str, provider_instance_id_or_domain: str
    ) -> list[AlbumTrack]:
        """Return album tracks for the given provider album id."""
        assert provider_instance_id_or_domain != "library"
        prov = self.mass.get_provider(provider_instance_id_or_domain)
        if prov is None:
            return []

        full_album = await self.get_provider_item(item_id, provider_instance_id_or_domain)
        # prefer cache items (if any) for streaming providers only
        cache_key = f"{prov.instance_id}.albumtracks.{item_id}"
        if isinstance(full_album, ItemMapping):
            cache_checksum = None
        else:
            cache_checksum = full_album.metadata.checksum
        if prov.is_streaming_provider and (
            cache := await self.mass.cache.get(cache_key, checksum=cache_checksum)
        ):
            return [AlbumTrack.from_dict(x) for x in cache]
        # no items in cache - get listing from provider
        items = []
        for track in await prov.get_album_tracks(item_id):
            assert isinstance(track, AlbumTrack)
            # make sure that the (full) album is stored on the tracks
            track.album = full_album
            if not isinstance(full_album, ItemMapping) and full_album.metadata.images:
                track.metadata.images = full_album.metadata.images
            items.append(track)
        # store (serializable items) in cache
        if prov.is_streaming_provider:
            self.mass.create_task(
                self.mass.cache.set(
                    cache_key, [x.to_dict() for x in items], checksum=cache_checksum
                )
            )
        return items

    async def _get_provider_dynamic_tracks(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
        limit: int = 25,
    ):
        """Generate a dynamic list of tracks based on the album content."""
        assert provider_instance_id_or_domain != "library"
        prov = self.mass.get_provider(provider_instance_id_or_domain)
        if prov is None:
            return []
        if ProviderFeature.SIMILAR_TRACKS not in prov.supported_features:
            return []
        album_tracks = await self._get_provider_album_tracks(
            item_id, provider_instance_id_or_domain
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
        # ruff: noqa: ARG005
        dynamic_playlist = [
            *sorted(album_tracks, key=lambda n: random())[:no_of_album_tracks],
            *sorted(similar_tracks, key=lambda n: random())[:no_of_similar_tracks],
        ]
        return sorted(dynamic_playlist, key=lambda n: random())

    async def _get_dynamic_tracks(
        self,
        media_item: Album,
        limit: int = 25,
    ) -> list[Track]:
        """Get dynamic list of tracks for given item, fallback/default implementation."""
        # TODO: query metadata provider(s) to get similar tracks (or tracks from similar artists)
        msg = "No Music Provider found that supports requesting similar tracks."
        raise UnsupportedFeaturedException(msg)

    async def _get_db_album_tracks(
        self,
        item_id: str | int,
    ) -> list[AlbumTrack]:
        """Return in-database album tracks for the given database album."""
        db_id = int(item_id)  # ensure integer
        db_album = await self.get_library_item(db_id)
        result: list[AlbumTrack] = []
        query = (
            f"SELECT * FROM {DB_TABLE_TRACKS} INNER JOIN albumtracks "
            "ON albumtracks.track_id = tracks.item_id WHERE albumtracks.album_id = :album_id"
        )
        track_rows = await self.mass.music.database.get_rows_from_query(query, {"album_id": db_id})
        for album_track_row in track_rows:
            album_track = AlbumTrack.from_dict(
                self._parse_db_row({**album_track_row, "album": db_album.to_dict()})
            )
            if db_album.metadata.images:
                album_track.metadata.images = db_album.metadata.images
            result.append(album_track)
        return sorted(result, key=lambda x: (x.disc_number, x.track_number))

    async def _match(self, db_album: Album) -> None:
        """Try to find match on all (streaming) providers for the provided (database) album.

        This is used to link objects of different providers/qualities together.
        """
        if db_album.provider != "library":
            return  # Matching only supported for database items
        artist_name = db_album.artists[0].name

        async def find_prov_match(provider: MusicProvider):
            self.logger.debug(
                "Trying to match album %s on provider %s", db_album.name, provider.name
            )
            match_found = False
            for search_str in (
                db_album.name,
                f"{artist_name} - {db_album.name}",
                f"{artist_name} {db_album.name}",
            ):
                if match_found:
                    break
                search_result = await self.search(search_str, provider.instance_id)
                for search_result_item in search_result:
                    if not search_result_item.available:
                        continue
                    if not compare_album(db_album, search_result_item):
                        continue
                    # we must fetch the full album version, search results are simplified objects
                    prov_album = await self.get_provider_item(
                        search_result_item.item_id,
                        search_result_item.provider,
                        fallback=search_result_item,
                    )
                    if compare_album(db_album, prov_album):
                        # 100% match, we update the db with the additional provider mapping(s)
                        match_found = True
                        for provider_mapping in search_result_item.provider_mappings:
                            await self.add_provider_mapping(db_album.item_id, provider_mapping)
            return match_found

        # try to find match on all providers
        cur_provider_domains = {x.provider_domain for x in db_album.provider_mappings}
        for provider in self.mass.music.providers:
            if provider.domain in cur_provider_domains:
                continue
            if ProviderFeature.SEARCH not in provider.supported_features:
                continue
            if not provider.library_supported(MediaType.ALBUM):
                continue
            if not provider.is_streaming_provider:
                # matching on unique providers is pointless as they push (all) their content to MA
                continue
            if await find_prov_match(provider):
                cur_provider_domains.add(provider.domain)
            else:
                self.logger.debug(
                    "Could not find match for Album %s on provider %s",
                    db_album.name,
                    provider.name,
                )
