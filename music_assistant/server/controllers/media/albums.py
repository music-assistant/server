"""Manage MediaItems of type Album."""

from __future__ import annotations

import contextlib
from collections.abc import Iterable
from random import choice, random
from typing import TYPE_CHECKING, cast

from music_assistant.common.helpers.global_cache import get_global_cache_value
from music_assistant.common.helpers.json import serialize_to_json
from music_assistant.common.models.enums import ProviderFeature
from music_assistant.common.models.errors import (
    InvalidDataError,
    MediaNotFoundError,
    UnsupportedFeaturedException,
)
from music_assistant.common.models.media_items import (
    Album,
    AlbumTrack,
    AlbumType,
    Artist,
    ItemMapping,
    MediaType,
    Track,
    UniqueList,
)
from music_assistant.constants import (
    DB_TABLE_ALBUM_ARTISTS,
    DB_TABLE_ALBUM_TRACKS,
    DB_TABLE_ALBUMS,
    DB_TABLE_ARTISTS,
    DB_TABLE_PROVIDER_MAPPINGS,
)
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
        self.base_query = f"""
        SELECT DISTINCT {self.db_table}.* FROM {self.db_table}
        LEFT JOIN {DB_TABLE_ALBUM_ARTISTS} on {DB_TABLE_ALBUM_ARTISTS}.album_id = {self.db_table}.item_id
        LEFT JOIN {DB_TABLE_ARTISTS} on {DB_TABLE_ARTISTS}.item_id = {DB_TABLE_ALBUM_ARTISTS}.artist_id
        LEFT JOIN {DB_TABLE_PROVIDER_MAPPINGS} ON
            {DB_TABLE_PROVIDER_MAPPINGS}.item_id = {self.db_table}.item_id AND media_type = '{self.media_type}'
        """  # noqa: E501
        # register (extra) api handlers
        api_base = self.api_base
        self.mass.register_api_command(f"music/{api_base}/album_tracks", self.tracks)
        self.mass.register_api_command(f"music/{api_base}/album_versions", self.versions)

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
        # append artist details to full track item (resolve ItemMappings)
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
                        lazy=lazy,
                        details=artist,
                        add_to_library=False,
                    )
                )
        album.artists = album_artists
        if not force_refresh:
            return album
        # if force refresh, we need to ensure that we also refresh all album tracks
        # in case of a filebased (non streaming) provider to ensure we catch changes the user
        # made on track level and then pressed the refresh button on album level.
        file_provs = get_global_cache_value("non_streaming_providers", [])
        for album_provider_mapping in album.provider_mappings:
            if album_provider_mapping.provider_instance not in file_provs:
                continue
            for prov_album_track in await self._get_provider_album_tracks(
                album_provider_mapping.item_id, album_provider_mapping.provider_instance
            ):
                if prov_album_track.provider != "library":
                    continue
                for track_prov_map in prov_album_track.provider_mappings:
                    if track_prov_map.provider_instance != album_provider_mapping.provider_instance:
                        continue
                    prov_track = await self.mass.music.tracks.get_provider_item(
                        track_prov_map.item_id, track_prov_map.provider_instance, force_refresh=True
                    )
                    if (
                        prov_track.metadata.cache_checksum
                        == prov_album_track.metadata.cache_checksum
                    ):
                        continue
                    await self.mass.music.tracks._update_library_item(
                        prov_album_track.item_id, prov_track, True
                    )
                    break
        return album

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
    ) -> UniqueList[AlbumTrack]:
        """Return album tracks for the given provider album id."""
        # always check if we have a library item for this album
        library_album = await self.get_library_item_by_prov_id(
            item_id, provider_instance_id_or_domain
        )
        if not library_album:
            return await self._get_provider_album_tracks(item_id, provider_instance_id_or_domain)
        db_items = await self.get_library_album_tracks(library_album.item_id)
        result: UniqueList[AlbumTrack] = UniqueList(db_items)
        if in_library_only:
            # return in-library items only
            return sorted(db_items, key=lambda x: (x.disc_number, x.track_number))
        # return all (unique) items from all providers
        unique_ids: set[str] = {f"{x.disc_number or 1}.{x.track_number}" for x in db_items}
        for db_item in db_items:
            unique_ids.add(x.item_id for x in db_item.provider_mappings)
        for provider_mapping in library_album.provider_mappings:
            provider_tracks = await self._get_provider_album_tracks(
                provider_mapping.item_id, provider_mapping.provider_instance
            )
            for provider_track in provider_tracks:
                if provider_track.item_id in unique_ids:
                    continue
                unique_id = f"{provider_track.disc_number or 1}.{provider_track.track_number}"
                if unique_id in unique_ids:
                    continue
                unique_ids.add(unique_id)
                result.append(AlbumTrack.from_track(provider_track, library_album))
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
    ) -> list[AlbumTrack]:
        """Return in-database album tracks for the given database album."""
        query = f"WHERE {DB_TABLE_ALBUM_TRACKS}.album_id = {item_id}"
        result = await self.mass.music.tracks._get_library_items_by_query(extra_query=query)
        if TYPE_CHECKING:
            return cast(list[AlbumTrack], result)
        return result

    async def _add_library_item(self, item: Album) -> int:
        """Add a new record to the database."""
        if not isinstance(item, Album):
            msg = "Not a valid Album object (ItemMapping can not be added to db)"
            raise InvalidDataError(msg)
        if not item.artists:
            msg = "Album is missing artist(s)"
            raise InvalidDataError(msg)
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
                "external_ids": serialize_to_json(item.external_ids),
            },
        )
        db_id = new_item["item_id"]
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
                "version": update.version if overwrite else cur_item.version,
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
        cache_key = f"{prov.lookup_key}.albumtracks.{item_id}"
        if (
            prov.is_streaming_provider
            and (cache := await self.mass.cache.get(cache_key)) is not None
        ):
            return [Track.from_dict(x) for x in cache]
        # no items in cache - get listing from provider
        items = await prov.get_album_tracks(item_id)
        # store (serializable items) in cache
        if prov.is_streaming_provider:
            self.mass.create_task(self.mass.cache.set(cache_key, [x.to_dict() for x in items]))
        for item in items:
            # if this is a complete track object, pre-cache it as
            # that will save us an (expensive) lookup later
            if item.image and item.artist_str and item.album and prov.domain != "builtin":
                await self.mass.cache.set(
                    f"provider_item.track.{prov.lookup_key}.{item_id}", item.to_dict()
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
        artist_mappings: UniqueList[ItemMapping] = UniqueList()
        for artist in artists:
            mapping = await self._set_album_artist(db_id, artist=artist, overwrite=overwrite)
            artist_mappings.append(mapping)
        # we (temporary?) duplicate the artist mappings in a separate column of the media
        # item's table, because the json_group_array query is superslow
        await self.mass.music.database.update(
            self.db_table,
            {"item_id": db_id},
            {"artists": serialize_to_json(artist_mappings)},
        )

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
                artist, metadata_lookup=False, overwrite_existing=overwrite
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

    async def _match(self, db_album: Album) -> None:
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
