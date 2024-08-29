"""Manage MediaItems of type Album."""

from __future__ import annotations

import contextlib
from collections.abc import Iterable
from random import choice, random
from typing import TYPE_CHECKING, Any

from music_assistant.common.helpers.json import serialize_to_json
from music_assistant.common.models.enums import CacheCategory, ProviderFeature
from music_assistant.common.models.errors import (
    InvalidDataError,
    MediaNotFoundError,
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
from music_assistant.constants import DB_TABLE_ALBUM_ARTISTS, DB_TABLE_ALBUM_TRACKS, DB_TABLE_ALBUMS
from music_assistant.server.controllers.media.base import MediaControllerBase
from music_assistant.server.helpers.compare import (
    compare_album,
    compare_artists,
    compare_media_item,
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
        self.base_query = """
        SELECT
            albums.*,
            (SELECT JSON_GROUP_ARRAY(
                json_object(
                'item_id', provider_mappings.provider_item_id,
                    'provider_domain', provider_mappings.provider_domain,
                        'provider_instance', provider_mappings.provider_instance,
                        'available', provider_mappings.available,
                        'audio_format', json(provider_mappings.audio_format),
                        'url', provider_mappings.url,
                        'details', provider_mappings.details
                )) FROM provider_mappings WHERE provider_mappings.item_id = albums.item_id AND media_type = 'album') AS provider_mappings,
            (SELECT JSON_GROUP_ARRAY(
                json_object(
                'item_id', artists.item_id,
                'provider', 'library',
                    'name', artists.name,
                    'sort_name', artists.sort_name,
                    'media_type', 'artist'
                )) FROM artists JOIN album_artists on album_artists.album_id = albums.item_id  WHERE artists.item_id = album_artists.artist_id) AS artists
            FROM albums"""  # noqa: E501
        # register (extra) api handlers
        api_base = self.api_base
        self.mass.register_api_command(f"music/{api_base}/album_tracks", self.tracks)
        self.mass.register_api_command(f"music/{api_base}/album_versions", self.versions)

    async def get(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
        recursive: bool = True,
    ) -> Album:
        """Return (full) details for a single media item."""
        album = await super().get(
            item_id,
            provider_instance_id_or_domain,
        )
        if not recursive:
            return album

        # append artist details to full album item (resolve ItemMappings)
        album_artists = UniqueList()
        for artist in album.artists:
            if not isinstance(artist, ItemMapping):
                album_artists.append(artist)
                continue
            with contextlib.suppress(MediaNotFoundError):
                album_artists.append(
                    await self.mass.music.artists.get(
                        artist.item_id,
                        artist.provider,
                    )
                )
        album.artists = album_artists
        return album

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
        album_types: list[AlbumType] | None = None,
    ) -> list[Artist]:
        """Get in-database albums."""
        extra_query_params: dict[str, Any] = extra_query_params or {}
        extra_query_parts: list[str] = [extra_query] if extra_query else []
        extra_join_parts: list[str] = []
        artist_table_joined = False
        # optional album type filter
        if album_types:
            extra_query_parts.append("albums.album_type IN :album_types")
            extra_query_params["album_types"] = [x.value for x in album_types]
        if order_by and "artist_name" in order_by:
            # join artist table to allow sorting on artist name
            extra_join_parts.append(
                "JOIN album_artists ON album_artists.album_id = albums.item_id "
                "JOIN artists ON artists.item_id = album_artists.artist_id "
            )
            artist_table_joined = True
        if search and " - " in search:
            # handle combined artist + title search
            artist_str, title_str = search.split(" - ", 1)
            search = None
            extra_query_parts.append("albums.name LIKE :search_title")
            extra_query_params["search_title"] = f"%{title_str}%"
            # use join with artists table to filter on artist name
            extra_join_parts.append(
                "JOIN album_artists ON album_artists.album_id = albums.item_id "
                "JOIN artists ON artists.item_id = album_artists.artist_id "
                "AND artists.name LIKE :search_artist"
                if not artist_table_joined
                else "AND artists.name LIKE :search_artist"
            )
            artist_table_joined = True
            extra_query_params["search_artist"] = f"%{artist_str}%"
        result = await self._get_library_items_by_query(
            favorite=favorite,
            search=search,
            limit=limit,
            offset=offset,
            order_by=order_by,
            provider=provider,
            extra_query_parts=extra_query_parts,
            extra_query_params=extra_query_params,
            extra_join_parts=extra_join_parts,
        )
        if search and len(result) < 25 and not offset:
            # append artist items to result
            extra_join_parts.append(
                "JOIN album_artists ON album_artists.album_id = albums.item_id "
                "JOIN artists ON artists.item_id = album_artists.artist_id "
                "AND artists.name LIKE :search_artist"
                if not artist_table_joined
                else "AND artists.name LIKE :search_artist"
            )
            extra_query_params["search_artist"] = f"%{search}%"
            return result + await self._get_library_items_by_query(
                favorite=favorite,
                search=None,
                limit=limit,
                order_by=order_by,
                provider=provider,
                extra_query_parts=extra_query_parts,
                extra_query_params=extra_query_params,
                extra_join_parts=extra_join_parts,
            )
        return result

    async def library_count(
        self, favorite_only: bool = False, album_types: list[AlbumType] | None = None
    ) -> int:
        """Return the total number of items in the library."""
        sql_query = f"SELECT item_id FROM {self.db_table}"
        query_parts: list[str] = []
        query_params: dict[str, Any] = {}
        if favorite_only:
            query_parts.append("favorite = 1")
        if album_types:
            query_parts.append("albums.album_type IN :album_types")
            query_params["album_types"] = [x.value for x in album_types]
        if query_parts:
            sql_query += f" WHERE {' AND '.join(query_parts)}"
        return await self.mass.music.database.get_count_from_query(sql_query, query_params)

    async def remove_item_from_library(self, item_id: str | int) -> None:
        """Delete record from the database."""
        db_id = int(item_id)  # ensure integer
        # recursively also remove album tracks
        for db_track in await self.get_library_album_tracks(db_id):
            with contextlib.suppress(MediaNotFoundError):
                await self.mass.music.tracks.remove_item_from_library(db_track.item_id)
        # delete entry(s) from albumtracks table
        await self.mass.music.database.delete(DB_TABLE_ALBUM_TRACKS, {"album_id": db_id})
        # delete entry(s) from album artists table
        await self.mass.music.database.delete(DB_TABLE_ALBUM_ARTISTS, {"album_id": db_id})
        # delete the album itself from db
        await super().remove_item_from_library(item_id)

    async def tracks(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
        in_library_only: bool = False,
    ) -> UniqueList[Track]:
        """Return album tracks for the given provider album id."""
        # always check if we have a library item for this album
        library_album = await self.get_library_item_by_prov_id(
            item_id, provider_instance_id_or_domain
        )
        if not library_album:
            return await self._get_provider_album_tracks(item_id, provider_instance_id_or_domain)
        db_items = await self.get_library_album_tracks(library_album.item_id)
        result: UniqueList[Track] = UniqueList(db_items)
        if in_library_only:
            # return in-library items only
            return sorted(db_items, key=lambda x: (x.disc_number, x.track_number))

        # return all (unique) items from all providers
        # because we are returning the items from all providers combined,
        # we need to make sure that we don't return duplicates
        unique_ids: set[str] = {f"{x.disc_number}.{x.track_number}" for x in db_items}
        unique_ids.update({f"{x.name.lower()}.{x.version.lower()}" for x in db_items})
        for db_item in db_items:
            unique_ids.add(x.item_id for x in db_item.provider_mappings)
        for provider_mapping in library_album.provider_mappings:
            provider_tracks = await self._get_provider_album_tracks(
                provider_mapping.item_id, provider_mapping.provider_instance
            )
            for provider_track in provider_tracks:
                if provider_track.item_id in unique_ids:
                    continue
                unique_id = f"{provider_track.disc_number}.{provider_track.track_number}"
                if unique_id in unique_ids:
                    continue
                unique_id = f"{provider_track.name.lower()}.{provider_track.version.lower()}"
                if unique_id in unique_ids:
                    continue
                unique_ids.add(unique_id)
                provider_track.album = library_album
                result.append(provider_track)
        # NOTE: we need to return the results sorted on disc/track here
        # to ensure the correct order at playback
        return sorted(result, key=lambda x: (x.disc_number, x.track_number))

    async def versions(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
    ) -> UniqueList[Album]:
        """Return all versions of an album we can find on all providers."""
        album = await self.get_provider_item(item_id, provider_instance_id_or_domain)
        search_query = f"{album.artists[0].name} - {album.name}" if album.artists else album.name
        result: UniqueList[Album] = UniqueList()
        for provider_id in self.mass.music.get_unique_providers():
            provider = self.mass.get_provider(provider_id)
            if not provider:
                continue
            if not provider.library_supported(MediaType.ALBUM):
                continue
            result.extend(
                prov_item
                for prov_item in await self.search(search_query, provider_id)
                if loose_compare_strings(album.name, prov_item.name)
                and compare_artists(prov_item.artists, album.artists, any_match=True)
                # make sure that the 'base' version is NOT included
                and not album.provider_mappings.intersection(prov_item.provider_mappings)
            )
        return result

    async def get_library_album_tracks(
        self,
        item_id: str | int,
    ) -> list[Track]:
        """Return in-database album tracks for the given database album."""
        subquery = f"SELECT track_id FROM {DB_TABLE_ALBUM_TRACKS} WHERE album_id = {item_id}"
        query = f"WHERE tracks.item_id in ({subquery})"
        return await self.mass.music.tracks._get_library_items_by_query(extra_query_parts=[query])

    async def _add_library_item(self, item: Album) -> int:
        """Add a new record to the database."""
        if not isinstance(item, Album):
            msg = "Not a valid Album object (ItemMapping can not be added to db)"
            raise InvalidDataError(msg)
        if not item.artists:
            msg = "Album is missing artist(s)"
            raise InvalidDataError(msg)
        db_id = await self.mass.music.database.insert(
            self.db_table,
            {
                "name": item.name,
                "sort_name": item.sort_name,
                "version": item.version,
                "favorite": item.favorite,
                "album_type": item.album_type,
                "year": item.year,
                "metadata": serialize_to_json(item.metadata),
                "external_ids": serialize_to_json(item.external_ids),
            },
        )
        # update/set provider_mappings table
        await self._set_provider_mappings(db_id, item.provider_mappings)
        # set track artist(s)
        await self._set_album_artists(db_id, item.artists)
        self.logger.debug("added %s to database (id: %s)", item.name, db_id)
        return db_id

    async def _update_library_item(
        self, item_id: str | int, update: Album, overwrite: bool = False
    ) -> None:
        """Update existing record in the database."""
        db_id = int(item_id)  # ensure integer
        cur_item = await self.get_library_item(db_id)
        metadata = update.metadata if overwrite else cur_item.metadata.update(update.metadata)
        if getattr(update, "album_type", AlbumType.UNKNOWN) != AlbumType.UNKNOWN:
            album_type = update.album_type
        else:
            album_type = cur_item.album_type
        cur_item.external_ids.update(update.external_ids)
        provider_mappings = (
            update.provider_mappings
            if overwrite
            else {*cur_item.provider_mappings, *update.provider_mappings}
        )
        await self.mass.music.database.update(
            self.db_table,
            {"item_id": db_id},
            {
                "name": update.name if overwrite else cur_item.name,
                "sort_name": update.sort_name
                if overwrite
                else cur_item.sort_name or update.sort_name,
                "version": update.version if overwrite else cur_item.version or update.version,
                "year": update.year if overwrite else cur_item.year or update.year,
                "album_type": album_type.value,
                "metadata": serialize_to_json(metadata),
                "external_ids": serialize_to_json(
                    update.external_ids if overwrite else cur_item.external_ids
                ),
            },
        )
        # update/set provider_mappings table
        await self._set_provider_mappings(db_id, provider_mappings, overwrite)
        # set album artist(s)
        artists = update.artists if overwrite else cur_item.artists + update.artists
        await self._set_album_artists(db_id, artists, overwrite=overwrite)
        self.logger.debug("updated %s in database: (id %s)", update.name, db_id)

    async def _get_provider_album_tracks(
        self, item_id: str, provider_instance_id_or_domain: str
    ) -> list[Track]:
        """Return album tracks for the given provider album id."""
        prov: MusicProvider = self.mass.get_provider(provider_instance_id_or_domain)
        if prov is None:
            return []
        # prefer cache items (if any) - for streaming providers only
        cache_category = CacheCategory.MUSIC_ALBUM_TRACKS
        cache_base_key = prov.lookup_key
        cache_key = item_id
        if (
            prov.is_streaming_provider
            and (
                cache := await self.mass.cache.get(
                    cache_key, category=cache_category, base_key=cache_base_key
                )
            )
            is not None
        ):
            return [Track.from_dict(x) for x in cache]
        # no items in cache - get listing from provider
        items = await prov.get_album_tracks(item_id)
        # store (serializable items) in cache
        if prov.is_streaming_provider:
            self.mass.create_task(
                self.mass.cache.set(cache_key, [x.to_dict() for x in items]),
                category=cache_category,
                base_key=cache_base_key,
            )
        for item in items:
            # if this is a complete track object, pre-cache it as
            # that will save us an (expensive) lookup later
            if item.image and item.artist_str and item.album and prov.domain != "builtin":
                await self.mass.cache.set(
                    f"track.{item_id}",
                    item.to_dict(),
                    category=CacheCategory.MUSIC_PROVIDER_ITEM,
                    base_key=prov.lookup_key,
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

    async def _set_album_artists(
        self, db_id: int, artists: Iterable[Artist | ItemMapping], overwrite: bool = False
    ) -> None:
        """Store Album Artists."""
        if overwrite:
            # on overwrite, clear the album_artists table first
            await self.mass.music.database.delete(
                DB_TABLE_ALBUM_ARTISTS,
                {
                    "album_id": db_id,
                },
            )
        for artist in artists:
            await self._set_album_artist(db_id, artist=artist, overwrite=overwrite)

    async def _set_album_artist(
        self, db_id: int, artist: Artist | ItemMapping, overwrite: bool = False
    ) -> ItemMapping:
        """Store Album Artist info."""
        db_artist: Artist | ItemMapping = None
        if artist.provider == "library":
            db_artist = artist
        elif existing := await self.mass.music.artists.get_library_item_by_prov_id(
            artist.item_id, artist.provider
        ):
            db_artist = existing

        if not db_artist or overwrite:
            db_artist = await self.mass.music.artists.add_item_to_library(
                artist, overwrite_existing=overwrite
            )
        # write (or update) record in album_artists table
        await self.mass.music.database.insert_or_replace(
            DB_TABLE_ALBUM_ARTISTS,
            {
                "album_id": db_id,
                "artist_id": int(db_artist.item_id),
            },
        )
        return ItemMapping.from_item(db_artist)

    async def match_providers(self, db_album: Album) -> None:
        """Try to find match on all (streaming) providers for the provided (database) album.

        This is used to link objects of different providers/qualities together.
        """
        if db_album.provider != "library":
            return  # Matching only supported for database items
        if not db_album.artists:
            return  # guard
        artist_name = db_album.artists[0].name

        async def find_prov_match(provider: MusicProvider):
            self.logger.debug(
                "Trying to match album %s on provider %s", db_album.name, provider.name
            )
            match_found = False
            search_str = f"{artist_name} - {db_album.name}"
            search_result = await self.search(search_str, provider.instance_id)
            for search_result_item in search_result:
                if not search_result_item.available:
                    continue
                if not compare_media_item(db_album, search_result_item):
                    continue
                # we must fetch the full album version, search results can be simplified objects
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
                        db_album.provider_mappings.add(provider_mapping)
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
