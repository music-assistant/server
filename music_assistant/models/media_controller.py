"""Model for a base media_controller."""
from __future__ import annotations

import asyncio
from abc import ABCMeta, abstractmethod
from time import time
from typing import (
    TYPE_CHECKING,
    AsyncGenerator,
    Generic,
    List,
    Optional,
    Tuple,
    TypeVar,
    Union,
)

from music_assistant.models.errors import MediaNotFoundError
from music_assistant.models.event import MassEvent

from .enums import EventType, MediaType, MusicProviderFeature, ProviderType
from .media_items import MediaItemType, PagedItems, media_from_dict

if TYPE_CHECKING:
    from music_assistant.mass import MusicAssistant

ItemCls = TypeVar("ItemCls", bound="MediaControllerBase")

REFRESH_INTERVAL = 60 * 60 * 24 * 30


class MediaControllerBase(Generic[ItemCls], metaclass=ABCMeta):
    """Base model for controller managing a MediaType."""

    media_type: MediaType
    item_cls: MediaItemType
    db_table: str

    def __init__(self, mass: MusicAssistant):
        """Initialize class."""
        self.mass = mass
        self.logger = mass.logger.getChild(f"music.{self.media_type.value}")
        self._db_add_lock = asyncio.Lock()

    @abstractmethod
    async def add(self, item: ItemCls, overwrite_existing: bool = False) -> ItemCls:
        """Add item to local db and return the database item."""
        raise NotImplementedError

    @abstractmethod
    async def add_db_item(
        self, item: ItemCls, overwrite_existing: bool = False
    ) -> ItemCls:
        """Add a new record for this mediatype to the database."""
        raise NotImplementedError

    @abstractmethod
    async def update_db_item(
        self,
        item_id: int,
        item: ItemCls,
        overwrite: bool = False,
    ) -> ItemCls:
        """Update record in the database, merging data."""
        raise NotImplementedError

    async def db_items(
        self,
        in_library: Optional[bool] = None,
        search: Optional[str] = None,
        limit: int = 500,
        offset: int = 0,
        order_by: str = "sort_name",
    ) -> PagedItems:
        """Get in-database items."""
        sql_query = f"SELECT * FROM {self.db_table}"
        params = {}
        query_parts = []
        if search:
            params["search"] = f"%{search}%"
            query_parts.append("name LIKE :search")
        if in_library is not None:
            query_parts.append("in_library = :in_library")
            params["in_library"] = in_library
        if query_parts:
            sql_query += " WHERE " + " AND ".join(query_parts)
        sql_query += f" ORDER BY {order_by}"
        items = await self.get_db_items_by_query(
            sql_query, params, limit=limit, offset=offset
        )
        count = len(items)
        if count < limit:
            total = offset + count
        else:
            total = await self.mass.database.get_count_from_query(sql_query, params)
        return PagedItems(items, count, limit, offset, total)

    async def iter_db_items(
        self,
        in_library: Optional[bool] = None,
        search: Optional[str] = None,
        order_by: str = "sort_name",
    ) -> AsyncGenerator[ItemCls, None]:
        """Iterate all in-database items."""
        limit: int = 500
        offset: int = 0
        while True:
            next_items = await self.db_items(
                in_library=in_library,
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
        provider_item_id: str,
        provider: Optional[ProviderType] = None,
        provider_id: Optional[str] = None,
        force_refresh: bool = False,
        lazy: bool = True,
        details: ItemCls = None,
        overwrite_existing: bool = None,
    ) -> ItemCls:
        """Return (full) details for a single media item."""
        assert provider or provider_id, "provider or provider_id must be supplied"
        if isinstance(provider, str):
            provider = ProviderType(provider)
        db_item = await self.get_db_item_by_prov_id(
            provider_item_id=provider_item_id,
            provider=provider,
            provider_id=provider_id,
        )
        if overwrite_existing is None:
            overwrite_existing = force_refresh
        if db_item and (time() - db_item.last_refresh) > REFRESH_INTERVAL:
            # it's been too long since the full metadata was last retrieved (or never at all)
            force_refresh = True
        if db_item and force_refresh:
            # get (first) provider item id belonging to this db item
            provider_id, provider_item_id = await self.get_provider_id(db_item)
        elif db_item:
            # we have a db item and no refreshing is needed, return the results!
            return db_item
        if not details and provider_id:
            # no details provider nor in db, fetch them from the provider
            details = await self.get_provider_item(provider_item_id, provider_id)
        if not details and provider:
            # check providers for given provider type one by one
            for prov in self.mass.music.providers:
                if not prov.available:
                    continue
                if prov.type == provider:
                    try:
                        details = await self.get_provider_item(
                            provider_item_id, prov.id
                        )
                    except MediaNotFoundError:
                        pass
                    else:
                        break
        if not details:
            # we couldn't get a match from any of the providers, raise error
            raise MediaNotFoundError(
                f"Item not found: {provider.value or provider_id}/{provider_item_id}"
            )
        # create job to add the item to the db, including matching metadata etc. takes some time
        # in 99% of the cases we just return lazy because we want the details as fast as possible
        # only if we really need to wait for the result (e.g. to prevent race conditions), we
        # can set lazy to false and we await to job to complete.
        add_job = self.mass.add_job(
            self.add(details, overwrite_existing=overwrite_existing),
            f"Add {details.uri} to database",
        )
        if not lazy:
            await add_job.wait()
            return add_job.result

        return db_item if db_item else details

    async def search(
        self,
        search_query: str,
        provider: Optional[ProviderType] = None,
        provider_id: Optional[str] = None,
        limit: int = 25,
    ) -> List[ItemCls]:
        """Search database or provider with given query."""
        # create safe search string
        search_query = search_query.replace("/", " ").replace("'", "")
        if provider == ProviderType.DATABASE or provider_id == "database":
            return [
                self.item_cls.from_db_row(db_row)
                for db_row in await self.mass.database.search(
                    self.db_table, search_query
                )
            ]

        prov = self.mass.music.get_provider(provider_id or provider)
        if not prov or MusicProviderFeature.SEARCH not in prov.supported_features:
            return []

        # prefer cache items (if any)
        cache_key = (
            f"{prov.type.value}.search.{self.media_type.value}.{search_query}.{limit}"
        )
        if cache := await self.mass.cache.get(cache_key):
            return [media_from_dict(x) for x in cache]
        # no items in cache - get listing from provider
        items = await prov.search(
            search_query,
            [self.media_type],
            limit,
        )
        # store (serializable items) in cache
        if not prov.type.is_file():  # do not cache filesystem results
            self.mass.create_task(
                self.mass.cache.set(
                    cache_key, [x.to_dict() for x in items], expiration=86400 * 7
                )
            )
        return items

    async def add_to_library(
        self,
        provider_item_id: str,
        provider: Optional[ProviderType] = None,
        provider_id: Optional[str] = None,
    ) -> None:
        """Add an item to the library."""
        prov_item = await self.get_db_item_by_prov_id(
            provider_item_id, provider=provider, provider_id=provider_id
        )
        if prov_item is None:
            prov_item = await self.get_provider_item(
                provider_item_id, provider_id or provider
            )
        if prov_item.in_library is True:
            return
        # mark as favorite/library item on provider(s)
        for prov_id in prov_item.provider_ids:
            if prov := self.mass.music.get_provider(prov_id.prov_id):
                await prov.library_add(prov_id.item_id, self.media_type)
        # mark as library item in internal db if db item
        if prov_item.provider == ProviderType.DATABASE:
            if not prov_item.in_library:
                prov_item.in_library = True
                await self.set_db_library(prov_item.item_id, True)

    async def remove_from_library(
        self,
        provider_item_id: str,
        provider: Optional[ProviderType] = None,
        provider_id: Optional[str] = None,
    ) -> None:
        """Remove item from the library."""
        prov_item = await self.get_db_item_by_prov_id(
            provider_item_id, provider=provider, provider_id=provider_id
        )
        if prov_item is None:
            prov_item = await self.get_provider_item(
                provider_item_id, provider_id or provider
            )
        if prov_item.in_library is False:
            return
        # unmark as favorite/library item on provider(s)
        for prov_id in prov_item.provider_ids:
            if prov := self.mass.music.get_provider(prov_id.prov_id):
                await prov.library_remove(prov_id.item_id, self.media_type)
        # unmark as library item in internal db if db item
        if prov_item.provider == ProviderType.DATABASE:
            prov_item.in_library = False
            await self.set_db_library(prov_item.item_id, False)

    async def get_provider_id(self, item: ItemCls) -> Tuple[str, str]:
        """Return provider and item id."""
        if item.provider == ProviderType.DATABASE:
            # make sure we have a full object
            item = await self.get_db_item(item.item_id)
        for prov in item.provider_ids:
            # returns the first provider that is available
            if not prov.available:
                continue
            if self.mass.music.get_provider(prov.prov_id):
                return (prov.prov_id, prov.item_id)
        return None, None

    async def get_db_items_by_query(
        self,
        custom_query: Optional[str] = None,
        query_params: Optional[dict] = None,
        limit: int = 500,
        offset: int = 0,
    ) -> List[ItemCls]:
        """Fetch MediaItem records from database given a custom query."""
        return [
            self.item_cls.from_db_row(db_row)
            for db_row in await self.mass.database.get_rows_from_query(
                custom_query, query_params, limit=limit, offset=offset
            )
        ]

    async def get_db_item(self, item_id: Union[int, str]) -> ItemCls:
        """Get record by id."""
        match = {"item_id": int(item_id)}
        if db_row := await self.mass.database.get_row(self.db_table, match):
            return self.item_cls.from_db_row(db_row)
        return None

    async def get_db_item_by_prov_id(
        self,
        provider_item_id: str,
        provider: Optional[ProviderType] = None,
        provider_id: Optional[str] = None,
    ) -> ItemCls | None:
        """Get the database item for the given prov_id."""
        assert provider or provider_id, "provider or provider_id must be supplied"
        if isinstance(provider, str):
            provider = ProviderType(provider)
        if provider == ProviderType.DATABASE or provider_id == "database":
            return await self.get_db_item(provider_item_id)
        for item in await self.get_db_items_by_prov_id(
            provider=provider,
            provider_id=provider_id,
            provider_item_ids=(provider_item_id,),
        ):
            return item
        return None

    async def get_db_items_by_prov_id(
        self,
        provider: Optional[ProviderType] = None,
        provider_id: Optional[str] = None,
        provider_item_ids: Optional[Tuple[str]] = None,
        limit: int = 500,
        offset: int = 0,
    ) -> List[ItemCls]:
        """Fetch all records from database for given provider."""
        assert provider or provider_id, "provider or provider_id must be supplied"
        if isinstance(provider, str):
            provider = ProviderType(provider)
        if provider == ProviderType.DATABASE or provider_id == "database":
            return await self.get_db_items_by_query(limit=limit, offset=offset)

        query = f"SELECT * FROM {self.db_table}, json_each(provider_ids)"
        if provider_id is not None:
            query += (
                f" WHERE json_extract(json_each.value, '$.prov_id') = '{provider_id}'"
            )
        elif provider is not None:
            query += f" WHERE json_extract(json_each.value, '$.prov_type') = '{provider.value}'"
        if provider_item_ids is not None:
            prov_ids = str(tuple(provider_item_ids))
            if prov_ids.endswith(",)"):
                prov_ids = prov_ids.replace(",)", ")")
            query += f" AND json_extract(json_each.value, '$.item_id') in {prov_ids}"

        return await self.get_db_items_by_query(query, limit=limit, offset=offset)

    async def set_db_library(self, item_id: int, in_library: bool) -> None:
        """Set the in-library bool on a database item."""
        match = {"item_id": item_id}
        timestamp = int(time()) if in_library else 0
        await self.mass.database.update(
            self.db_table, match, {"in_library": in_library, "timestamp": timestamp}
        )
        db_item = await self.get_db_item(item_id)
        self.mass.signal_event(
            MassEvent(EventType.MEDIA_ITEM_UPDATED, db_item.uri, db_item)
        )

    async def get_provider_item(
        self,
        item_id: str,
        provider_id: Union[str, ProviderType],
    ) -> ItemCls:
        """Return item details for the given provider item id."""
        if provider_id in ("database", ProviderType.DATABASE):
            item = await self.get_db_item(item_id)
        else:
            provider = self.mass.music.get_provider(provider_id)
            item = await provider.get_item(self.media_type, item_id)
        if not item:
            raise MediaNotFoundError(
                f"{self.media_type.value} {item_id} not found on provider {provider.name}"
            )
        return item

    async def remove_prov_mapping(self, item_id: int, prov_id: str) -> None:
        """Remove provider id(s) from item."""
        if db_item := await self.get_db_item(item_id):
            db_item.provider_ids = {
                x for x in db_item.provider_ids if x.prov_id != prov_id
            }
            if not db_item.provider_ids:
                # item has no more provider_ids left, it is completely deleted
                try:
                    await self.delete_db_item(db_item.item_id)
                except AssertionError:
                    self.logger.debug(
                        "Could not delete %s: it has items attached", db_item.item_id
                    )
                return
            await self.update_db_item(db_item.item_id, db_item, overwrite=True)

        self.logger.debug("removed provider %s from item id %s", prov_id, item_id)

    async def delete_db_item(self, item_id: int, recursive: bool = False) -> None:
        """Delete record from the database."""
        db_item = await self.get_db_item(item_id)
        assert db_item, f"Item does not exist: {item_id}"
        # delete item
        await self.mass.database.delete(
            self.db_table,
            {"item_id": int(item_id)},
        )
        # NOTE: this does not delete any references to this item in other records,
        # this is handled/overridden in the mediatype specific controllers
        self.mass.signal_event(
            MassEvent(EventType.MEDIA_ITEM_DELETED, db_item.uri, db_item)
        )
        self.logger.debug("deleted item with id %s from database", item_id)
