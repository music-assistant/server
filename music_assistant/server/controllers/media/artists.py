"""Manage MediaItems of type Artist."""

from __future__ import annotations

import asyncio
import contextlib
from random import choice, random
from typing import TYPE_CHECKING, Any

from music_assistant.common.helpers.datetime import utc_timestamp
from music_assistant.common.helpers.json import serialize_to_json
from music_assistant.common.models.enums import EventType, ProviderFeature
from music_assistant.common.models.errors import MediaNotFoundError, UnsupportedFeaturedException
from music_assistant.common.models.media_items import (
    Album,
    AlbumType,
    Artist,
    ItemMapping,
    MediaType,
    PagedItems,
    Track,
)
from music_assistant.constants import (
    DB_TABLE_ALBUMS,
    DB_TABLE_ARTISTS,
    DB_TABLE_TRACKS,
    VARIOUS_ARTISTS_ID_MBID,
    VARIOUS_ARTISTS_NAME,
)
from music_assistant.server.controllers.media.base import MediaControllerBase
from music_assistant.server.helpers.compare import compare_artist, compare_strings

if TYPE_CHECKING:
    from music_assistant.server.models.music_provider import MusicProvider


class ArtistsController(MediaControllerBase[Artist]):
    """Controller managing MediaItems of type Artist."""

    db_table = DB_TABLE_ARTISTS
    media_type = MediaType.ARTIST
    item_cls = Artist

    def __init__(self, *args, **kwargs) -> None:
        """Initialize class."""
        super().__init__(*args, **kwargs)
        self._db_add_lock = asyncio.Lock()
        # register api handlers
        self.mass.register_api_command("music/artists/library_items", self.library_items)
        self.mass.register_api_command(
            "music/artists/update_item_in_library", self.update_item_in_library
        )
        self.mass.register_api_command(
            "music/artists/remove_item_from_library", self.remove_item_from_library
        )
        self.mass.register_api_command("music/artists/get_artist", self.get)
        self.mass.register_api_command("music/artists/artist_albums", self.albums)
        self.mass.register_api_command("music/artists/artist_tracks", self.tracks)

    async def add_item_to_library(
        self,
        item: Artist | ItemMapping,
        metadata_lookup: bool = True,
    ) -> Artist:
        """Add artist to library and return the database item."""
        if isinstance(item, ItemMapping):
            metadata_lookup = False
            item = Artist.from_item_mapping(item)
        # grab musicbrainz id and additional metadata
        if metadata_lookup:
            await self.mass.metadata.get_artist_metadata(item)
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
                if compare_artist(db_item, item):
                    # existing item found: update it
                    # NOTE: if we matched an artist by name this could theoretically lead to
                    # collisions but the chance is so small it is not worth the additional
                    # overhead of grabbing the musicbrainz id upfront
                    library_item = await self.update_item_in_library(db_item.item_id, item)
                    break
        if not library_item:
            # actually add (or update) the item in the library db
            # use the lock to prevent a race condition of the same item being added twice
            async with self._db_add_lock:
                library_item = await self._add_library_item(item)
        # also fetch same artist on all providers
        if metadata_lookup:
            await self.match_artist(library_item)
            library_item = await self.get_library_item(library_item.item_id)
        self.mass.signal_event(
            EventType.MEDIA_ITEM_ADDED,
            library_item.uri,
            library_item,
        )
        # return final library_item after all match/metadata actions
        return library_item

    async def update_item_in_library(
        self, item_id: str | int, update: Artist, overwrite: bool = False
    ) -> Artist:
        """Update existing record in the database."""
        db_id = int(item_id)  # ensure integer
        cur_item = await self.get_library_item(db_id)
        metadata = cur_item.metadata.update(getattr(update, "metadata", None), overwrite)
        provider_mappings = self._get_provider_mappings(cur_item, update, overwrite)
        cur_item.external_ids.update(update.external_ids)
        # enforce various artists name + id
        mbid = cur_item.mbid
        if (not mbid or overwrite) and getattr(update, "mbid", None):
            if compare_strings(update.name, VARIOUS_ARTISTS_NAME):
                update.mbid = VARIOUS_ARTISTS_ID_MBID
            if update.mbid == VARIOUS_ARTISTS_ID_MBID:
                update.name = VARIOUS_ARTISTS_NAME
        await self.mass.music.database.update(
            self.db_table,
            {"item_id": db_id},
            {
                "name": update.name if overwrite else cur_item.name,
                "sort_name": update.sort_name if overwrite else cur_item.sort_name,
                "external_ids": serialize_to_json(
                    update.external_ids if overwrite else cur_item.external_ids
                ),
                "metadata": serialize_to_json(metadata),
                "provider_mappings": serialize_to_json(provider_mappings),
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

    async def library_items(
        self,
        favorite: bool | None = None,
        search: str | None = None,
        limit: int = 500,
        offset: int = 0,
        order_by: str = "sort_name",
        extra_query: str | None = None,
        extra_query_params: dict[str, Any] | None = None,
        album_artists_only: bool = False,
    ) -> PagedItems:
        """Get in-database (album) artists."""
        if album_artists_only:
            artist_query = "artists.sort_name in (select albums.sort_artist from albums)"
            extra_query = f"{extra_query} AND {artist_query}" if extra_query else artist_query
        return await super().library_items(
            favorite=favorite,
            search=search,
            limit=limit,
            offset=offset,
            order_by=order_by,
            extra_query=extra_query,
            extra_query_params=extra_query_params,
        )

    async def tracks(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
    ) -> list[Track]:
        """Return all/top tracks for an artist."""
        if provider_instance_id_or_domain == "library":
            return await self.get_library_artist_tracks(
                item_id,
            )
        return await self.get_provider_artist_toptracks(
            item_id,
            provider_instance_id_or_domain,
        )

    async def albums(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
    ) -> list[Album]:
        """Return (all/most popular) albums for an artist."""
        if provider_instance_id_or_domain == "library":
            return await self.get_library_artist_albums(
                item_id,
            )
        return await self.get_provider_artist_albums(
            item_id,
            provider_instance_id_or_domain,
        )

    async def remove_item_from_library(self, item_id: str | int) -> None:
        """Delete record from the database."""
        db_id = int(item_id)  # ensure integer
        # recursively also remove artist albums
        for db_row in await self.mass.music.database.get_rows_from_query(
            f"SELECT item_id FROM {DB_TABLE_ALBUMS} WHERE artists LIKE '%\"{db_id}\"%'",
            limit=5000,
        ):
            with contextlib.suppress(MediaNotFoundError):
                await self.mass.music.albums.remove_item_from_library(db_row["item_id"])

        # recursively also remove artist tracks
        for db_row in await self.mass.music.database.get_rows_from_query(
            f"SELECT item_id FROM {DB_TABLE_TRACKS} WHERE artists LIKE '%\"{db_id}\"%'",
            limit=5000,
        ):
            with contextlib.suppress(MediaNotFoundError):
                await self.mass.music.tracks.remove_item_from_library(db_row["item_id"])

        # delete the artist itself from db
        await super().remove_item_from_library(db_id)

    async def match_artist(self, db_artist: Artist) -> None:
        """Try to find matching artists on all providers for the provided (database) item_id.

        This is used to link objects of different providers together.
        """
        assert db_artist.provider == "library", "Matching only supported for database items!"
        cur_provider_domains = {x.provider_domain for x in db_artist.provider_mappings}
        for provider in self.mass.music.providers:
            if provider.domain in cur_provider_domains:
                continue
            if ProviderFeature.SEARCH not in provider.supported_features:
                continue
            if not provider.library_supported(MediaType.ARTIST):
                continue
            if not provider.is_streaming_provider:
                # matching on unique providers is pointless as they push (all) their content to MA
                continue
            if await self._match(db_artist, provider):
                cur_provider_domains.add(provider.domain)
            else:
                self.logger.debug(
                    "Could not find match for Artist %s on provider %s",
                    db_artist.name,
                    provider.name,
                )

    async def get_provider_artist_toptracks(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
    ) -> list[Track]:
        """Return top tracks for an artist on given provider."""
        items = []
        assert provider_instance_id_or_domain != "library"
        artist = await self.get(item_id, provider_instance_id_or_domain, add_to_library=False)
        cache_checksum = artist.metadata.checksum
        prov = self.mass.get_provider(provider_instance_id_or_domain)
        if prov is None:
            return []
        # prefer cache items (if any)
        cache_key = f"{prov.instance_id}.artist_toptracks.{item_id}"
        if cache := await self.mass.cache.get(cache_key, checksum=cache_checksum):
            return [Track.from_dict(x) for x in cache]
        # no items in cache - get listing from provider
        if ProviderFeature.ARTIST_TOPTRACKS in prov.supported_features:
            items = await prov.get_artist_toptracks(item_id)
        else:
            # fallback implementation using the db
            if db_artist := await self.mass.music.artists.get_library_item_by_prov_id(
                item_id,
                provider_instance_id_or_domain,
            ):
                # TODO: adjust to json query instead of text search?
                query = f"WHERE tracks.artists LIKE '%\"{db_artist.item_id}\"%'"
                query += (
                    f" AND tracks.provider_mappings LIKE '%\"{provider_instance_id_or_domain}\"%'"
                )
                paged_list = await self.mass.music.tracks.library_items(extra_query=query)
                return paged_list.items
        # store (serializable items) in cache
        self.mass.create_task(
            self.mass.cache.set(cache_key, [x.to_dict() for x in items], checksum=cache_checksum)
        )
        return items

    async def get_library_artist_tracks(
        self,
        item_id: str | int,
    ) -> list[Track]:
        """Return all tracks for an artist in the library."""
        # TODO: adjust to json query instead of text search?
        query = f"WHERE tracks.artists LIKE '%\"{item_id}\"%'"
        paged_list = await self.mass.music.tracks.library_items(extra_query=query)
        return paged_list.items

    async def get_provider_artist_albums(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
    ) -> list[Album]:
        """Return albums for an artist on given provider."""
        items = []
        assert provider_instance_id_or_domain != "library"
        artist = await self.get_provider_item(item_id, provider_instance_id_or_domain)
        cache_checksum = artist.metadata.checksum
        prov = self.mass.get_provider(provider_instance_id_or_domain)
        if prov is None:
            return []
        # prefer cache items (if any)
        cache_key = f"{prov.instance_id}.artist_albums.{item_id}"
        if cache := await self.mass.cache.get(cache_key, checksum=cache_checksum):
            return [Album.from_dict(x) for x in cache]
        # no items in cache - get listing from provider
        if ProviderFeature.ARTIST_ALBUMS in prov.supported_features:
            items = await prov.get_artist_albums(item_id)
        else:
            # fallback implementation using the db
            # ruff: noqa: PLR5501
            if db_artist := await self.mass.music.artists.get_library_item_by_prov_id(
                item_id,
                provider_instance_id_or_domain,
            ):
                # TODO: adjust to json query instead of text search?
                query = f"WHERE albums.artists LIKE '%\"{db_artist.item_id}\"%'"
                query += (
                    f" AND albums.provider_mappings LIKE '%\"{provider_instance_id_or_domain}\"%'"
                )
                paged_list = await self.mass.music.albums.library_items(extra_query=query)
                return paged_list.items
        # store (serializable items) in cache
        self.mass.create_task(
            self.mass.cache.set(cache_key, [x.to_dict() for x in items], checksum=cache_checksum)
        )
        return items

    async def get_library_artist_albums(
        self,
        item_id: str | int,
    ) -> list[Album]:
        """Return all in-library albums for an artist."""
        # TODO: adjust to json query instead of text search?
        query = f"WHERE albums.artists LIKE '%\"{item_id}\"%'"
        paged_list = await self.mass.music.albums.library_items(extra_query=query)
        return paged_list.items

    async def _add_library_item(self, item: Artist) -> Artist:
        """Add a new item record to the database."""
        # enforce various artists name + id
        if compare_strings(item.name, VARIOUS_ARTISTS_NAME):
            item.mbid = VARIOUS_ARTISTS_ID_MBID
        if item.mbid == VARIOUS_ARTISTS_ID_MBID:
            item.name = VARIOUS_ARTISTS_NAME
        # no existing item matched: insert item
        item.timestamp_added = int(utc_timestamp())
        item.timestamp_modified = int(utc_timestamp())
        new_item = await self.mass.music.database.insert(
            self.db_table,
            {
                "name": item.name,
                "sort_name": item.sort_name,
                "favorite": item.favorite,
                "external_ids": serialize_to_json(item.external_ids),
                "metadata": serialize_to_json(item.metadata),
                "provider_mappings": serialize_to_json(item.provider_mappings),
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

    async def _get_provider_dynamic_tracks(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
        limit: int = 25,
    ):
        """Generate a dynamic list of tracks based on the artist's top tracks."""
        assert provider_instance_id_or_domain != "library"
        prov = self.mass.get_provider(provider_instance_id_or_domain)
        if prov is None:
            return []
        if ProviderFeature.SIMILAR_TRACKS not in prov.supported_features:
            return []
        top_tracks = await self.get_provider_artist_toptracks(
            item_id,
            provider_instance_id_or_domain,
        )
        # Grab a random track from the album that we use to obtain similar tracks for
        track = choice(top_tracks)
        # Calculate no of songs to grab from each list at a 10/90 ratio
        total_no_of_tracks = limit + limit % 2
        no_of_artist_tracks = int(total_no_of_tracks * 10 / 100)
        no_of_similar_tracks = int(total_no_of_tracks * 90 / 100)
        # Grab similar tracks from the music provider
        similar_tracks = await prov.get_similar_tracks(
            prov_track_id=track.item_id, limit=no_of_similar_tracks
        )
        # Merge album content with similar tracks
        dynamic_playlist = [
            *sorted(top_tracks, key=lambda _: random())[:no_of_artist_tracks],
            *sorted(similar_tracks, key=lambda _: random())[:no_of_similar_tracks],
        ]
        return sorted(dynamic_playlist, key=lambda n: random())  # noqa: ARG005

    async def _get_dynamic_tracks(
        self,
        media_item: Artist,
        limit: int = 25,
    ) -> list[Track]:
        """Get dynamic list of tracks for given item, fallback/default implementation."""
        # TODO: query metadata provider(s) to get similar tracks (or tracks from similar artists)
        msg = "No Music Provider found that supports requesting similar tracks."
        raise UnsupportedFeaturedException(msg)

    async def _match(self, db_artist: Artist, provider: MusicProvider) -> bool:
        """Try to find matching artists on given provider for the provided (database) artist."""
        self.logger.debug("Trying to match artist %s on provider %s", db_artist.name, provider.name)
        # try to get a match with some reference tracks of this artist
        ref_tracks = await self.mass.music.artists.tracks(db_artist.item_id, db_artist.provider)
        if len(ref_tracks) < 10:
            # fetch reference tracks from provider(s) attached to the artist
            for provider_mapping in db_artist.provider_mappings:
                ref_tracks += await self.mass.music.artists.tracks(
                    provider_mapping.item_id, provider_mapping.provider_instance
                )
        for ref_track in ref_tracks:
            # make sure we have a full track
            if isinstance(ref_track.album, ItemMapping):
                try:
                    ref_track = await self.mass.music.tracks.get_provider_item(  # noqa: PLW2901
                        ref_track.item_id, ref_track.provider
                    )
                except MediaNotFoundError:
                    continue
            provider_ref_track = ref_track
            for search_str in (
                f"{db_artist.name} - {provider_ref_track.name}",
                f"{db_artist.name} {provider_ref_track.name}",
                f"{db_artist.sort_name} {provider_ref_track.sort_name}",
                provider_ref_track.name,
            ):
                search_results = await self.mass.music.tracks.search(search_str, provider.domain)
                for search_result_item in search_results:
                    if search_result_item.sort_name != provider_ref_track.sort_name:
                        continue
                    # get matching artist from track
                    for search_item_artist in search_result_item.artists:
                        if search_item_artist.sort_name != db_artist.sort_name:
                            continue
                        # 100% album match
                        # get full artist details so we have all metadata
                        prov_artist = await self.get_provider_item(
                            search_item_artist.item_id,
                            search_item_artist.provider,
                            fallback=search_result_item,
                        )
                        # 100% match, we update the db with the additional provider mapping(s)
                        for provider_mapping in prov_artist.provider_mappings:
                            await self.add_provider_mapping(db_artist.item_id, provider_mapping)
                        return True
        # try to get a match with some reference albums of this artist
        ref_albums = await self.mass.music.artists.albums(db_artist.item_id, db_artist.provider)
        if len(ref_albums) < 10:
            # fetch reference albums from provider(s) attached to the artist
            for provider_mapping in db_artist.provider_mappings:
                ref_albums += await self.mass.music.artists.albums(
                    provider_mapping.item_id, provider_mapping.provider_instance
                )
        for ref_album in ref_albums:
            if ref_album.album_type == AlbumType.COMPILATION:
                continue
            if not ref_album.artists:
                continue
            for search_str in (
                ref_album.name,
                f"{db_artist.name} - {ref_album.name}",
                f"{db_artist.name} {ref_album.name}",
                f"{db_artist.sort_name} {ref_album.sort_name}",
            ):
                search_result = await self.mass.music.albums.search(search_str, provider.domain)
                for search_result_item in search_result:
                    if not search_result_item.artists:
                        continue
                    if search_result_item.sort_name != ref_album.sort_name:
                        continue
                    # artist must match 100%
                    if not compare_artist(
                        db_artist, search_result_item.artists[0], allow_name_match=True
                    ):
                        continue
                    # 100% match
                    # get full artist details so we have all metadata
                    prov_artist = await self.get_provider_item(
                        search_result_item.artists[0].item_id,
                        search_result_item.artists[0].provider,
                        fallback=search_result_item,
                    )
                    await self.update_item_in_library(db_artist.item_id, prov_artist)
                    return True
        return False
