"""Base (ABC) MediaType specific controller."""

from __future__ import annotations

import asyncio
import logging
from abc import ABCMeta, abstractmethod
from collections.abc import Iterable
from contextlib import suppress
from datetime import datetime
from typing import TYPE_CHECKING, Any, TypeVar, cast, final

from music_assistant_models.enums import EventType, ExternalID, MediaType, ProviderFeature
from music_assistant_models.errors import (
    InsufficientPermissions,
    MediaNotFoundError,
    ProviderUnavailableError,
)
from music_assistant_models.media_items import ItemMapping, MediaItemType, ProviderMapping, Track

from music_assistant.constants import DB_TABLE_PLAYLOG, DB_TABLE_PROVIDER_MAPPINGS, MASS_LOGGER_NAME
from music_assistant.controllers.webserver.helpers.auth_middleware import get_current_user
from music_assistant.helpers.compare import compare_media_item, create_safe_string
from music_assistant.helpers.json import json_loads, serialize_to_json
from music_assistant.helpers.util import guard_single_request

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Mapping

    from music_assistant import MusicAssistant
    from music_assistant.models.music_provider import MusicProvider


ItemCls = TypeVar("ItemCls", bound="MediaItemType")


JSON_KEYS = (
    "artists",
    "track_album",
    "metadata",
    "provider_mappings",
    "external_ids",
    "narrators",
    "authors",
)

SORT_KEYS = {
    # sqlite has no builtin support for natural sorting
    # so we have use an additional column for this
    # this also improves searching and sorting performance
    "name": "search_name ASC",
    "name_desc": "search_name DESC",
    "duration": "duration ASC",
    "duration_desc": "duration DESC",
    "sort_name": "search_sort_name ASC",
    "sort_name_desc": "search_sort_name DESC",
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
    "artist_name": "artists.search_name ASC",
    "artist_name_desc": "artists.search_name DESC",
    "random": "RANDOM()",
    "random_play_count": "RANDOM(), play_count ASC",
}


class MediaControllerBase[ItemCls: "MediaItemType"](metaclass=ABCMeta):
    """Base model for controller managing a MediaType."""

    media_type: MediaType
    item_cls: type[MediaItemType]
    db_table: str

    def __init__(self, mass: MusicAssistant) -> None:
        """Initialize class."""
        self.mass = mass
        self.base_query = f"""
        SELECT
            {self.db_table}.*,
            (SELECT JSON_GROUP_ARRAY(
                json_object(
                'item_id', provider_mappings.provider_item_id,
                    'provider_domain', provider_mappings.provider_domain,
                        'provider_instance', provider_mappings.provider_instance,
                        'available', provider_mappings.available,
                        'audio_format', json(provider_mappings.audio_format),
                        'url', provider_mappings.url,
                        'details', provider_mappings.details,
                        'in_library', provider_mappings.in_library,
                        'is_unique', provider_mappings.is_unique
                )) FROM provider_mappings WHERE provider_mappings.item_id = {self.db_table}.item_id
                    AND provider_mappings.media_type = '{self.media_type.value}') AS provider_mappings
            FROM {self.db_table} """  # noqa: E501
        self.logger = logging.getLogger(f"{MASS_LOGGER_NAME}.music.{self.media_type.value}")
        # register (base) api handlers
        self.api_base = api_base = f"{self.media_type}s"
        self.mass.register_api_command(f"music/{api_base}/count", self.library_count)
        self.mass.register_api_command(f"music/{api_base}/library_items", self.library_items)
        self.mass.register_api_command(f"music/{api_base}/get", self.get)
        # Backward compatibility alias - prefer the generic "get" endpoint
        self.mass.register_api_command(
            f"music/{api_base}/get_{self.media_type}", self.get, alias=True
        )
        self.mass.register_api_command(
            f"music/{api_base}/update", self.update_item_in_library, required_role="admin"
        )
        self.mass.register_api_command(
            f"music/{api_base}/remove", self.remove_item_from_library, required_role="admin"
        )
        self._db_add_lock = asyncio.Lock()

    @final
    async def add_item_to_library(
        self,
        item: ItemCls,
        overwrite_existing: bool = False,
    ) -> ItemCls:
        """Add item to library and return the new (or updated) database item."""
        new_item = False
        # check for existing item first
        if library_id := await self._get_library_item_by_match(item):
            # update existing item
            await self._update_library_item(library_id, item, overwrite=overwrite_existing)
        else:
            # actually add a new item in the library db
            self.mass.music.match_provider_instances(item)
            async with self._db_add_lock:
                library_id = await self._add_library_item(item)
                new_item = True
        # return final library_item
        library_item = await self.get_library_item(library_id)
        self.mass.signal_event(
            EventType.MEDIA_ITEM_ADDED if new_item else EventType.MEDIA_ITEM_UPDATED,
            library_item.uri,
            library_item,
        )
        return library_item

    @final
    async def _get_library_item_by_match(self, item: ItemCls | ItemMapping) -> int | None:
        if item.provider == "library":
            return int(item.item_id)
        # search by provider mappings if item is ItemMapping
        if isinstance(item, ItemMapping):
            if cur_item := await self.get_library_item_by_prov_id(item.item_id, item.provider):
                return int(cur_item.item_id)

        # for all other items that are MediaItemType, check provider_mappings if it exists
        provider_mappings = getattr(item, "provider_mappings", None)
        if provider_mappings:
            if cur_item := await self.get_library_item_by_prov_mappings(provider_mappings):
                return int(cur_item.item_id)
        if cur_item := await self.get_library_item_by_external_ids(item.external_ids):
            # existing item match by external id
            # Double check external IDs - if MBID exists, regards that as overriding
            if compare_media_item(item, cur_item):
                return int(cur_item.item_id)
        # search by (exact) name match
        query = f"{self.db_table}.name = :name OR {self.db_table}.sort_name = :sort_name"
        query_params = {"name": item.name, "sort_name": item.sort_name}
        for db_item in await self.get_library_items_by_query(
            extra_query_parts=[query], extra_query_params=query_params
        ):
            if compare_media_item(db_item, item, True):
                return int(db_item.item_id)
        return None

    @final
    async def update_item_in_library(
        self, item_id: str | int, update: ItemCls, overwrite: bool = False
    ) -> ItemCls:
        """Update existing library record in the library database."""
        self.mass.music.match_provider_instances(update)
        await self._update_library_item(item_id, update, overwrite=overwrite)
        # return the updated object
        library_item = await self.get_library_item(item_id)
        self.mass.signal_event(
            EventType.MEDIA_ITEM_UPDATED,
            library_item.uri,
            library_item,
        )
        return library_item

    async def remove_item_from_library(self, item_id: str | int, recursive: bool = True) -> None:
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
        provider: str | list[str] | None = None,
    ) -> list[ItemCls]:
        """
        Get the library items for this mediatype.

        :param favorite: Filter by favorite status.
        :param search: Filter by search query.
        :param limit: Maximum number of items to return.
        :param offset: Number of items to skip.
        :param order_by: Order by field (e.g. 'sort_name', 'timestamp_added').
        :param provider: Filter by provider instance ID (single string or list).
        """
        return await self.get_library_items_by_query(
            favorite=favorite,
            search=search,
            limit=limit,
            offset=offset,
            order_by=order_by,
            provider_filter=self._ensure_provider_filter(provider),
        )

    async def iter_library_items(
        self,
        favorite: bool | None = None,
        search: str | None = None,
        order_by: str = "sort_name",
        provider: str | list[str] | None = None,
    ) -> AsyncGenerator[ItemCls, None]:
        """Iterate all in-database items."""
        limit: int = 500
        offset: int = 0
        if provider is not None:
            provider_filter = provider if isinstance(provider, list) else [provider]
        else:
            provider_filter = None
        while True:
            next_items = await self.get_library_items_by_query(
                favorite=favorite,
                search=search,
                limit=limit,
                offset=offset,
                order_by=order_by,
                provider_filter=provider_filter,
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
    ) -> ItemCls:
        """Return (full) details for a single media item."""
        # always prefer the full library item if we have it
        if library_item := await self.get_library_item_by_prov_id(
            item_id,
            provider_instance_id_or_domain,
        ):
            # schedule a refresh of the metadata on access of the item
            # e.g. the item is being played or opened in the UI
            assert library_item.uri is not None
            self.mass.metadata.schedule_update_metadata(library_item.uri)
            return library_item
        # grab full details from the provider
        return await self.get_provider_item(
            item_id,
            provider_instance_id_or_domain,
        )

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
            return await self.library_items(search=search_query, limit=limit)
        if not (prov := self.mass.get_provider(provider_instance_id_or_domain)):
            return []
        prov = cast("MusicProvider", prov)
        if ProviderFeature.SEARCH not in prov.supported_features:
            return []
        if not prov.library_supported(self.media_type):
            # assume library supported also means that this mediatype is supported
            return []
        searchresult = await prov.search(
            search_query,
            [self.media_type],
            limit,
        )
        match self.media_type:
            case MediaType.ARTIST:
                return cast("list[ItemCls]", searchresult.artists)
            case MediaType.ALBUM:
                return cast("list[ItemCls]", searchresult.albums)
            case MediaType.TRACK:
                return cast("list[ItemCls]", searchresult.tracks)
            case MediaType.PLAYLIST:
                return cast("list[ItemCls]", searchresult.playlists)
            case MediaType.AUDIOBOOK:
                return cast("list[ItemCls]", searchresult.audiobooks)
            case MediaType.PODCAST:
                return cast("list[ItemCls]", searchresult.podcasts)
            case MediaType.RADIO:
                return cast("list[ItemCls]", searchresult.radio)
            case _:
                return []

    async def get_library_item(self, item_id: int | str) -> ItemCls:
        """Get single library item by id."""
        db_id = int(item_id)  # ensure integer
        extra_query = f"WHERE {self.db_table}.item_id = :item_id"
        for db_item in await self.get_library_items_by_query(
            extra_query_parts=[extra_query],
            extra_query_params={"item_id": db_id},
        ):
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

    @final
    async def get_library_item_by_prov_mappings(
        self,
        provider_mappings: Iterable[ProviderMapping],
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

    @final
    async def get_library_item_by_external_id(
        self, external_id: str, external_id_type: ExternalID | None = None
    ) -> ItemCls | None:
        """Get the library item for the given external id."""
        query = f"{self.db_table}.external_ids LIKE :external_id_str"
        if external_id_type:
            external_id_str = f'%"{external_id_type}","{external_id}"%'
        else:
            external_id_str = f'%"{external_id}"%'
        for item in await self.get_library_items_by_query(
            extra_query_parts=[query],
            extra_query_params={"external_id_str": external_id_str},
        ):
            return item
        return None

    @final
    async def get_library_item_by_external_ids(
        self, external_ids: set[tuple[ExternalID, str]]
    ) -> ItemCls | None:
        """Get the library item for (one of) the given external ids."""
        for external_id_type, external_id in external_ids:
            if match := await self.get_library_item_by_external_id(external_id, external_id_type):
                return match
        return None

    @final
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
        subquery_parts: list[str] = []
        query_params: dict[str, Any] = {}
        if provider_instance:
            query_params = {"prov_id": provider_instance}
            subquery_parts.append("provider_mappings.provider_instance = :prov_id")
        elif provider_domain:
            query_params = {"prov_id": provider_domain}
            subquery_parts.append("provider_mappings.provider_domain = :prov_id")
        else:
            query_params = {"prov_id": provider_instance_id_or_domain}
            subquery_parts.append(
                "(provider_mappings.provider_instance = :prov_id "
                "OR provider_mappings.provider_domain = :prov_id)"
            )
        if provider_item_id:
            subquery_parts.append("provider_mappings.provider_item_id = :item_id")
            query_params["item_id"] = provider_item_id
        subquery = f"SELECT item_id FROM provider_mappings WHERE {' AND '.join(subquery_parts)}"
        query = f"WHERE {self.db_table}.item_id IN ({subquery})"
        return await self.get_library_items_by_query(
            limit=limit,
            offset=offset,
            extra_query_parts=[query],
            extra_query_params=query_params,
        )

    @final
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

    @final
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

    @guard_single_request  # type: ignore[type-var]  # TODO: fix typing for MediaControllerBase
    @final
    async def get_provider_item(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
        force_refresh: bool = False,
        fallback: ItemMapping | ItemCls | None = None,
    ) -> ItemCls:
        """Return item details for the given provider item id."""
        if provider_instance_id_or_domain == "library":
            return await self.get_library_item(item_id)
        if not (provider := self.mass.get_provider(provider_instance_id_or_domain)):
            raise ProviderUnavailableError(f"{provider_instance_id_or_domain} is not available")
        if provider := self.mass.get_provider(provider_instance_id_or_domain):
            provider = cast("MusicProvider", provider)
            with suppress(MediaNotFoundError):
                async with self.mass.cache.handle_refresh(force_refresh):
                    return cast("ItemCls", await provider.get_item(self.media_type, item_id))
        # if we reach this point all possibilities failed and the item could not be found.
        # There is a possibility that the (streaming) provider changed the id of the item
        # so we return the previous details (if we have any) marked as unavailable, so
        # at least we have the possibility to sort out the new id through matching logic.
        fallback = fallback or await self.get_library_item_by_prov_id(
            item_id, provider_instance_id_or_domain
        )
        if (
            fallback
            and isinstance(fallback, ItemMapping)
            and (fallback_provider := self.mass.get_provider(fallback.provider))
        ):
            # fallback is a ItemMapping, try to convert to full item
            with suppress(LookupError, TypeError, ValueError):
                return cast(
                    "ItemCls",
                    self.item_cls.from_dict(
                        {
                            **fallback.to_dict(),
                            "provider_mappings": [
                                {
                                    "item_id": fallback.item_id,
                                    "provider_domain": fallback_provider.domain,
                                    "provider_instance": fallback_provider.instance_id,
                                    "available": fallback.available,
                                }
                            ],
                        }
                    ),
                )
        if fallback:
            # simply return the fallback item
            return cast("ItemCls", fallback)
        # all options exhausted, we really can not find this item
        msg = (
            f"{self.media_type.value}://{item_id} not "
            f"found on provider {provider_instance_id_or_domain}"
        )
        raise MediaNotFoundError(msg)

    @final
    async def add_provider_mapping(
        self, item_id: str | int, provider_mapping: ProviderMapping
    ) -> None:
        """Add provider mapping to existing library item."""
        await self.add_provider_mappings(item_id, [provider_mapping])

    @final
    async def add_provider_mappings(
        self, item_id: str | int, provider_mappings: Iterable[ProviderMapping]
    ) -> None:
        """
        Add provider mappings to existing library item.

        :param item_id: The library item ID to add mappings to.
        :param provider_mappings: The provider mappings to add.
        """
        db_id = int(item_id)  # ensure integer
        library_item = await self.get_library_item(db_id)
        new_mappings: set[ProviderMapping] = set()
        for provider_mapping in provider_mappings:
            # ignore if the mapping is already present
            if provider_mapping not in library_item.provider_mappings:
                new_mappings.add(provider_mapping)
        if not new_mappings:
            return
        # handle special case where the user wants to merge 2 library items
        for mapping in new_mappings:
            if _library_item := await self.get_library_item_by_prov_id(
                mapping.item_id, mapping.provider_instance
            ):
                if _library_item.item_id != library_item.item_id:
                    # merging items
                    self.logger.debug(
                        "merging item id %s into item id %s based on provider mapping %s/%s",
                        _library_item.item_id,
                        library_item.item_id,
                        mapping.provider_instance,
                        mapping.item_id,
                    )
                    await self.remove_item_from_library(_library_item.item_id, recursive=True)
                    break
        library_item.provider_mappings.update(new_mappings)
        self.mass.music.match_provider_instances(library_item)
        await self.set_provider_mappings(db_id, library_item.provider_mappings)
        self.mass.signal_event(EventType.MEDIA_ITEM_UPDATED, library_item.uri, library_item)

    @final
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
        # cleanup playlog table
        await self.mass.music.database.delete(
            DB_TABLE_PLAYLOG,
            {
                "media_type": self.media_type.value,
                "item_id": provider_item_id,
                "provider": provider_instance_id,
            },
        )
        library_item.provider_mappings = {
            x
            for x in library_item.provider_mappings
            if not (x.provider_instance == provider_instance_id and x.item_id == provider_item_id)
        }
        if library_item.provider_mappings:
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

    @final
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

    @final
    async def set_provider_mappings(
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
            await self.mass.music.database.delete(
                DB_TABLE_PROVIDER_MAPPINGS,
                {"media_type": self.media_type.value, "item_id": db_id},
            )
        for provider_mapping in provider_mappings:
            prov_map_obj = {
                "media_type": self.media_type.value,
                "item_id": db_id,
                "provider_domain": provider_mapping.provider_domain,
                "provider_instance": provider_mapping.provider_instance,
                "provider_item_id": provider_mapping.item_id,
                "available": provider_mapping.available,
                "audio_format": serialize_to_json(provider_mapping.audio_format),
            }
            for key in ("url", "details", "in_library", "is_unique"):
                if (value := getattr(provider_mapping, key, None)) is not None:
                    prov_map_obj[key] = value
            await self.mass.music.database.upsert(
                DB_TABLE_PROVIDER_MAPPINGS,
                prov_map_obj,
            )

    @abstractmethod
    async def _add_library_item(
        self,
        item: ItemCls,
        overwrite_existing: bool = False,
    ) -> int:
        """Add artist to library and return the database id."""

    @abstractmethod
    async def _update_library_item(
        self, item_id: str | int, update: ItemCls, overwrite: bool = False
    ) -> None:
        """Update existing library record in the database."""

    @abstractmethod
    async def match_providers(self, db_item: ItemCls) -> None:
        """
        Try to find match on all (streaming) providers for the provided (database) item.

        This is used to link objects of different providers/qualities together.
        """

    @abstractmethod
    async def radio_mode_base_tracks(
        self,
        item: ItemCls,
        preferred_provider_instances: list[str] | None = None,
    ) -> list[Track]:
        """
        Get the list of base tracks from the controller used to calculate the dynamic radio.

        :param item: The MediaItem to get base tracks for.
        :param preferred_provider_instances: List of preferred provider instance IDs to use.
            When provided, these providers will be tried first before falling back to others.
        """

    @final
    async def get_library_items_by_query(
        self,
        favorite: bool | None = None,
        search: str | None = None,
        limit: int = 500,
        offset: int = 0,
        order_by: str | None = None,
        provider_filter: list[str] | None = None,
        extra_query_parts: list[str] | None = None,
        extra_query_params: dict[str, Any] | None = None,
        extra_join_parts: list[str] | None = None,
    ) -> list[ItemCls]:
        """Fetch MediaItem records from database by building the query."""
        query_params = extra_query_params or {}
        query_parts: list[str] = extra_query_parts or []
        join_parts: list[str] = extra_join_parts or []
        search = self._preprocess_search(search, query_params)
        # create special performant random query
        if order_by and order_by.startswith("random"):
            self._apply_random_subquery(
                query_parts=query_parts,
                query_params=query_params,
                join_parts=join_parts,
                favorite=favorite,
                search=search,
                provider_filter=provider_filter,
                limit=limit,
            )
        else:
            # apply filters
            self._apply_filters(
                query_parts=query_parts,
                query_params=query_params,
                join_parts=join_parts,
                favorite=favorite,
                search=search,
                provider_filter=provider_filter,
            )
        # build and execute final query
        sql_query = self._build_final_query(query_parts, join_parts, order_by)

        return [
            cast("ItemCls", self.item_cls.from_dict(self._parse_db_row(db_row)))
            for db_row in await self.mass.music.database.get_rows_from_query(
                sql_query, query_params, limit=limit, offset=offset
            )
        ]

    @final
    def _preprocess_search(self, search: str | None, query_params: dict[str, Any]) -> str | None:
        """Preprocess search string and add to query params."""
        if search:
            search = create_safe_string(search, True, True)
            query_params["search"] = f"%{search}%"
        return search

    @final
    @staticmethod
    def _clean_query_parts(query_parts: list[str]) -> list[str]:
        """Clean the query parts list by removing duplicate where statements."""
        return [x[5:] if x.lower().startswith("where ") else x for x in query_parts]

    @final
    def _apply_random_subquery(
        self,
        query_parts: list[str],
        query_params: dict[str, Any],
        join_parts: list[str],
        favorite: bool | None,
        search: str | None,
        provider_filter: list[str] | None,
        limit: int,
    ) -> None:
        """Build a fast random subquery with all filters applied."""
        sub_query_parts = query_parts.copy()
        sub_join_parts = join_parts.copy()

        # Apply all filters to the subquery
        self._apply_filters(
            query_parts=sub_query_parts,
            query_params=query_params,
            join_parts=sub_join_parts,
            favorite=favorite,
            search=search,
            provider_filter=provider_filter,
        )

        # Build the subquery
        sub_query = f"SELECT {self.db_table}.item_id FROM {self.db_table}"

        if sub_join_parts:
            sub_query += f" {' '.join(sub_join_parts)}"

        if sub_query_parts:
            sub_query += " WHERE " + " AND ".join(self._clean_query_parts(sub_query_parts))

        sub_query += f" ORDER BY RANDOM() LIMIT {limit}"

        # The query now only consists of the random subquery, which applies all filters
        # within itself
        query_parts.clear()
        query_parts.append(f"{self.db_table}.item_id in ({sub_query})")
        join_parts.clear()

    @final
    def _apply_filters(
        self,
        query_parts: list[str],
        query_params: dict[str, Any],
        join_parts: list[str],
        favorite: bool | None,
        search: str | None,
        provider_filter: list[str] | None,
    ) -> None:
        """Apply search, favorite, and provider filters."""
        # handle search
        if search:
            query_parts.append(f"{self.db_table}.search_name LIKE :search")
        # handle favorite filter
        if favorite is not None:
            query_parts.append(f"{self.db_table}.favorite = :favorite")
            query_params["favorite"] = favorite
        # Apply the provider filter
        if provider_filter:
            provider_conditions = []
            for idx, prov in enumerate(provider_filter):
                param_name = f"provider_filter_{idx}"
                provider_conditions.append(f"provider_mappings.provider_instance = :{param_name}")
                query_params[param_name] = prov
            query_params["provider_media_type"] = self.media_type.value
            join_parts.append(
                f"JOIN provider_mappings ON provider_mappings.item_id = {self.db_table}.item_id "
                "AND provider_mappings.media_type = :provider_media_type "
                "AND provider_mappings.in_library = 1 "
                f"AND ({' OR '.join(provider_conditions)})"
            )

    @final
    def _build_final_query(
        self,
        query_parts: list[str],
        join_parts: list[str],
        order_by: str | None,
    ) -> str:
        """Build the final SQL query string."""
        sql_query = self.base_query

        # Add joins
        if join_parts:
            sql_query += f" {' '.join(join_parts)} "

        # Add where clauses
        if query_parts:
            # prevent duplicate where statement
            sql_query += " WHERE " + " AND ".join(self._clean_query_parts(query_parts))

        # Add grouping and ordering
        sql_query += f" GROUP BY {self.db_table}.item_id"

        if order_by:
            if sort_key := SORT_KEYS.get(order_by):
                sql_query += f" ORDER BY {sort_key}"

        return sql_query

    @final
    @staticmethod
    def _parse_db_row(db_row: Mapping[str, Any]) -> dict[str, Any]:
        """Parse raw db Mapping into a dict."""
        db_row_dict = dict(db_row)
        db_row_dict["provider"] = "library"
        db_row_dict["favorite"] = bool(db_row_dict["favorite"])
        db_row_dict["item_id"] = str(db_row_dict["item_id"])
        db_row_dict["date_added"] = datetime.fromtimestamp(
            db_row_dict["timestamp_added"]
        ).isoformat()

        for key in JSON_KEYS:
            if key not in db_row_dict:
                continue
            if not (raw_value := db_row_dict[key]):
                continue
            db_row_dict[key] = json_loads(raw_value)

        # copy track_album --> album
        if track_album := db_row_dict.get("track_album"):
            db_row_dict["album"] = track_album
            db_row_dict["disc_number"] = track_album["disc_number"]
            db_row_dict["track_number"] = track_album["track_number"]
            # always prefer album image over track image
            if (album_images := track_album.get("images")) and (
                album_thumb := next((x for x in album_images if x["type"] == "thumb"), None)
            ):
                # copy album image to itemmapping single image
                db_row_dict["image"] = album_thumb
                if db_row_dict["metadata"].get("images"):
                    # merge album image with existing images
                    db_row_dict["metadata"]["images"] = [
                        album_thumb,
                        *db_row_dict["metadata"]["images"],
                    ]
                else:
                    db_row_dict["metadata"]["images"] = [album_thumb]
        return db_row_dict

    @final
    def _ensure_provider_filter(
        self,
        provider: str | list[str] | None,
    ) -> list[str] | None:
        """Ensure the provider filter respects the current user's provider filter."""
        # Apply user provider filter if needed
        user = get_current_user()
        user_provider_filter = user.provider_filter if user and user.provider_filter else None
        final_provider_filter: list[str] | None = None
        if user_provider_filter:
            # User has a provider filter set
            if provider:
                # Explicit provider filter provided - validate against user's allowed providers
                requested_providers = [provider] if isinstance(provider, str) else provider
                # Only include providers that are in both the user's filter and the requested list
                final_provider_filter = [
                    p for p in requested_providers if p in user_provider_filter
                ]
                if not final_provider_filter:
                    # No overlap - user requested providers they don't have access to
                    raise InsufficientPermissions(
                        "User does not have permission to access the requested provider(s)."
                    )
            else:
                # No explicit filter - use user's provider filter
                final_provider_filter = user_provider_filter
        elif provider is not None:
            # No user filter - use the provided filter as is
            final_provider_filter = [provider] if isinstance(provider, str) else provider
        return final_provider_filter

    @final
    def _select_provider_id(self, library_item: ItemCls) -> tuple[str, str]:
        """Select the correct provider id to use for fetching the item."""
        user = get_current_user()
        user_provider_filter = user.provider_filter if user and user.provider_filter else None
        # prefer user provider filter if available
        for mapping in library_item.provider_mappings:
            if user_provider_filter and mapping.provider_instance not in user_provider_filter:
                continue
            return (mapping.provider_instance, mapping.item_id)
        # fallback to first mapping
        mapping = next(iter(library_item.provider_mappings))
        return (mapping.provider_instance, mapping.item_id)
