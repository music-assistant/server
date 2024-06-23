"""Manage MediaItems of type Artist."""

from __future__ import annotations

import asyncio
import contextlib
from random import choice, random
from typing import TYPE_CHECKING, Any

from music_assistant.common.helpers.json import serialize_to_json
from music_assistant.common.models.enums import ProviderFeature
from music_assistant.common.models.errors import (
    MediaNotFoundError,
    ProviderUnavailableError,
    UnsupportedFeaturedException,
)
from music_assistant.common.models.media_items import (
    Album,
    AlbumType,
    Artist,
    ItemMapping,
    MediaType,
    Track,
    UniqueList,
)
from music_assistant.constants import (
    DB_TABLE_ALBUM_ARTISTS,
    DB_TABLE_ARTISTS,
    DB_TABLE_TRACK_ARTISTS,
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
        # register (extra) api handlers
        api_base = self.api_base
        self.mass.register_api_command(f"music/{api_base}/artist_albums", self.albums)
        self.mass.register_api_command(f"music/{api_base}/artist_tracks", self.tracks)

    async def library_count(
        self, favorite_only: bool = False, album_artists_only: bool = False
    ) -> int:
        """Return the total number of items in the library."""
        sql_query = f"SELECT item_id FROM {self.db_table}"
        query_parts: list[str] = []
        if favorite_only:
            query_parts.append("favorite = 1")
        if album_artists_only:
            query_parts.append(
                f"item_id in (select {DB_TABLE_ALBUM_ARTISTS}.artist_id "
                f"FROM {DB_TABLE_ALBUM_ARTISTS})"
            )
        if query_parts:
            sql_query += f" WHERE {' AND '.join(query_parts)}"
        return await self.mass.music.database.get_count_from_query(sql_query)

    async def library_items(
        self,
        favorite: bool | None = None,
        search: str | None = None,
        limit: int = 500,
        offset: int = 0,
        order_by: str = "sort_name",
        provider: str | None = None,
        extra_query: str | None = None,
        extra_query_params: dict[str, Any] | None = None,
        album_artists_only: bool = False,
    ) -> list[Artist]:
        """Get in-database (album) artists."""
        if album_artists_only:
            artist_query = (
                f"artists.item_id in (select {DB_TABLE_ALBUM_ARTISTS}.artist_id "
                f"from {DB_TABLE_ALBUM_ARTISTS})"
            )
            extra_query = f"{extra_query} AND {artist_query}" if extra_query else artist_query
        return await super().library_items(
            favorite=favorite,
            search=search,
            limit=limit,
            offset=offset,
            order_by=order_by,
            provider=provider,
            extra_query=extra_query,
            extra_query_params=extra_query_params,
        )

    async def tracks(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
        in_library_only: bool = False,
    ) -> UniqueList[Track]:
        """Return all/top tracks for an artist."""
        # always check if we have a library item for this artist
        library_artist = await self.get_library_item_by_prov_id(
            item_id, provider_instance_id_or_domain
        )
        if not library_artist:
            return await self.get_provider_artist_toptracks(item_id, provider_instance_id_or_domain)
        db_items = await self.get_library_artist_tracks(library_artist.item_id)
        result: UniqueList[Track] = UniqueList(db_items)
        if in_library_only:
            # return in-library items only
            return result
        # return all (unique) items from all providers
        unique_ids: set[str] = set()
        for provider_mapping in library_artist.provider_mappings:
            provider_tracks = await self.get_provider_artist_toptracks(
                provider_mapping.item_id, provider_mapping.provider_instance
            )
            for provider_track in provider_tracks:
                unique_id = f"{provider_track.name}.{provider_track.version}"
                if unique_id in unique_ids:
                    continue
                unique_ids.add(unique_id)
                # prefer db item
                if db_item := await self.mass.music.tracks.get_library_item_by_prov_id(
                    provider_track.item_id, provider_track.provider
                ):
                    result.append(db_item)
                elif not in_library_only:
                    result.append(provider_track)
        return result

    async def albums(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
        in_library_only: bool = False,
    ) -> UniqueList[Album]:
        """Return (all/most popular) albums for an artist."""
        # always check if we have a library item for this artist
        library_artist = await self.get_library_item_by_prov_id(
            item_id, provider_instance_id_or_domain
        )
        if not library_artist:
            return await self.get_provider_artist_albums(item_id, provider_instance_id_or_domain)
        db_items = await self.get_library_artist_albums(library_artist.item_id)
        result: UniqueList[Album] = UniqueList(db_items)
        if in_library_only:
            # return in-library items only
            return result
        # return all (unique) items from all providers
        unique_ids: set[str] = set()
        for provider_mapping in library_artist.provider_mappings:
            provider_albums = await self.get_provider_artist_albums(
                provider_mapping.item_id, provider_mapping.provider_instance
            )
            for provider_album in provider_albums:
                unique_id = f"{provider_album.name}.{provider_album.version}"
                if unique_id in unique_ids:
                    continue
                unique_ids.add(unique_id)
                # prefer db item
                if db_item := await self.mass.music.albums.get_library_item_by_prov_id(
                    provider_album.item_id, provider_album.provider
                ):
                    result.append(db_item)
                elif not in_library_only:
                    result.append(provider_album)
        return result

    async def remove_item_from_library(self, item_id: str | int) -> None:
        """Delete record from the database."""
        db_id = int(item_id)  # ensure integer
        # recursively also remove artist albums
        for db_row in await self.mass.music.database.get_rows_from_query(
            f"SELECT album_id FROM {DB_TABLE_ALBUM_ARTISTS} WHERE artist_id = {db_id}",
            limit=5000,
        ):
            with contextlib.suppress(MediaNotFoundError):
                await self.mass.music.albums.remove_item_from_library(db_row["album_id"])

        # recursively also remove artist tracks
        for db_row in await self.mass.music.database.get_rows_from_query(
            f"SELECT track_id FROM {DB_TABLE_TRACK_ARTISTS} WHERE artist_id = {db_id}",
            limit=5000,
        ):
            with contextlib.suppress(MediaNotFoundError):
                await self.mass.music.tracks.remove_item_from_library(db_row["track_id"])

        # delete the artist itself from db
        await super().remove_item_from_library(db_id)

    async def get_provider_artist_toptracks(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
    ) -> list[Track]:
        """Return top tracks for an artist on given provider."""
        items = []
        assert provider_instance_id_or_domain != "library"
        prov = self.mass.get_provider(provider_instance_id_or_domain)
        if prov is None:
            return []
        # prefer cache items (if any) - for streaming providers
        cache_key = f"{prov.lookup_key}.artist_toptracks.{item_id}"
        if (
            prov.is_streaming_provider
            and (cache := await self.mass.cache.get(cache_key)) is not None
        ):
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
                query = (
                    f"WHERE {DB_TABLE_TRACK_ARTISTS}.artist_id = :artist_id "
                    "AND (provider_domain = :prov_id "
                    "OR provider_instance = :prov_id)"
                )
                query_params = {
                    "artist_id": db_artist.item_id,
                    "prov_id": provider_instance_id_or_domain,
                }
                return await self.mass.music.tracks._get_library_items_by_query(
                    extra_query=query, extra_query_params=query_params
                )
        # store (serializable items) in cache
        if prov.is_streaming_provider:
            self.mass.create_task(self.mass.cache.set(cache_key, [x.to_dict() for x in items]))
        return items

    async def get_library_artist_tracks(
        self,
        item_id: str | int,
    ) -> list[Track]:
        """Return all tracks for an artist in the library/db."""
        return await self.mass.music.tracks._get_library_items_by_query(
            extra_query=f"WHERE {DB_TABLE_TRACK_ARTISTS}.artist_id = {item_id}",
        )

    async def get_provider_artist_albums(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
    ) -> list[Album]:
        """Return albums for an artist on given provider."""
        items = []
        assert provider_instance_id_or_domain != "library"
        prov = self.mass.get_provider(provider_instance_id_or_domain)
        if prov is None:
            return []
        # prefer cache items (if any)
        cache_key = f"{prov.lookup_key}.artist_albums.{item_id}"
        if (
            prov.is_streaming_provider
            and (cache := await self.mass.cache.get(cache_key)) is not None
        ):
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
                query = (
                    f"WHERE {DB_TABLE_ALBUM_ARTISTS}.artist_id = :artist_id "
                    "AND (provider_domain = :prov_id "
                    "OR provider_instance = :prov_id)"
                )
                query_params = {
                    "prov_id": provider_instance_id_or_domain,
                    "artist_id": db_artist.item_id,
                }
                return await self.mass.music.albums._get_library_items_by_query(
                    extra_query=query, extra_query_params=query_params
                )

        # store (serializable items) in cache
        if prov.is_streaming_provider:
            self.mass.create_task(self.mass.cache.set(cache_key, [x.to_dict() for x in items]))
        return items

    async def get_library_artist_albums(
        self,
        item_id: str | int,
    ) -> list[Album]:
        """Return all in-library albums for an artist."""
        query = f"WHERE {DB_TABLE_ALBUM_ARTISTS}.artist_id = {item_id}"
        return await self.mass.music.albums._get_library_items_by_query(extra_query=query)

    async def _add_library_item(self, item: Artist | ItemMapping) -> int:
        """Add a new item record to the database."""
        if isinstance(item, ItemMapping):
            item = self._artist_from_item_mapping(item)
        # enforce various artists name + id
        if compare_strings(item.name, VARIOUS_ARTISTS_NAME):
            item.mbid = VARIOUS_ARTISTS_ID_MBID
        if item.mbid == VARIOUS_ARTISTS_ID_MBID:
            item.name = VARIOUS_ARTISTS_NAME
        # no existing item matched: insert item
        new_item = await self.mass.music.database.insert(
            self.db_table,
            {
                "name": item.name,
                "sort_name": item.sort_name,
                "favorite": item.favorite,
                "external_ids": serialize_to_json(item.external_ids),
                "metadata": serialize_to_json(item.metadata),
            },
        )
        db_id = new_item["item_id"]
        # update/set provider_mappings table
        await self._set_provider_mappings(db_id, item.provider_mappings)
        self.logger.debug("added %s to database (id: %s)", item.name, db_id)
        return db_id

    async def _update_library_item(
        self, item_id: str | int, update: Artist | ItemMapping, overwrite: bool = False
    ) -> None:
        """Update existing record in the database."""
        db_id = int(item_id)  # ensure integer
        cur_item = await self.get_library_item(db_id)
        if isinstance(update, ItemMapping):
            # NOTE that artist is the only mediatype where its accepted we
            # receive an itemmapping from streaming providers
            update = self._artist_from_item_mapping(update)
            metadata = cur_item.metadata
        else:
            metadata = update.metadata if overwrite else cur_item.metadata.update(update.metadata)
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
                "sort_name": update.sort_name
                if overwrite
                else cur_item.sort_name or update.sort_name,
                "external_ids": serialize_to_json(
                    update.external_ids if overwrite else cur_item.external_ids
                ),
                "metadata": serialize_to_json(metadata),
            },
        )
        self.logger.debug("updated %s in database: %s", update.name, db_id)
        # update/set provider_mappings table
        provider_mappings = (
            update.provider_mappings
            if overwrite
            else {*cur_item.provider_mappings, *update.provider_mappings}
        )
        await self._set_provider_mappings(db_id, provider_mappings, overwrite)
        self.logger.debug("updated %s in database: (id %s)", update.name, db_id)

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

    async def _match(self, db_artist: Artist) -> None:
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
            if await self._match_provider(db_artist, provider):
                cur_provider_domains.add(provider.domain)
            else:
                self.logger.debug(
                    "Could not find match for Artist %s on provider %s",
                    db_artist.name,
                    provider.name,
                )

    async def _match_provider(self, db_artist: Artist, provider: MusicProvider) -> bool:
        """Try to find matching artists on given provider for the provided (database) artist."""
        self.logger.debug("Trying to match artist %s on provider %s", db_artist.name, provider.name)
        # try to get a match with some reference tracks of this artist
        ref_tracks = await self.mass.music.artists.tracks(db_artist.item_id, db_artist.provider)
        if len(ref_tracks) < 10:
            # fetch reference tracks from provider(s) attached to the artist
            for provider_mapping in db_artist.provider_mappings:
                with contextlib.suppress(ProviderUnavailableError, MediaNotFoundError):
                    ref_tracks += await self.mass.music.artists.tracks(
                        provider_mapping.item_id, provider_mapping.provider_instance
                    )
        for ref_track in ref_tracks:
            for search_str in (
                f"{db_artist.name} - {ref_track.name}",
                f"{db_artist.name} {ref_track.name}",
                ref_track.name,
            ):
                search_results = await self.mass.music.tracks.search(search_str, provider.domain)
                for search_result_item in search_results:
                    if not compare_strings(search_result_item.name, ref_track.name, strict=True):
                        continue
                    # get matching artist from track
                    for search_item_artist in search_result_item.artists:
                        if not compare_strings(
                            search_item_artist.name, db_artist.name, strict=True
                        ):
                            continue
                        # 100% track match
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
                with contextlib.suppress(ProviderUnavailableError, MediaNotFoundError):
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
            ):
                search_result = await self.mass.music.albums.search(search_str, provider.domain)
                for search_result_item in search_result:
                    if not search_result_item.artists:
                        continue
                    if not compare_strings(search_result_item.name, ref_album.name):
                        continue
                    # artist must match 100%
                    if not compare_artist(db_artist, search_result_item.artists[0]):
                        continue
                    # 100% match
                    # get full artist details so we have all metadata
                    prov_artist = await self.get_provider_item(
                        search_result_item.artists[0].item_id,
                        search_result_item.artists[0].provider,
                        fallback=search_result_item,
                    )
                    await self._update_library_item(db_artist.item_id, prov_artist)
                    return True
        return False

    def _artist_from_item_mapping(self, item: ItemMapping) -> Artist:
        domain, instance_id = None, None
        if prov := self.mass.get_provider(item.provider):
            domain = prov.domain
            instance_id = prov.instance_id
        return Artist.from_dict(
            {
                **item.to_dict(),
                "provider_mappings": [
                    {
                        "item_id": item.item_id,
                        "provider_domain": domain,
                        "provider_instance": instance_id,
                        "available": item.available,
                    }
                ],
            }
        )
