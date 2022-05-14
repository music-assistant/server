"""Model for a base media_controller."""
from __future__ import annotations

from abc import ABCMeta, abstractmethod
from time import time
from typing import TYPE_CHECKING, Generic, List, Optional, Tuple, TypeVar

from databases import Database as Db

from music_assistant.models.errors import MediaNotFoundError, ProviderUnavailableError

from .enums import MediaType, ProviderType
from .media_items import MediaItemType

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

    @abstractmethod
    async def add(self, item: ItemCls) -> ItemCls:
        """Add item to local db and return the database item."""
        raise NotImplementedError

    async def library(self) -> List[ItemCls]:
        """Get all in-library items."""
        match = {"in_library": True}
        return [
            self.item_cls.from_db_row(db_row)
            for db_row in await self.mass.database.get_rows(self.db_table, match)
        ]

    async def count(self) -> int:
        """Return number of in-library items for this MediaType."""
        return await self.mass.database.get_count(self.db_table, {"in_library": 1})

    async def get(
        self,
        provider_item_id: str,
        provider: Optional[ProviderType] = None,
        provider_id: Optional[str] = None,
        force_refresh: bool = False,
        lazy: bool = True,
        details: ItemCls = None,
    ) -> ItemCls:
        """Return (full) details for a single media item."""
        assert provider or provider_id, "provider or provider_id must be supplied"
        db_item = await self.get_db_item_by_prov_id(
            provider_item_id=provider_item_id,
            provider=provider,
            provider_id=provider_id,
        )
        if db_item and (time() - db_item.last_refresh) > REFRESH_INTERVAL:
            force_refresh = True
        if db_item and force_refresh:
            provider_id, provider_item_id = await self.get_provider_id(db_item)
        elif db_item:
            return db_item
        if not details and provider_id:
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
        if not lazy:
            return await self.add(details)
        self.mass.add_job(self.add(details), f"Add {details.uri} to database")
        return db_item if db_item else details

    async def search(
        self,
        search_query: str,
        provider: Optional[ProviderType] = None,
        provider_id: Optional[str] = None,
        limit: int = 25,
    ) -> List[ItemCls]:
        """Search database or provider with given query."""
        if provider == ProviderType.DATABASE or provider_id == "database":
            return [
                self.item_cls.from_db_row(db_row)
                for db_row in await self.mass.database.search(
                    self.db_table, search_query
                )
            ]

        provider = self.mass.music.get_provider(provider_id or provider)
        if not provider:
            return {}
        return await provider.search(
            search_query,
            [self.media_type],
            limit,
        )

    async def add_to_library(
        self,
        provider_item_id: str,
        provider: Optional[ProviderType] = None,
        provider_id: Optional[str] = None,
    ) -> None:
        """Add an item to the library."""
        # make sure we have a valid full item
        db_item = await self.get(
            provider_item_id, provider=provider, provider_id=provider_id, lazy=False
        )
        # add to provider libraries
        for prov_id in db_item.provider_ids:
            if prov := self.mass.music.get_provider(prov_id.prov_id):
                await prov.library_add(prov_id.item_id, self.media_type)
        # mark as library item in internal db
        if not db_item.in_library:
            await self.set_db_library(db_item.item_id, True)

    async def remove_from_library(
        self,
        provider_item_id: str,
        provider: Optional[ProviderType] = None,
        provider_id: Optional[str] = None,
    ) -> None:
        """Remove item from the library."""
        # make sure we have a valid full item
        db_item = await self.get(
            provider_item_id, provider=provider, provider_id=provider_id, lazy=False
        )
        # add to provider's libraries
        for prov_id in db_item.provider_ids:
            if prov := self.mass.music.get_provider(prov_id.prov_id):
                await prov.library_remove(prov_id.item_id, self.media_type)
        # unmark as library item in internal db
        if db_item.in_library:
            await self.set_db_library(db_item.item_id, False)

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

    async def get_db_items(
        self, custom_query: Optional[str] = None, db: Optional[Db] = None
    ) -> List[ItemCls]:
        """Fetch all records from database."""
        if custom_query is not None:
            func = self.mass.database.get_rows_from_query(custom_query, db=db)
        else:
            func = self.mass.database.get_rows(self.db_table, db=db)
        return [self.item_cls.from_db_row(db_row) for db_row in await func]

    async def get_db_item(self, item_id: int, db: Optional[Db] = None) -> ItemCls:
        """Get record by id."""
        match = {"item_id": int(item_id)}
        if db_row := await self.mass.database.get_row(self.db_table, match, db=db):
            return self.item_cls.from_db_row(db_row)
        return None

    async def get_db_item_by_prov_id(
        self,
        provider_item_id: str,
        provider: Optional[ProviderType] = None,
        provider_id: Optional[str] = None,
        db: Optional[Db] = None,
    ) -> ItemCls | None:
        """Get the database album for the given prov_id."""
        assert provider or provider_id, "provider or provider_id must be supplied"
        if provider == ProviderType.DATABASE or provider_id == "database":
            return await self.get_db_item(provider_item_id, db=db)
        if item_id := await self.mass.music.get_provider_mapping(
            self.media_type, provider_item_id, provider, provider_id=provider_id, db=db
        ):
            return await self.get_db_item(item_id, db=db)
        return None

    async def get_db_items_by_prov_id(
        self,
        provider: Optional[ProviderType] = None,
        provider_id: Optional[str] = None,
        db: Optional[Db] = None,
    ) -> List[ItemCls]:
        """Fetch all records from database for given provider."""
        assert provider or provider_id, "provider or provider_id must be supplied"
        db_ids = await self.mass.music.get_provider_mappings(
            self.media_type, provider=provider, provider_id=provider_id, db=db
        )
        query = f"SELECT * FROM tracks WHERE item_id in {str(tuple(db_ids))}"
        return await self.get_db_items(query, db=db)

    async def set_db_library(self, item_id: int, in_library: bool) -> None:
        """Set the in-library bool on a database item."""
        match = {"item_id": item_id}
        await self.mass.database.update(
            self.db_table,
            match,
            {"in_library": in_library},
        )

    async def get_provider_item(
        self,
        item_id: str,
        provider_id: str,
    ) -> ItemCls:
        """Return item details for the given provider item id."""
        if provider_id == "database":
            item = await self.get_db_item(item_id)
        else:
            provider = self.mass.music.get_provider(provider_id)
            if not provider:
                raise ProviderUnavailableError(
                    f"Provider {provider_id} is not available!"
                )
            item = await provider.get_item(self.media_type, item_id)
        if not item:
            raise MediaNotFoundError(
                f"{self.media_type.value} {item_id} not found on provider {provider.name}"
            )
        return item
