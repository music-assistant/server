"""Base (ABC) MediaType specific controller."""

from __future__ import annotations

import asyncio
import logging
from abc import ABCMeta, abstractmethod
from collections.abc import Iterable
from contextlib import suppress
from time import time
from typing import TYPE_CHECKING, Any, Generic, TypeVar

from music_assistant.common.helpers.json import json_loads, serialize_to_json
from music_assistant.common.models.enums import EventType, ExternalID, MediaType, ProviderFeature
from music_assistant.common.models.errors import MediaNotFoundError, ProviderUnavailableError
from music_assistant.common.models.media_items import (
    Album,
    ItemMapping,
    MediaItemType,
    ProviderMapping,
    Track,
    media_from_dict,
)
from music_assistant.constants import (
    DB_TABLE_ARTISTS,
    DB_TABLE_PLAYLOG,
    DB_TABLE_PROVIDER_MAPPINGS,
    MASS_LOGGER_NAME,
)
from music_assistant.server.helpers.compare import compare_media_item

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Mapping

    from music_assistant.server import MusicAssistant

ItemCls = TypeVar("ItemCls", bound="MediaItemType")

REFRESH_INTERVAL = 60 * 60 * 24 * 30
JSON_KEYS = ("artists", "album", "metadata", "provider_mappings", "external_ids")

SORT_KEYS = {
    "name": "name COLLATE NOCASE ASC",
    "name_desc": "name COLLATE NOCASE DESC",
    "sort_name": "sort_name COLLATE NOCASE ASC",
    "sort_name_desc": "sort_name COLLATE NOCASE DESC",
    "timestamp_added": "timestamp_added ASC",
    "timestamp_added_desc": "timestamp_added DESC",
    "timestamp_modified": "timestamp_modified ASC",
    "timestamp_modified_desc": "timestamp_modified DESC",
    "last_played": "last_played ASC",
    "last_played_desc": "last_played DESC",
    "play_count": "play_count ASC",
    "play_count_desc": "play_count DESC",
    "year": "year ASC",
    "year_desc": "year DESC",
    "position": "position ASC",
    "position_desc": "position DESC",
    "random": "RANDOM()",
    "random_play_count": "random(), play_count ASC",
    "random_fast": "play_count ASC",  # this one is handled with a special query
}


class MediaControllerBase(Generic[ItemCls], metaclass=ABCMeta):
    """Base model for controller managing a MediaType."""

    media_type: MediaType
    item_cls: MediaItemType
    db_table: str

    def __init__(self, mass: MusicAssistant) -> None:
        """Initialize class."""
        self.mass = mass
        self.base_query = (
            f"SELECT DISTINCT {self.db_table}.* FROM {self.db_table} "
            f"LEFT JOIN {DB_TABLE_PROVIDER_MAPPINGS} ON "
            f"{DB_TABLE_PROVIDER_MAPPINGS}.item_id = {self.db_table}.item_id "
            f"AND media_type = '{self.media_type}'"
        )
        self.logger = logging.getLogger(f"{MASS_LOGGER_NAME}.music.{self.media_type.value}")
        # register (base) api handlers
        self.api_base = api_base = f"{self.media_type}s"
        self.mass.register_api_command(f"music/{api_base}/count", self.library_count)
        self.mass.register_api_command(f"music/{api_base}/library_items", self.library_items)
        self.mass.register_api_command(f"music/{api_base}/get", self.get)
        self.mass.register_api_command(f"music/{api_base}/get_{self.media_type}", self.get)
        self.mass.register_api_command(f"music/{api_base}/add", self.add_item_to_library)
        self.mass.register_api_command(f"music/{api_base}/update", self.update_item_in_library)
        self.mass.register_api_command(f"music/{api_base}/remove", self.remove_item_from_library)
        self._db_add_lock = asyncio.Lock()

    async def add_item_to_library(
        self, item: Track, metadata_lookup: bool = True, overwrite_existing: bool = False
    ) -> ItemCls:
        """Add item to library and return the new (or updated) database item."""
        new_item = False
        # grab additional metadata
        if metadata_lookup:
            await self.mass.metadata.get_metadata(item)
        # check for existing item first
        library_id = None
        if cur_item := await self.get_library_item_by_prov_id(item.item_id, item.provider):
            # existing item match by provider id
            await self._update_library_item(cur_item.item_id, item, overwrite=overwrite_existing)
            library_id = cur_item.item_id
        elif cur_item := await self.get_library_item_by_external_ids(item.external_ids):
            # existing item match by external id
            await self._update_library_item(cur_item.item_id, item, overwrite=overwrite_existing)
            library_id = cur_item.item_id
        else:
            # search by (exact) name match
            query = f"WHERE {self.db_table}.name = :name OR {self.db_table}.sort_name = :sort_name"
            query_params = {"name": item.name, "sort_name": item.sort_name}
            async for db_item in self.iter_library_items(
                extra_query=query, extra_query_params=query_params
            ):
                if compare_media_item(db_item, item, True):
                    # existing item found: update it
                    await self._update_library_item(
                        db_item.item_id, item, overwrite=overwrite_existing
                    )
                    library_id = db_item.item_id
                    break
        if library_id is None:
            # actually add a new item in the library db
            async with self._db_add_lock:
                library_id = await self._add_library_item(item)
                new_item = True
        # also fetch same track on all providers (will also get other quality versions)
        if metadata_lookup:
            library_item = await self.get_library_item(library_id)
            await self._match(library_item)
        # return final library_item after all match/metadata actions
        library_item = await self.get_library_item(library_id)
        self.mass.signal_event(
            EventType.MEDIA_ITEM_ADDED if new_item else EventType.MEDIA_ITEM_UPDATED,
            library_item.uri,
            library_item,
        )
        return library_item

    async def update_item_in_library(
        self, item_id: str | int, update: ItemCls, overwrite: bool = False
    ) -> ItemCls:
        """Update existing library record in the database."""
        await self._update_library_item(item_id, update, overwrite=overwrite)
        # return the updated object
        library_item = await self.get_library_item(item_id)
        self.mass.signal_event(
            EventType.MEDIA_ITEM_UPDATED,
            library_item.uri,
            library_item,
        )
        return library_item

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
        # cleanup playlog table
        await self.mass.music.database.delete(
            DB_TABLE_PLAYLOG,
            {
                "media_type": self.media_type.value,
                "item_id": db_id,
                "provider": "library",
            },
        )
        for prov_mapping in library_item.provider_mappings:
            await self.mass.music.database.delete(
                DB_TABLE_PLAYLOG,
                {
                    "media_type": self.media_type.value,
                    "item_id": prov_mapping.item_id,
                    "provider": prov_mapping.provider_instance,
                },
            )
        # NOTE: this does not delete any references to this item in other records,
        # this is handled/overridden in the mediatype specific controllers
        self.mass.signal_event(EventType.MEDIA_ITEM_DELETED, library_item.uri, library_item)
        self.logger.debug("deleted item with id %s from database", db_id)

    async def library_count(self, favorite_only: bool = False) -> int:
        """Return the total number of items in the library."""
        if favorite_only:
            sql_query = f"SELECT item_id FROM {self.db_table} WHERE favorite = 1"
            return await self.mass.music.database.get_count_from_query(sql_query)
        return await self.mass.music.database.get_count(self.db_table)

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
    ) -> list[ItemCls]:
        """Get in-database items."""
        # create special performant random query
        if order_by == "random_fast" and not extra_query:
            extra_query = (
                f"{self.db_table}.rowid > (ABS(RANDOM()) % "
                f"(SELECT max({self.db_table}.rowid) FROM {self.db_table}))"
            )
        return await self._get_library_items_by_query(
            favorite=favorite,
            search=search,
            limit=limit,
            offset=offset,
            order_by=order_by,
            provider=provider,
            extra_query=extra_query,
            extra_query_params=extra_query_params,
        )

    async def iter_library_items(
        self,
        favorite: bool | None = None,
        search: str | None = None,
        order_by: str = "sort_name",
        extra_query: str | None = None,
        extra_query_params: dict[str, Any] | None = None,
    ) -> AsyncGenerator[ItemCls, None]:
        """Iterate all in-database items."""
        limit: int = 500
        offset: int = 0
        while True:
            next_items = await self._get_library_items_by_query(
                favorite=favorite,
                search=search,
                limit=limit,
                offset=offset,
                order_by=order_by,
                extra_query=extra_query,
                extra_query_params=extra_query_params,
            )
            for item in next_items:
                yield item
            if len(next_items) < limit:
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
        metadata_lookup = False
        # always prefer the full library item if we have it
        library_item = await self.get_library_item_by_prov_id(
            item_id,
            provider_instance_id_or_domain,
        )
        if library_item and (time() - (library_item.metadata.last_refresh or 0)) > REFRESH_INTERVAL:
            # it's been too long since the full metadata was last retrieved (or never at all)
            # NOTE: do not attempt metadata refresh on unavailable items as it has side effects
            metadata_lookup = library_item.available

        if library_item and not (force_refresh or metadata_lookup or add_to_library):
            # we have a library item and no refreshing is needed, return the results!
            return library_item

        if force_refresh:
            # get (first) provider item id belonging to this library item
            add_to_library = True
            metadata_lookup = True
            if library_item:
                # resolve library item into a provider item to get the source details
                provider_instance_id_or_domain, item_id = await self.get_provider_mapping(
                    library_item
                )

        # grab full details from the provider
        details = await self.get_provider_item(
            item_id,
            provider_instance_id_or_domain,
            force_refresh=force_refresh,
            fallback=details,
        )
        if not details and library_item:
            # something went wrong while trying to fetch/refresh this item
            # return the existing (unavailable) library item and leave this for another day
            return library_item

        if not details:
            # we couldn't get a match from any of the providers, raise error
            msg = f"Item not found: {provider_instance_id_or_domain}/{item_id}"
            raise MediaNotFoundError(msg)

        if not (add_to_library or metadata_lookup):
            # return the provider item as-is
            return details

        # create task to add the item to the library,
        # including matching metadata etc. takes some time
        # in 99% of the cases we just return lazy because we want the details as fast as possible
        # only if we really need to wait for the result (e.g. to prevent race conditions),
        # we can set lazy to false and we await the job to complete.
        overwrite_existing = force_refresh and library_item is not None
        task_id = f"add_{self.media_type.value}.{details.provider}.{details.item_id}"
        add_task = self.mass.create_task(
            self.add_item_to_library,
            item=details,
            metadata_lookup=metadata_lookup,
            overwrite_existing=overwrite_existing,
            task_id=task_id,
        )
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
            return [item async for item in await self.iter_library_items(search=search_query)]
        prov = self.mass.get_provider(provider_instance_id_or_domain)
        if prov is None:
            return []
        if ProviderFeature.SEARCH not in prov.supported_features:
            return []
        if not prov.library_supported(self.media_type):
            # assume library supported also means that this mediatype is supported
            return []

        # prefer cache items (if any)
        cache_key = f"{prov.lookup_key}.search.{self.media_type.value}.{search_query}.{limit}"
        cache_key = cache_key.lower().replace("", "")
        if (cache := await self.mass.cache.get(cache_key)) is not None:
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
            if item.provider == "library":
                item = await self.get_library_item(item.item_id)
            return (item.provider, item.item_id)
        for prefer_unique in (True, False):
            for prov_mapping in item.provider_mappings:
                if not prov_mapping.available:
                    continue
                if provider := self.mass.get_provider(
                    prov_mapping.provider_instance
                    if prefer_unique
                    else prov_mapping.provider_domain
                ):
                    if prefer_unique and provider.is_streaming_provider:
                        continue
                    return (prov_mapping.provider_instance, prov_mapping.item_id)
        # last resort: return just the first entry
        for prov_mapping in item.provider_mappings:
            return (prov_mapping.provider_domain, prov_mapping.item_id)

        return (None, None)

    async def get_library_item(self, item_id: int | str) -> ItemCls:
        """Get single library item by id."""
        db_id = int(item_id)  # ensure integer
        extra_query = f"WHERE {self.db_table}.item_id = {item_id}"
        async for db_item in self.iter_library_items(extra_query=extra_query):
            return db_item
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
            provider_instance_id_or_domain=provider_instance_id_or_domain,
            provider_item_id=item_id,
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
                provider_instance=mapping.provider_instance,
                provider_item_id=mapping.item_id,
            ):
                return item
        # check by domain too
        for mapping in provider_mappings:
            for item in await self.get_library_items_by_prov_id(
                provider_domain=mapping.provider_domain,
                provider_item_id=mapping.item_id,
            ):
                return item
        return None

    async def get_library_item_by_external_id(
        self, external_id: str, external_id_type: ExternalID | None = None
    ) -> ItemCls | None:
        """Get the library item for the given external id."""
        query = f"WHERE {self.db_table}.external_ids LIKE :external_id_str"
        if external_id_type:
            external_id_str = f'%"{external_id_type}","{external_id}"%'
        else:
            external_id_str = f'%"{external_id}"%'
        for item in await self._get_library_items_by_query(
            extra_query=query, extra_query_params={"external_id_str": external_id_str}
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
        provider_domain: str | None = None,
        provider_instance: str | None = None,
        provider_instance_id_or_domain: str | None = None,
        provider_item_id: str | None = None,
        limit: int = 500,
        offset: int = 0,
    ) -> list[ItemCls]:
        """Fetch all records from library for given provider."""
        assert provider_instance_id_or_domain != "library"
        assert provider_domain != "library"
        assert provider_instance != "library"
        if provider_instance:
            query_params = {"prov_id": provider_instance}
            query = "provider_mappings.provider_instance = :prov_id"
        elif provider_domain:
            query_params = {"prov_id": provider_domain}
            query = "provider_mappings.provider_domain = :prov_id"
        else:
            query_params = {"prov_id": provider_instance_id_or_domain}
            query = (
                "(provider_mappings.provider_instance = :prov_id "
                "OR provider_mappings.provider_domain = :prov_id)"
            )
        if provider_item_id:
            query += " AND provider_mappings.provider_item_id = :item_id"
            query_params["item_id"] = provider_item_id
        return await self._get_library_items_by_query(
            limit=limit, offset=offset, extra_query=query, extra_query_params=query_params
        )

    async def iter_library_items_by_prov_id(
        self,
        provider_instance_id_or_domain: str,
        provider_item_id: str | None = None,
    ) -> AsyncGenerator[ItemCls, None]:
        """Iterate all records from database for given provider."""
        limit: int = 500
        offset: int = 0
        while True:
            next_items = await self.get_library_items_by_prov_id(
                provider_instance_id_or_domain=provider_instance_id_or_domain,
                provider_item_id=provider_item_id,
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
        if provider_instance_id_or_domain == "library":
            return await self.get_library_item(item_id)
        if not (provider := self.mass.get_provider(provider_instance_id_or_domain)):
            raise ProviderUnavailableError(f"{provider_instance_id_or_domain} is not available")
        cache_key = f"provider_item.{self.media_type.value}.{provider.lookup_key}.{item_id}"
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
        library_item.provider_mappings.add(provider_mapping)
        await self._set_provider_mappings(db_id, library_item.provider_mappings)

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
        library_item.provider_mappings = {
            x
            for x in library_item.provider_mappings
            if x.provider_instance != provider_instance_id and x.item_id != provider_item_id
        }
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
        # cleanup playlog table
        await self.mass.music.database.delete(
            DB_TABLE_PLAYLOG,
            {
                "media_type": self.media_type.value,
                "item_id": provider_item_id,
                "provider": provider_instance_id,
            },
        )
        if library_item.provider_mappings:
            # we (temporary?) duplicate the provider mappings in a separate column of the media
            # item's table, because the json_group_array query is superslow
            await self.mass.music.database.update(
                self.db_table,
                {"item_id": db_id},
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
            library_item = None
        # update provider_mappings table
        await self.mass.music.database.delete(
            DB_TABLE_PROVIDER_MAPPINGS,
            {
                "media_type": self.media_type.value,
                "item_id": db_id,
                "provider_instance": provider_instance_id,
            },
        )
        if library_item is None:
            return
        # update the item's provider mappings (and check if we still have any)
        library_item.provider_mappings = {
            x for x in library_item.provider_mappings if x.provider_instance != provider_instance_id
        }
        if library_item.provider_mappings:
            # we (temporary?) duplicate the provider mappings in a separate column of the media
            # item's table, because the json_group_array query is superslow
            await self.mass.music.database.update(
                self.db_table,
                {"item_id": db_id},
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
    async def _add_library_item(
        self,
        item: ItemCls,
        metadata_lookup: bool = True,
        overwrite_existing: bool = False,
    ) -> int:
        """Add artist to library and return the database id."""

    @abstractmethod
    async def _update_library_item(
        self, item_id: str | int, update: ItemCls, overwrite: bool = False
    ) -> None:
        """Update existing library record in the database."""

    async def _match(self, db_item: ItemCls) -> None:
        """
        Try to find match on all (streaming) providers for the provided (database) item.

        This is used to link objects of different providers/qualities together.
        """

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
        favorite: bool | None = None,
        search: str | None = None,
        limit: int = 500,
        offset: int = 0,
        order_by: str | None = None,
        provider: str | None = None,
        extra_query: str | None = None,
        extra_query_params: dict[str, Any] | None = None,
    ) -> list[ItemCls] | int:
        """Fetch MediaItem records from database given a custom (WHERE) clause."""
        sql_query = self.base_query
        query_params = extra_query_params or {}
        query_parts: list[str] = []
        # handle basic search on name
        if search:
            # handle combined artist + title search
            if self.media_type in (MediaType.ALBUM, MediaType.TRACK) and " - " in search:
                artist_str, title_str = search.split(" - ", 1)
                query_parts.append(
                    f"({self.db_table}.name LIKE :search_title "
                    f"AND {DB_TABLE_ARTISTS}.name LIKE :search_artist)"
                )
                query_params["search_title"] = f"%{title_str}%"
                query_params["search_artist"] = f"%{artist_str}%"
            else:
                query_params["search"] = f"%{search}%"
                query_parts.append(f"{self.db_table}.name LIKE :search")
        # handle favorite filter
        if favorite is not None:
            query_parts.append(f"{self.db_table}.favorite = :favorite")
            query_params["favorite"] = favorite
        # handle provider filter
        if provider:
            query_parts.append(f"{DB_TABLE_PROVIDER_MAPPINGS}.provider_instance = :provider")
            query_params["provider"] = provider
        # handle extra/custom query
        if extra_query:
            # prevent duplicate where statement
            if extra_query.lower().startswith("where "):
                extra_query = extra_query[5:]
            query_parts.append(extra_query)
        # concetenate all where queries
        if query_parts:
            sql_query += " WHERE " + " AND ".join(query_parts)
        # build final query
        if order_by:
            if sort_key := SORT_KEYS.get(order_by):
                sql_query += f" ORDER BY {sort_key}"
        # return dbresult parsed to media item model
        return [
            self.item_cls.from_dict(self._parse_db_row(db_row))
            for db_row in await self.mass.music.database.get_rows_from_query(
                sql_query, query_params, limit=limit, offset=offset
            )
        ]

    async def _set_provider_mappings(
        self,
        item_id: str | int,
        provider_mappings: Iterable[ProviderMapping],
        overwrite: bool = False,
    ) -> None:
        """Update the provider_items table for the media item."""
        db_id = int(item_id)  # ensure integer
        if overwrite:
            # on overwrite, clear the provider_mappings table first
            # this is done for filesystem provider changing the path (and thus item_id)
            for provider_mapping in provider_mappings:
                await self.mass.music.database.delete(
                    DB_TABLE_PROVIDER_MAPPINGS,
                    {
                        "media_type": self.media_type.value,
                        "item_id": db_id,
                        "provider_instance": provider_mapping.provider_instance,
                    },
                )
        for provider_mapping in provider_mappings:
            await self.mass.music.database.insert_or_replace(
                DB_TABLE_PROVIDER_MAPPINGS,
                {
                    "media_type": self.media_type.value,
                    "item_id": db_id,
                    "provider_domain": provider_mapping.provider_domain,
                    "provider_instance": provider_mapping.provider_instance,
                    "provider_item_id": provider_mapping.item_id,
                    "available": provider_mapping.available,
                    "url": provider_mapping.url,
                    "audio_format": serialize_to_json(provider_mapping.audio_format),
                    "details": provider_mapping.details,
                },
            )
        # we (temporary?) duplicate the provider mappings in a separate column of the media
        # item's table, because the json_group_array query is superslow
        await self.mass.music.database.update(
            self.db_table,
            {"item_id": db_id},
            {"provider_mappings": serialize_to_json(provider_mappings)},
        )

    @staticmethod
    def _parse_db_row(db_row: Mapping) -> dict[str, Any]:
        """Parse raw db Mapping into a dict."""
        db_row_dict = dict(db_row)
        db_row_dict["provider"] = "library"
        db_row_dict["favorite"] = bool(db_row_dict["favorite"])
        db_row_dict["item_id"] = str(db_row_dict["item_id"])

        for key in JSON_KEYS:
            if key not in db_row_dict:
                continue
            if not (raw_value := db_row_dict[key]):
                continue
            db_row_dict[key] = json_loads(raw_value)

        # copy album image to itemmapping single image
        if (album := db_row_dict.get("album")) and (images := album.get("images")):
            db_row_dict["album"]["image"] = next((x for x in images if x["type"] == "thumb"), None)
        return db_row_dict
