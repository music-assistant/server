"""Manage MediaItems of type Artist."""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any, cast

from music_assistant_models.enums import AlbumType, MediaType, ProviderFeature
from music_assistant_models.errors import (
    MediaNotFoundError,
    MusicAssistantError,
    ProviderUnavailableError,
)
from music_assistant_models.media_items import Album, Artist, ItemMapping, ProviderMapping, Track

from music_assistant.constants import (
    DB_TABLE_ALBUM_ARTISTS,
    DB_TABLE_ARTISTS,
    DB_TABLE_TRACK_ARTISTS,
    VARIOUS_ARTISTS_MBID,
    VARIOUS_ARTISTS_NAME,
)
from music_assistant.controllers.media.base import MediaControllerBase
from music_assistant.helpers.compare import compare_artist, compare_strings, create_safe_string
from music_assistant.helpers.database import UNSET
from music_assistant.helpers.json import serialize_to_json

if TYPE_CHECKING:
    from music_assistant import MusicAssistant
    from music_assistant.models.music_provider import MusicProvider


class ArtistsController(MediaControllerBase[Artist]):
    """Controller managing MediaItems of type Artist."""

    db_table = DB_TABLE_ARTISTS
    media_type = MediaType.ARTIST
    item_cls = Artist

    def __init__(self, mass: MusicAssistant) -> None:
        """Initialize class."""
        super().__init__(mass)
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
        provider: str | list[str] | None = None,
        album_artists_only: bool = False,
    ) -> list[Artist]:
        """Get in-database (album) artists.

        :param favorite: Filter by favorite status.
        :param search: Filter by search query.
        :param limit: Maximum number of items to return.
        :param offset: Number of items to skip.
        :param order_by: Order by field (e.g. 'sort_name', 'timestamp_added').
        :param provider: Filter by provider instance ID (single string or list).
        :param album_artists_only: Only return artists that have albums.
        """
        extra_query_params: dict[str, Any] = {}
        extra_query_parts: list[str] = []
        if album_artists_only:
            extra_query_parts.append(
                f"artists.item_id in (select {DB_TABLE_ALBUM_ARTISTS}.artist_id "
                f"from {DB_TABLE_ALBUM_ARTISTS})"
            )
        return await self.get_library_items_by_query(
            favorite=favorite,
            search=search,
            limit=limit,
            offset=offset,
            order_by=order_by,
            provider_filter=self._ensure_provider_filter(provider),
            extra_query_parts=extra_query_parts,
            extra_query_params=extra_query_params,
        )

    async def tracks(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
        in_library_only: bool = False,
        provider_filter: str | list[str] | None = None,
    ) -> list[Track]:
        """Return all/top tracks for an artist."""
        if provider_filter and provider_instance_id_or_domain != "library":
            raise MusicAssistantError("Cannot use provider_filter with specific provider request")
        if isinstance(provider_filter, str):
            provider_filter = [provider_filter]
        # always check if we have a library item for this artist
        library_artist = await self.get_library_item_by_prov_id(
            item_id, provider_instance_id_or_domain
        )
        if not library_artist:
            return await self.get_provider_artist_toptracks(item_id, provider_instance_id_or_domain)
        db_items = await self.get_library_artist_tracks(library_artist.item_id)
        result: list[Track] = db_items
        if in_library_only and not provider_filter:
            # return in-library items only
            return result
        # return all (unique) items from all providers
        # initialize unique_ids with db_items to prevent duplicates
        unique_ids: set[str] = {f"{item.name}.{item.version}" for item in db_items}
        unique_providers = self.mass.music.get_unique_providers()
        for provider_mapping in library_artist.provider_mappings:
            if provider_mapping.provider_instance not in unique_providers:
                continue
            if provider_filter and provider_mapping.provider_instance not in provider_filter:
                continue
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
    ) -> list[Album]:
        """Return (all/most popular) albums for an artist."""
        # always check if we have a library item for this artist
        library_artist = await self.get_library_item_by_prov_id(
            item_id, provider_instance_id_or_domain
        )
        if not library_artist:
            return await self.get_provider_artist_albums(item_id, provider_instance_id_or_domain)
        db_items = await self.get_library_artist_albums(library_artist.item_id)
        result: list[Album] = db_items
        if in_library_only:
            # return in-library items only
            return result
        # return all (unique) items from all providers
        # initialize unique_ids with db_items to prevent duplicates
        unique_ids: set[str] = {f"{item.name}.{item.version}" for item in db_items}
        unique_providers = self.mass.music.get_unique_providers()
        for provider_mapping in library_artist.provider_mappings:
            if provider_mapping.provider_instance not in unique_providers:
                continue
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

    async def remove_item_from_library(self, item_id: str | int, recursive: bool = True) -> None:
        """Delete record from the database."""
        db_id = int(item_id)  # ensure integer

        # recursively also remove artist albums
        for db_row in await self.mass.music.database.get_rows_from_query(
            f"SELECT album_id FROM {DB_TABLE_ALBUM_ARTISTS} WHERE artist_id = :artist_id",
            {"artist_id": db_id},
            limit=5000,
        ):
            if not recursive:
                raise MusicAssistantError("Artist still has albums linked")
            with contextlib.suppress(MediaNotFoundError):
                await self.mass.music.albums.remove_item_from_library(db_row["album_id"])
        # recursively also remove artist tracks
        for db_row in await self.mass.music.database.get_rows_from_query(
            f"SELECT track_id FROM {DB_TABLE_TRACK_ARTISTS} WHERE artist_id = :artist_id",
            {"artist_id": db_id},
            limit=5000,
        ):
            if not recursive:
                raise MusicAssistantError("Artist still has tracks linked")
            with contextlib.suppress(MediaNotFoundError):
                await self.mass.music.tracks.remove_item_from_library(db_row["track_id"])

        # delete the artist itself from db
        # this will raise if the item still has references and recursive is false
        await super().remove_item_from_library(db_id)

    async def get_provider_artist_toptracks(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
    ) -> list[Track]:
        """Return top tracks for an artist on given provider."""
        assert provider_instance_id_or_domain != "library"
        if not (prov := self.mass.get_provider(provider_instance_id_or_domain)):
            return []
        prov = cast("MusicProvider", prov)
        if ProviderFeature.ARTIST_TOPTRACKS in prov.supported_features:
            return await prov.get_artist_toptracks(item_id)
        # fallback implementation using the library db
        if db_artist := await self.mass.music.artists.get_library_item_by_prov_id(
            item_id,
            provider_instance_id_or_domain,
        ):
            db_artist_id = int(db_artist.item_id)  # ensure integer
            subquery = f"SELECT track_id FROM {DB_TABLE_TRACK_ARTISTS} WHERE artist_id = :artist_id"
            query = f"tracks.item_id in ({subquery})"
            return await self.mass.music.tracks.get_library_items_by_query(
                extra_query_parts=[query],
                extra_query_params={"artist_id": db_artist_id},
                provider_filter=[provider_instance_id_or_domain],
            )
        return []

    async def get_library_artist_tracks(
        self,
        item_id: str | int,
    ) -> list[Track]:
        """Return all tracks for an artist in the library/db."""
        db_id = int(item_id)  # ensure integer
        subquery = f"SELECT track_id FROM {DB_TABLE_TRACK_ARTISTS} WHERE artist_id = :artist_id"
        query = f"tracks.item_id in ({subquery})"
        return await self.mass.music.tracks.get_library_items_by_query(
            extra_query_parts=[query],
            extra_query_params={"artist_id": db_id},
        )

    async def get_provider_artist_albums(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
    ) -> list[Album]:
        """Return albums for an artist on given provider."""
        assert provider_instance_id_or_domain != "library"
        if not (prov := self.mass.get_provider(provider_instance_id_or_domain)):
            return []
        prov = cast("MusicProvider", prov)
        if ProviderFeature.ARTIST_ALBUMS in prov.supported_features:
            return await prov.get_artist_albums(item_id)
        # fallback implementation using the db
        if db_artist := await self.mass.music.artists.get_library_item_by_prov_id(
            item_id,
            provider_instance_id_or_domain,
        ):
            db_artist_id = int(db_artist.item_id)  # ensure integer
            subquery = f"SELECT album_id FROM {DB_TABLE_ALBUM_ARTISTS} WHERE artist_id = :artist_id"
            query = f"albums.item_id in ({subquery})"
            return await self.mass.music.albums.get_library_items_by_query(
                extra_query_parts=[query],
                extra_query_params={"artist_id": db_artist_id},
                provider_filter=[provider_instance_id_or_domain],
            )
        return []

    async def get_library_artist_albums(
        self,
        item_id: str | int,
    ) -> list[Album]:
        """Return all in-library albums for an artist."""
        db_id = int(item_id)  # ensure integer
        subquery = f"SELECT album_id FROM {DB_TABLE_ALBUM_ARTISTS} WHERE artist_id = :artist_id"
        query = f"albums.item_id in ({subquery})"
        return await self.mass.music.albums.get_library_items_by_query(
            extra_query_parts=[query],
            extra_query_params={"artist_id": db_id},
        )

    async def _add_library_item(
        self, item: Artist | ItemMapping, overwrite_existing: bool = False
    ) -> int:
        """Add a new item record to the database."""
        # If item is an ItemMapping, convert it
        if isinstance(item, ItemMapping):
            item = self.artist_from_item_mapping(item)
        # enforce various artists name + id
        if compare_strings(item.name, VARIOUS_ARTISTS_NAME):
            item.mbid = VARIOUS_ARTISTS_MBID
        if item.mbid == VARIOUS_ARTISTS_MBID:
            item.name = VARIOUS_ARTISTS_NAME
        # no existing item matched: insert item
        db_id = await self.mass.music.database.insert(
            self.db_table,
            {
                "name": item.name,
                "sort_name": item.sort_name,
                "favorite": item.favorite,
                "external_ids": serialize_to_json(item.external_ids),
                "metadata": serialize_to_json(item.metadata),
                "search_name": create_safe_string(item.name, True, True),
                "search_sort_name": create_safe_string(item.sort_name or "", True, True),
                "timestamp_added": int(item.date_added.timestamp()) if item.date_added else UNSET,
            },
        )
        # update/set provider_mappings table
        await self.set_provider_mappings(db_id, item.provider_mappings)
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
            update = self.artist_from_item_mapping(update)
            metadata = cur_item.metadata
        else:
            metadata = update.metadata if overwrite else cur_item.metadata.update(update.metadata)
        cur_item.external_ids.update(update.external_ids)
        # enforce various artists name + id
        mbid = cur_item.mbid
        if (not mbid or overwrite) and getattr(update, "mbid", None):
            if compare_strings(update.name, VARIOUS_ARTISTS_NAME):
                update.mbid = VARIOUS_ARTISTS_MBID
            if update.mbid == VARIOUS_ARTISTS_MBID:
                update.name = VARIOUS_ARTISTS_NAME

        name = update.name if overwrite else cur_item.name
        sort_name = update.sort_name if overwrite else cur_item.sort_name or update.sort_name
        await self.mass.music.database.update(
            self.db_table,
            {"item_id": db_id},
            {
                "name": name,
                "sort_name": sort_name,
                "external_ids": serialize_to_json(
                    update.external_ids if overwrite else cur_item.external_ids
                ),
                "metadata": serialize_to_json(metadata),
                "search_name": create_safe_string(name, True, True),
                "search_sort_name": create_safe_string(sort_name or "", True, True),
                "timestamp_added": int(update.date_added.timestamp())
                if update.date_added
                else UNSET,
            },
        )
        self.logger.debug("updated %s in database: %s", update.name, db_id)
        # update/set provider_mappings table
        provider_mappings = (
            update.provider_mappings
            if overwrite
            else {*update.provider_mappings, *cur_item.provider_mappings}
        )
        await self.set_provider_mappings(db_id, provider_mappings, overwrite)
        self.logger.debug("updated %s in database: (id %s)", update.name, db_id)

    async def radio_mode_base_tracks(
        self,
        item: Artist,
        preferred_provider_instances: list[str] | None = None,
    ) -> list[Track]:
        """
        Get the list of base tracks from the controller used to calculate the dynamic radio.

        :param item: The Artist to get base tracks for.
        :param preferred_provider_instances: List of preferred provider instance IDs to use.
        """
        return await self.tracks(
            item.item_id,
            item.provider,
            in_library_only=False,
        )

    async def match_provider(
        self, db_artist: Artist, provider: MusicProvider, strict: bool = True
    ) -> list[ProviderMapping]:
        """
        Try to find match on (streaming) provider for the provided (database) artist.

        This is used to link objects of different providers/qualities together.
        """
        self.logger.debug("Trying to match artist %s on provider %s", db_artist.name, provider.name)
        matches: list[ProviderMapping] = []
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
            search_str = f"{db_artist.name} - {ref_track.name}"
            search_results = await self.mass.music.tracks.search(search_str, provider.domain)
            for search_result_item in search_results:
                if not compare_strings(search_result_item.name, ref_track.name, strict=strict):
                    continue
                # get matching artist from track
                for search_item_artist in search_result_item.artists:
                    if not compare_strings(search_item_artist.name, db_artist.name, strict=strict):
                        continue
                    # 100% track match
                    # get full artist details so we have all metadata
                    prov_artist = await self.get_provider_item(
                        search_item_artist.item_id,
                        search_item_artist.provider,
                        fallback=search_item_artist,
                    )
                    # 100% match
                    matches.extend(prov_artist.provider_mappings)
                    if matches:
                        return matches
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
            search_str = f"{db_artist.name} - {ref_album.name}"
            search_result_albums = await self.mass.music.albums.search(search_str, provider.domain)
            for search_result_album in search_result_albums:
                if not search_result_album.artists:
                    continue
                if not compare_strings(search_result_album.name, ref_album.name, strict=strict):
                    continue
                # artist must match 100%
                if not compare_artist(db_artist, search_result_album.artists[0], strict=strict):
                    continue
                # 100% match
                # get full artist details so we have all metadata
                prov_artist = await self.get_provider_item(
                    search_result_album.artists[0].item_id,
                    search_result_album.artists[0].provider,
                    fallback=search_result_album.artists[0],
                )
                matches.extend(prov_artist.provider_mappings)
                if matches:
                    return matches
        if not matches:
            self.logger.debug(
                "Could not find match for Artist %s on provider %s",
                db_artist.name,
                provider.name,
            )
        return matches

    async def match_providers(self, db_artist: Artist) -> None:
        """Try to find matching artists on all providers for the provided (database) item_id.

        This is used to link objects of different providers together.
        """
        if db_artist.provider != "library":
            return  # Matching only supported for database items

        # try to find match on all providers

        cur_provider_domains = {
            x.provider_domain for x in db_artist.provider_mappings if x.available
        }
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
            if match := await self.match_provider(db_artist, provider):
                # 100% match, we update the db with the additional provider mapping(s)
                await self.add_provider_mappings(db_artist.item_id, match)
                cur_provider_domains.add(provider.domain)

    def artist_from_item_mapping(self, item: ItemMapping) -> Artist:
        """Create an Artist object from an ItemMapping object."""
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
