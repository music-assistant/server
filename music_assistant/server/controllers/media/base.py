"""Base (ABC) MediaType specific controller."""

from __future__ import annotations

import logging
from abc import ABCMeta, abstractmethod
from contextlib import suppress
from time import time
from typing import TYPE_CHECKING, Any, Generic, TypeVar

from music_assistant.common.helpers.json import json_loads, serialize_to_json
from music_assistant.common.models.enums import EventType, ExternalID, MediaType, ProviderFeature
from music_assistant.common.models.errors import InvalidDataError, MediaNotFoundError
from music_assistant.common.models.media_items import (
    Album,
    Artist,
    ItemMapping,
    MediaItemType,
    PagedItems,
    ProviderMapping,
    Track,
    media_from_dict,
)
from music_assistant.constants import DB_TABLE_PROVIDER_MAPPINGS, ROOT_LOGGER_NAME

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Iterable, Mapping

    from music_assistant.server import MusicAssistant

ItemCls = TypeVar("ItemCls", bound="MediaItemType")

REFRESH_INTERVAL = 60 * 60 * 24 * 30
JSON_KEYS = ("artists", "metadata", "provider_mappings", "external_ids")


class MediaControllerBase(Generic[ItemCls], metaclass=ABCMeta):
    """Base model for controller managing a MediaType."""

    media_type: MediaType
    item_cls: MediaItemType
    db_table: str

    def __init__(self, mass: MusicAssistant) -> None:
        """Initialize class."""
        self.mass = mass
        self.base_query = f"SELECT * FROM {self.db_table}"
        self.logger = logging.getLogger(f"{ROOT_LOGGER_NAME}.music.{self.media_type.value}")

    @abstractmethod
    async def add_item_to_library(self, item: ItemCls, metadata_lookup: bool = True) -> ItemCls:
        """Add item to library and return the database item."""
        raise NotImplementedError

    @abstractmethod
    async def update_item_in_library(
        self, item_id: str | int, update: ItemCls, overwrite: bool = False
    ) -> ItemCls:
        """Update existing library record in the database."""

    async def remove_item_from_library(self, item_id: str | int) -> None:
        """Delete library record from the database."""
        db_id = int(item_id)  # ensure integer
        library_item = await self.get_library_item(db_id)
        assert library_item, f"Item does not exist: {db_id}"
        # delete item
        await self.mass.music.database.delete(
            self.db_table,
            {"item_id": db_id},
        )
        # update provider_mappings table
        await self.mass.music.database.delete(
            DB_TABLE_PROVIDER_MAPPINGS,
            {"media_type": self.media_type.value, "item_id": db_id},
        )
        # NOTE: this does not delete any references to this item in other records,
        # this is handled/overridden in the mediatype specific controllers
        self.mass.signal_event(EventType.MEDIA_ITEM_DELETED, library_item.uri, library_item)
        self.logger.debug("deleted item with id %s from database", db_id)

    async def library_items(
        self,
        favorite: bool | None = None,
        search: str | None = None,
        limit: int = 500,
        offset: int = 0,
        order_by: str = "sort_name",
        extra_query: str | None = None,
        extra_query_params: dict[str, Any] | None = None,
    ) -> PagedItems:
        """Get in-database items."""
        sql_query = self.base_query
        params = extra_query_params or {}
        query_parts: list[str] = []
        if extra_query:
            # prevent duplicate where statement
            if extra_query.lower().startswith("where "):
                extra_query = extra_query[5:]
            query_parts.append(extra_query)
        if search:
            params["search"] = f"%{search}%"
            if self.media_type in (MediaType.ALBUM, MediaType.TRACK):
                query_parts.append(
                    f"({self.db_table}.name LIKE :search "
                    f" OR {self.db_table}.sort_name LIKE :search"
                    f" OR {self.db_table}.artists LIKE :search)"
                )
            else:
                query_parts.append(f"{self.db_table}.name LIKE :search")
        if favorite is not None:
            query_parts.append(f"{self.db_table}.favorite = :favorite")
            params["favorite"] = favorite
        if query_parts:
            # concetenate all where queries
            sql_query += " WHERE " + " AND ".join(query_parts)
        sql_query += f" ORDER BY {order_by}"
        items = await self._get_library_items_by_query(
            sql_query, params, limit=limit, offset=offset
        )
        count = len(items)
        if 0 < count < limit:
            total = offset + count
        else:
            total = await self.mass.music.database.get_count_from_query(sql_query, params)
        return PagedItems(items=items, count=count, limit=limit, offset=offset, total=total)

    async def iter_library_items(
        self,
        favorite: bool | None = None,
        search: str | None = None,
        order_by: str = "sort_name",
    ) -> AsyncGenerator[ItemCls, None]:
        """Iterate all in-database items."""
        limit: int = 500
        offset: int = 0
        while True:
            next_items = await self.library_items(
                favorite=favorite,
                search=search,
                limit=limit,
                offset=offset,
                order_by=order_by,
            )
            for item in next_items.items:
                yield item
            if next_items.count < limit:
                break
            offset += limit

    async def get(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
        force_refresh: bool = False,
        lazy: bool = True,
        details: ItemCls = None,
        add_to_library: bool = False,
    ) -> ItemCls:
        """Return (full) details for a single media item."""
        if provider_instance_id_or_domain == "database":
            # backwards compatibility - to remove when 2.0 stable is released
            provider_instance_id_or_domain = "library"
        # always prefer the full library item if we have it
        library_item = await self.get_library_item_by_prov_id(
            item_id,
            provider_instance_id_or_domain,
        )
        if library_item and (time() - (library_item.metadata.last_refresh or 0)) > REFRESH_INTERVAL:
            # it's been too long since the full metadata was last retrieved (or never at all)
            force_refresh = True
            add_to_library = True
        if library_item and force_refresh:
            # get (first) provider item id belonging to this library item
            add_to_library = True
            provider_instance_id_or_domain, item_id = await self.get_provider_mapping(library_item)
        elif library_item:
            # we have a library item and no refreshing is needed, return the results!
            return library_item
        if (
            provider_instance_id_or_domain
            and item_id
            and (
                not details
                or isinstance(details, ItemMapping)
                or (add_to_library and details.provider == "library")
            )
        ):
            # grab full details from the provider
            details = await self.get_provider_item(
                item_id,
                provider_instance_id_or_domain,
                force_refresh=force_refresh,
                fallback=details,
            )
        if not details:
            # we couldn't get a match from any of the providers, raise error
            msg = f"Item not found: {provider_instance_id_or_domain}/{item_id}"
            raise MediaNotFoundError(msg)
        if not add_to_library:
            # return the provider item as-is
            return details
        # create task to add the item to the library,
        # including matching metadata etc. takes some time
        # in 99% of the cases we just return lazy because we want the details as fast as possible
        # only if we really need to wait for the result (e.g. to prevent race conditions),
        # we can set lazy to false and we await the job to complete.
        task_id = f"add_{self.media_type.value}.{details.provider}.{details.item_id}"
        add_task = self.mass.create_task(self.add_item_to_library, item=details, task_id=task_id)
        if not lazy:
            await add_task
            return add_task.result()

        return library_item or details

    async def search(
        self,
        search_query: str,
        provider_instance_id_or_domain: str,
        limit: int = 25,
    ) -> list[ItemCls]:
        """Search database or provider with given query."""
        # create safe search string
        search_query = search_query.replace("/", " ").replace("'", "")
        if provider_instance_id_or_domain == "library":
            return [
                self.item_cls.from_dict(self._parse_db_row(db_row))
                for db_row in await self.mass.music.database.search(self.db_table, search_query)
            ]
        prov = self.mass.get_provider(provider_instance_id_or_domain)
        if prov is None:
            return []
        if ProviderFeature.SEARCH not in prov.supported_features:
            return []
        if not prov.library_supported(self.media_type):
            # assume library supported also means that this mediatype is supported
            return []

        # prefer cache items (if any)
        cache_key = f"{prov.instance_id}.search.{self.media_type.value}.{search_query}.{limit}"
        if cache := await self.mass.cache.get(cache_key):
            return [media_from_dict(x) for x in cache]
        # no items in cache - get listing from provider
        searchresult = await prov.search(
            search_query,
            [self.media_type],
            limit,
        )
        if self.media_type == MediaType.ARTIST:
            items = searchresult.artists
        elif self.media_type == MediaType.ALBUM:
            items = searchresult.albums
        elif self.media_type == MediaType.TRACK:
            items = searchresult.tracks
        elif self.media_type == MediaType.PLAYLIST:
            items = searchresult.playlists
        else:
            items = searchresult.radio
        # store (serializable items) in cache
        if prov.is_streaming_provider:  # do not cache filesystem results
            self.mass.create_task(
                self.mass.cache.set(cache_key, [x.to_dict() for x in items], expiration=86400 * 7)
            )
        return items

    async def get_provider_mapping(self, item: ItemCls) -> tuple[str, str]:
        """Return (first) provider and item id."""
        if not getattr(item, "provider_mappings", None):
            # make sure we have a full object
            item = await self.get_library_item(item.item_id)
        for prefer_unique in (True, False):
            for prov_mapping in item.provider_mappings:
                # returns the first provider that is available
                if not prov_mapping.available:
                    continue
                if provider := self.mass.get_provider(prov_mapping.provider_instance):
                    if prefer_unique and provider.is_streaming_provider:
                        continue
                    return (prov_mapping.provider_instance, prov_mapping.item_id)
        return (None, None)

    async def get_library_item(self, item_id: int | str) -> ItemCls:
        """Get record by id."""
        db_id = int(item_id)  # ensure integer
        match = {"item_id": db_id}
        if db_row := await self.mass.music.database.get_row(self.db_table, match):
            return self.item_cls.from_dict(self._parse_db_row(db_row))
        msg = f"{self.media_type.value} not found in library: {db_id}"
        raise MediaNotFoundError(msg)

    async def get_library_item_by_prov_id(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
    ) -> ItemCls | None:
        """Get the library item for the given provider_instance."""
        assert item_id
        assert provider_instance_id_or_domain
        if provider_instance_id_or_domain == "library":
            return await self.get_library_item(item_id)
        for item in await self.get_library_items_by_prov_id(
            provider_instance_id_or_domain,
            provider_item_ids=(item_id,),
        ):
            return item
        return None

    async def get_library_item_by_prov_mappings(
        self,
        provider_mappings: list[ProviderMapping],
    ) -> ItemCls | None:
        """Get the library item for the given provider_instance."""
        # always prefer provider instance first
        for mapping in provider_mappings:
            for item in await self.get_library_items_by_prov_id(
                mapping.provider_instance,
                provider_item_ids=(mapping.item_id,),
            ):
                return item
        # check by domain too
        for mapping in provider_mappings:
            for item in await self.get_library_items_by_prov_id(
                mapping.provider_domain,
                provider_item_ids=(mapping.item_id,),
            ):
                return item
        return None

    async def get_library_item_by_external_id(
        self, external_id: str, external_id_type: ExternalID | None = None
    ) -> ItemCls | None:
        """Get the library item for the given external id."""
        query = self.base_query + f" WHERE {self.db_table}.external_ids LIKE :external_id_str"
        if external_id_type:
            external_id_str = f'%("{external_id_type}", "{external_id}")%'
        else:
            external_id_str = f'%"{external_id}"%'
        for item in await self._get_library_items_by_query(
            query=query, query_params={"external_id_str": external_id_str}
        ):
            return item
        return None

    async def get_library_item_by_external_ids(
        self, external_ids: set[tuple[ExternalID, str]]
    ) -> ItemCls | None:
        """Get the library item for (one of) the given external ids."""
        for external_id_type, external_id in external_ids:
            if match := await self.get_library_item_by_external_id(external_id, external_id_type):
                return match
        return None

    async def get_library_items_by_prov_id(
        self,
        provider_instance_id_or_domain: str,
        provider_item_ids: tuple[str, ...] | None = None,
        limit: int = 500,
        offset: int = 0,
    ) -> list[ItemCls]:
        """Fetch all records from library for given provider."""
        if provider_instance_id_or_domain == "library":
            if provider_item_ids is not None:
                prov_ids_string = str(tuple(int(x) for x in provider_item_ids))
                if prov_ids_string.endswith(",)"):
                    prov_ids_string = prov_ids_string.replace(",)", ")")
                extra_query = f"item_id in {prov_ids_string}"
            else:
                extra_query = None
            paged_list = await self.library_items(
                limit=limit, offset=offset, extra_query=extra_query
            )
            return paged_list.items

        # we use the separate provider_mappings table to perform quick lookups
        # from provider id's to database id's because this is faster
        # (and more compatible) than querying the provider_mappings json column
        subquery = (
            f"SELECT item_id FROM {DB_TABLE_PROVIDER_MAPPINGS} WHERE "
            f" media_type = '{self.media_type.value}' AND "
            f"(provider_instance = '{provider_instance_id_or_domain}' "
            f"OR provider_domain = '{provider_instance_id_or_domain}')"
        )
        if provider_item_ids is not None:
            prov_ids = str(tuple(provider_item_ids))
            if prov_ids.endswith(",)"):
                prov_ids = prov_ids.replace(",)", ")")
            subquery += f" AND provider_item_id in {prov_ids}"
        # final query is a where query from the subquery
        # that queries the provider_mappings table
        query = f"WHERE {self.db_table}.item_id in ({subquery})"
        paged_list = await self.library_items(limit=limit, offset=offset, extra_query=query)
        return paged_list.items

    async def iter_library_items_by_prov_id(
        self,
        provider_instance_id_or_domain: str,
        provider_item_ids: tuple[str, ...] | None = None,
    ) -> AsyncGenerator[ItemCls, None]:
        """Iterate all records from database for given provider."""
        limit: int = 500
        offset: int = 0
        while True:
            next_items = await self.get_library_items_by_prov_id(
                provider_instance_id_or_domain,
                provider_item_ids=provider_item_ids,
                limit=limit,
                offset=offset,
            )
            for item in next_items:
                yield item
            if len(next_items) < limit:
                break
            offset += limit

    async def set_favorite(self, item_id: str | int, favorite: bool) -> None:
        """Set the favorite bool on a database item."""
        db_id = int(item_id)  # ensure integer
        library_item = await self.get_library_item(db_id)
        if library_item.favorite == favorite:
            return
        match = {"item_id": db_id}
        await self.mass.music.database.update(self.db_table, match, {"favorite": favorite})
        library_item = await self.get_library_item(db_id)
        self.mass.signal_event(EventType.MEDIA_ITEM_UPDATED, library_item.uri, library_item)

    async def get_provider_item(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
        force_refresh: bool = False,
        fallback: ItemMapping | ItemCls = None,
    ) -> ItemCls:
        """Return item details for the given provider item id."""
        cache_key = (
            f"provider_item.{self.media_type.value}.{provider_instance_id_or_domain}.{item_id}"
        )
        if provider_instance_id_or_domain == "library":
            return await self.get_library_item(item_id)
        if not force_refresh and (cache := await self.mass.cache.get(cache_key)):
            return self.item_cls.from_dict(cache)
        if provider := self.mass.get_provider(provider_instance_id_or_domain):
            with suppress(MediaNotFoundError):
                if item := await provider.get_item(self.media_type, item_id):
                    await self.mass.cache.set(cache_key, item.to_dict())
                    return item
        # if we reach this point all possibilities failed and the item could not be found.
        # There is a possibility that the (streaming) provider changed the id of the item
        # so we return the previous details (if we have any) marked as unavailable, so
        # at least we have the possibility to sort out the new id through matching logic.
        fallback = fallback or await self.get_library_item_by_prov_id(
            item_id, provider_instance_id_or_domain
        )
        if fallback and not (isinstance(fallback, ItemMapping) and self.item_cls in (Track, Album)):
            # simply return the fallback item
            # NOTE: we only accept ItemMapping as fallback for flat items
            # so not for tracks and albums (which rely on other objects)
            return fallback
        # all options exhausted, we really can not find this item
        msg = (
            f"{self.media_type.value}://{item_id} not "
            f"found on provider {provider_instance_id_or_domain}"
        )
        raise MediaNotFoundError(msg)

    async def add_provider_mapping(
        self, item_id: str | int, provider_mapping: ProviderMapping
    ) -> None:
        """Add provider mapping to existing library item."""
        db_id = int(item_id)  # ensure integer
        library_item = await self.get_library_item(db_id)
        # ignore if the mapping is already present
        if provider_mapping in library_item.provider_mappings:
            return
        # update item's db record
        library_item.provider_mappings.add(provider_mapping)
        await self.mass.music.database.update(
            self.db_table,
            {"item_id": db_id},
            {
                "provider_mappings": serialize_to_json(library_item.provider_mappings),
            },
        )
        # update provider_mappings table
        await self._set_provider_mappings(
            item_id=item_id, provider_mappings=library_item.provider_mappings
        )

    async def remove_provider_mapping(
        self, item_id: str | int, provider_instance_id: str, provider_item_id: str
    ) -> None:
        """Remove provider mapping(s) from item."""
        db_id = int(item_id)  # ensure integer
        try:
            library_item = await self.get_library_item(db_id)
        except MediaNotFoundError:
            # edge case: already deleted / race condition
            return

        # update provider_mappings table
        await self.mass.music.database.delete(
            DB_TABLE_PROVIDER_MAPPINGS,
            {
                "media_type": self.media_type.value,
                "item_id": db_id,
                "provider_instance": provider_instance_id,
                "provider_item_id": provider_item_id,
            },
        )

        # update the item in db (provider_mappings column only)
        library_item.provider_mappings = {
            x
            for x in library_item.provider_mappings
            if x.provider_instance != provider_instance_id and x.item_id != provider_item_id
        }
        match = {"item_id": db_id}
        if library_item.provider_mappings:
            await self.mass.music.database.update(
                self.db_table,
                match,
                {"provider_mappings": serialize_to_json(library_item.provider_mappings)},
            )
            self.logger.debug(
                "removed provider_mapping %s/%s from item id %s",
                provider_instance_id,
                provider_item_id,
                db_id,
            )
            self.mass.signal_event(EventType.MEDIA_ITEM_UPDATED, library_item.uri, library_item)
        else:
            # remove item if it has no more providers
            with suppress(AssertionError):
                await self.remove_item_from_library(db_id)

    async def remove_provider_mappings(self, item_id: str | int, provider_instance_id: str) -> None:
        """Remove all provider mappings from an item."""
        db_id = int(item_id)  # ensure integer
        try:
            library_item = await self.get_library_item(db_id)
        except MediaNotFoundError:
            # edge case: already deleted / race condition
            return

        # update provider_mappings table
        await self.mass.music.database.delete(
            DB_TABLE_PROVIDER_MAPPINGS,
            {
                "media_type": self.media_type.value,
                "item_id": db_id,
                "provider_instance": provider_instance_id,
            },
        )

        # update the item in db (provider_mappings column only)
        library_item.provider_mappings = {
            x for x in library_item.provider_mappings if x.provider_instance != provider_instance_id
        }
        match = {"item_id": db_id}
        if library_item.provider_mappings:
            await self.mass.music.database.update(
                self.db_table,
                match,
                {"provider_mappings": serialize_to_json(library_item.provider_mappings)},
            )
            self.logger.debug(
                "removed all provider mappings for provider %s from item id %s",
                provider_instance_id,
                db_id,
            )
            self.mass.signal_event(EventType.MEDIA_ITEM_UPDATED, library_item.uri, library_item)
        else:
            # remove item if it has no more providers
            with suppress(AssertionError):
                await self.remove_item_from_library(db_id)

    async def dynamic_tracks(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
        limit: int = 25,
    ) -> list[Track]:
        """Return a dynamic list of tracks based on the given item."""
        ref_item = await self.get(item_id, provider_instance_id_or_domain)
        for prov_mapping in ref_item.provider_mappings:
            prov = self.mass.get_provider(prov_mapping.provider_instance)
            if prov is None:
                continue
            if ProviderFeature.SIMILAR_TRACKS not in prov.supported_features:
                continue
            return await self._get_provider_dynamic_tracks(
                prov_mapping.item_id,
                prov_mapping.provider_instance,
                limit=limit,
            )
        # Fallback to the default implementation
        return await self._get_dynamic_tracks(ref_item)

    @abstractmethod
    async def _get_provider_dynamic_tracks(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
        limit: int = 25,
    ) -> list[Track]:
        """Generate a dynamic list of tracks based on the item's content."""

    @abstractmethod
    async def _get_dynamic_tracks(self, media_item: ItemCls, limit: int = 25) -> list[Track]:
        """Get dynamic list of tracks for given item, fallback/default implementation."""

    async def _get_library_items_by_query(
        self,
        query: str,
        query_params: dict | None = None,
        limit: int = 500,
        offset: int = 0,
    ) -> list[ItemCls]:
        """Fetch MediaItem records from database given a custom (WHERE) clause."""
        if query_params is None:
            query_params = {}
        return [
            self.item_cls.from_dict(self._parse_db_row(db_row))
            for db_row in await self.mass.music.database.get_rows_from_query(
                query, query_params, limit=limit, offset=offset
            )
        ]

    async def _set_provider_mappings(
        self, item_id: str | int, provider_mappings: Iterable[ProviderMapping]
    ) -> None:
        """Update the provider_items table for the media item."""
        db_id = int(item_id)  # ensure integer
        # get current mappings (if any)
        cur_mappings: set[ProviderMapping] = set()
        match = {"media_type": self.media_type.value, "item_id": db_id}
        for db_row in await self.mass.music.database.get_rows(DB_TABLE_PROVIDER_MAPPINGS, match):
            cur_mappings.add(
                ProviderMapping(
                    item_id=db_row["provider_item_id"],
                    provider_domain=db_row["provider_domain"],
                    provider_instance=db_row["provider_instance"],
                )
            )
        # delete removed mappings
        for prov_mapping in cur_mappings:
            if prov_mapping not in set(provider_mappings):
                await self.mass.music.database.delete(
                    DB_TABLE_PROVIDER_MAPPINGS,
                    {
                        **match,
                        "provider_domain": prov_mapping.provider_domain,
                        "provider_instance": prov_mapping.provider_instance,
                        "provider_item_id": prov_mapping.item_id,
                    },
                )
        # add entries
        for provider_mapping in provider_mappings:
            await self.mass.music.database.insert_or_replace(
                DB_TABLE_PROVIDER_MAPPINGS,
                {
                    **match,
                    "provider_domain": provider_mapping.provider_domain,
                    "provider_instance": provider_mapping.provider_instance,
                    "provider_item_id": provider_mapping.item_id,
                },
            )

    def _get_provider_mappings(
        self,
        org_item: ItemCls,
        update_item: ItemCls | ItemMapping | None = None,
        overwrite: bool = False,
    ) -> set[ProviderMapping]:
        """Get/merge provider mappings for an item."""
        if not update_item or isinstance(update_item, ItemMapping):
            return org_item.provider_mappings
        if overwrite and update_item.provider_mappings:
            return update_item.provider_mappings
        return {*update_item.provider_mappings, *org_item.provider_mappings}

    async def _get_artist_mappings(
        self,
        org_item: Album | Track,
        update_item: Album | Track | ItemMapping | None = None,
        overwrite: bool = False,
    ) -> list[ItemMapping]:
        """Extract (database) album/track artist(s) as ItemMapping."""
        artist_mappings: list[ItemMapping] = []
        if update_item is None or isinstance(update_item, ItemMapping):
            source_artists = org_item.artists
        elif overwrite and update_item.artists:
            source_artists = update_item.artists
        else:
            source_artists = org_item.artists + update_item.artists
        for artist in source_artists:
            artist_mapping = await self._get_artist_mapping(artist)
            if artist_mapping not in artist_mappings:
                artist_mappings.append(artist_mapping)
        return artist_mappings

    async def _get_artist_mapping(self, artist: Artist | ItemMapping) -> ItemMapping:
        """Extract (database) track artist as ItemMapping."""
        if artist.provider == "library":
            if isinstance(artist, ItemMapping):
                return artist
            return ItemMapping.from_item(artist)

        if db_artist := await self.mass.music.artists.get_library_item_by_prov_id(
            artist.item_id, artist.provider
        ):
            return ItemMapping.from_item(db_artist)

        # try to request the full item
        artist = await self.mass.music.artists.get_provider_item(
            artist.item_id, artist.provider, fallback=artist
        )
        with suppress(MediaNotFoundError, AssertionError, InvalidDataError):
            db_artist = await self.mass.music.artists.add_item_to_library(
                artist, metadata_lookup=False
            )
            return ItemMapping.from_item(db_artist)
        # fallback to just the provider item
        # this can happen for unavailable items
        if isinstance(artist, ItemMapping):
            return artist
        return ItemMapping.from_item(artist)

    @staticmethod
    def _parse_db_row(db_row: Mapping) -> dict[str, Any]:
        """Parse raw db Mapping into a dict."""
        db_row_dict = dict(db_row)
        db_row_dict["provider"] = "library"
        for key in JSON_KEYS:
            if key in db_row_dict and db_row_dict[key] not in (None, ""):
                db_row_dict[key] = json_loads(db_row_dict[key])
        if "favorite" in db_row_dict:
            db_row_dict["favorite"] = bool(db_row_dict["favorite"])
        if "item_id" in db_row_dict:
            db_row_dict["item_id"] = str(db_row_dict["item_id"])
        if "album" not in db_row_dict and (album_id := db_row_dict.get("album_id")):
            # handle joined result with (limited) album data as ItemMapping
            db_row_dict["album"] = {
                "media_type": "album",
                "item_id": str(album_id),
                "provider": "library",
                "name": db_row_dict["album_name"],
                "version": db_row_dict["album_version"],
            }
            db_row_dict["album"] = ItemMapping.from_dict(db_row_dict["album"])
            if db_row_dict["album_metadata"]:
                # copy album image
                album_metadata = json_loads(db_row_dict["album_metadata"])
                if album_metadata and album_metadata["images"]:
                    db_row_dict["metadata"]["images"] = album_metadata["images"]
        return db_row_dict
