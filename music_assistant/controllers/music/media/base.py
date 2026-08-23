"""Base (ABC) MediaType specific controller."""

from __future__ import annotations

import asyncio
import logging
from abc import ABCMeta, abstractmethod
from collections.abc import Iterable
from contextlib import suppress
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, TypeVar, cast, final, overload

from music_assistant_models.auth import Scope
from music_assistant_models.enums import (
    EventType,
    ExternalID,
    ImageType,
    MediaType,
    ProviderFeature,
    ProviderType,
)
from music_assistant_models.errors import (
    InsufficientPermissions,
    InvalidDataError,
    MediaNotFoundError,
    ProviderUnavailableError,
)
from music_assistant_models.helpers import create_safe_string, get_global_cache_value
from music_assistant_models.media_items import (
    AudioFormat,
    ItemMapping,
    ItemMappingSummary,
    MediaCollection,
    MediaItemImage,
    MediaItemMetadata,
    MediaItemMetadataSummary,
    MediaItemSummaryType,
    MediaItemType,
    ProviderMapping,
    UniqueList,
)

from music_assistant.constants import (
    DB_TABLE_ALBUM_ARTISTS,
    DB_TABLE_ALBUM_TRACKS,
    DB_TABLE_AUDIO_ANALYSIS,
    DB_TABLE_AUDIOBOOK_ARTISTS,
    DB_TABLE_EXTERNAL_ID_LOOKUP,
    DB_TABLE_GENRE_MEDIA_ITEM_EXCLUSION,
    DB_TABLE_GENRE_MEDIA_ITEM_MAPPING,
    DB_TABLE_PLAYLOG,
    DB_TABLE_PROVIDER_MAPPINGS,
    DB_TABLE_TRACK_ARTISTS,
    MASS_LOGGER_NAME,
)
from music_assistant.controllers.music.helpers import search_name_match_clause
from music_assistant.controllers.webserver.helpers.auth_middleware import get_current_user
from music_assistant.helpers.collections import (
    get_collection_item_id,
    get_collection_name_from_item_id,
)
from music_assistant.helpers.compare import compare_media_item
from music_assistant.helpers.database import UNSET
from music_assistant.helpers.external_ids import (
    external_id_lookup_values,
    external_id_lookup_values_untyped,
    external_id_sort_key,
    normalize_external_ids,
)
from music_assistant.helpers.json import json_loads, serialize_to_json
from music_assistant.helpers.util import guard_single_request, parse_optional_bool

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Mapping

    from music_assistant import MusicAssistant
    from music_assistant.models.music_provider import MusicProvider
    from music_assistant.models.plugin import PluginProvider


ItemCls = TypeVar("ItemCls", bound="MediaItemType")


JSON_KEYS = (
    "artists",
    "track_album",
    "metadata",
    "provider_mappings",
    "external_ids",
    "narrators",
    "authors",
    "genre_aliases",
    "supported_mediatypes",
    "translation_params",
    "audiobook_artists",
)

# The columns that make up a relation row, so a merge can copy it onto the target
# without relying on SELECT *: album_tracks carries a surrogate autoincrement id that
# must not be copied along.
RELATION_TABLE_COLUMNS = {
    DB_TABLE_ALBUM_ARTISTS: ("album_id", "artist_id"),
    DB_TABLE_ALBUM_TRACKS: ("track_id", "album_id", "disc_number", "track_number"),
    DB_TABLE_AUDIOBOOK_ARTISTS: ("audiobook_id", "artist_id"),
    DB_TABLE_TRACK_ARTISTS: ("track_id", "artist_id"),
}

# When set (task-local), per-item MEDIA_ITEM_ADDED/UPDATED events and the on_item_updated
# provider write-back are suppressed, so bulk operations (provider sync, provider cleanup)
# don't flood subscribers with one event per touched item.
SUPPRESS_MEDIA_ITEM_UPDATES: ContextVar[bool] = ContextVar(
    "SUPPRESS_MEDIA_ITEM_UPDATES", default=False
)

# When set (task-local), the current library update is authoritative and persists the given item
# as the complete state, allowing fields the source no longer provides to be cleared. It is an
# internal control for the filesystem sidecar refresh, deliberately kept off the public update
# command so external clients cannot request a destructive full replace. Only album and artist
# honor it.
FULL_REPLACE_UPDATE: ContextVar[bool] = ContextVar("FULL_REPLACE_UPDATE", default=False)

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
    "album_artist_name": "artists.search_name ASC, year DESC",
    "album_artist_name_desc": "artists.search_name DESC, year DESC",
    "track_artist_name": "artists.search_name ASC, search_name ASC",
    "track_artist_name_desc": "artists.search_name DESC, search_name ASC",
    "random": "RANDOM()",
    "random_play_count": "RANDOM(), play_count ASC",
}


@dataclass(slots=True)
class LibraryItemSyncDetails:
    """
    Lightweight snapshot of a library item with just the fields the library sync needs.

    Used by the provider sync loops to detect (un)changed items without hydrating
    full MediaItem objects from the database.
    """

    item_id: int
    favorite: bool
    date_added: datetime
    provider_mappings: set[ProviderMapping]


@dataclass(slots=True)
class TrackSyncDetails(LibraryItemSyncDetails):
    """Lightweight sync snapshot of a library track."""

    has_album: bool
    has_artists: bool


@dataclass(slots=True)
class AudiobookSyncDetails(LibraryItemSyncDetails):
    """Lightweight sync snapshot of a library audiobook."""

    author_is_str: bool
    narrator_is_str: bool
    fully_played: bool | None
    resume_position_ms: int | None


class MediaControllerBase[ItemCls: "MediaItemType"](metaclass=ABCMeta):
    """Base model for controller managing a MediaType."""

    media_type: MediaType
    item_cls: type[MediaItemType]
    summary_item_cls: type[MediaItemSummaryType]
    db_table: str

    def __init__(self, mass: MusicAssistant) -> None:
        """Initialize class."""
        self.mass = mass
        self.logger = logging.getLogger(f"{MASS_LOGGER_NAME}.music.{self.media_type.value}")
        # register (base) api handlers
        self.api_base = api_base = f"{self.media_type}s"
        self.mass.register_api_command(
            f"music/{api_base}/count", self.library_count, required_scope=Scope.LIBRARY_READ
        )
        self.mass.register_api_command(
            f"music/{api_base}/library_items",
            self.library_items,
            required_scope=Scope.LIBRARY_READ,
            allow_impersonation=True,
        )
        self.mass.register_api_command(
            f"music/{api_base}/get", self.get, required_scope=Scope.LIBRARY_READ
        )
        self.mass.register_api_command(
            f"music/{api_base}/get_by_external_id",
            self.get_library_item_by_external_id,
            required_scope=Scope.LIBRARY_READ,
        )
        self.mass.register_api_command(
            f"music/{api_base}/get_collection",
            self.get_collection,
            required_scope=Scope.LIBRARY_READ,
            allow_impersonation=True,
        )
        # Backward compatibility alias - prefer the generic "get" endpoint
        self.mass.register_api_command(
            f"music/{api_base}/get_{self.media_type}",
            self.get,
            required_scope=Scope.LIBRARY_READ,
            alias=True,
        )
        self.mass.register_api_command(
            f"music/{api_base}/update",
            self.update_item_in_library,
            required_scope=Scope.LIBRARY_MANAGE,
        )
        self.mass.register_api_command(
            f"music/{api_base}/remove",
            self.remove_item_from_library,
            required_scope=Scope.LIBRARY_MANAGE,
        )
        self._db_add_lock = asyncio.Lock()

    @property
    def translation_owner(self) -> str:
        """Return the "core.music" namespace these media controllers' translation strings live under."""
        return "core.music"

    @property
    def base_query(self) -> tuple[str, dict[str, Any]]:
        """
        Return the base SELECT query for this media type and its bound query params.

        Override in a subclass to customize the query (extra joins/columns) and/or to
        inject dynamic, parameterized filters.
        """
        query = f"""
        SELECT
            {self.db_table}.*,
            {self._external_ids_query()} AS external_ids,
            {self._provider_mappings_query()} AS provider_mappings
            FROM {self.db_table} """
        return query, {}

    @property
    def summary_query(self) -> tuple[str, dict[str, Any]]:
        """
        Return the slim SELECT query used for summary listings and its bound query params.

        Selects only the columns needed to build summary items. Override in a subclass
        to select additional per-type columns.
        """
        query = f"""
        SELECT
            {self._summary_base_columns()},
            {self._provider_mappings_query()} AS provider_mappings
            FROM {self.db_table} """
        return query, {}

    @final
    async def add_item_to_library(
        self,
        item: ItemCls,
        overwrite_existing: bool = False,
    ) -> ItemCls:
        """Add item to library and return the new (or updated) database item."""
        new_item = False
        # batch the many writes of an item add/update into a single commit
        async with self.mass.music.database.deferred_commit():
            # check for existing item first
            if library_id := await self._get_library_item_by_match(item):
                # update existing item
                await self._update_library_item(library_id, item, overwrite=overwrite_existing)
            else:
                # actually add a new item in the library db
                self.mass.music.match_provider_instances(item)
                async with self._db_add_lock:
                    # Another task may have inserted the same item while this task waited.
                    if library_id := await self._get_library_item_by_match(item):
                        await self._update_library_item(
                            library_id, item, overwrite=overwrite_existing
                        )
                    else:
                        library_id = await self._add_library_item(item)
                        new_item = True
        # return final library_item
        library_item = await self.get_library_item(library_id)
        if not SUPPRESS_MEDIA_ITEM_UPDATES.get():
            self.mass.signal_event(
                EventType.MEDIA_ITEM_ADDED if new_item else EventType.MEDIA_ITEM_UPDATED,
                library_item.uri,
                library_item,
            )
        return library_item

    @final
    async def update_item_in_library(
        self,
        item_id: str | int,
        update: ItemCls,
        overwrite: bool = False,
    ) -> ItemCls:
        """
        Update existing library record in the library database.

        :param item_id: The library item id to update.
        :param update: The item carrying the new values.
        :param overwrite: Replace this provider's values, keeping other providers' data.
        """
        self.mass.music.match_provider_instances(update)
        # batch the many writes of an item update into a single commit
        async with self.mass.music.database.deferred_commit():
            await self._update_library_item(
                item_id, update, overwrite=overwrite, full_replace=FULL_REPLACE_UPDATE.get()
            )
        # return the updated object
        library_item = await self.get_library_item(item_id)
        if SUPPRESS_MEDIA_ITEM_UPDATES.get():
            # during a sync the update originates from the provider itself,
            # so skip both the event and the write-back to that provider
            return library_item
        # drop cached artwork for the updated item so replaced art is served fresh
        for img in library_item.metadata.images or []:
            await self.mass.metadata.invalidate_image_cache(img.provider, img.path)
        self.mass.signal_event(
            EventType.MEDIA_ITEM_UPDATED,
            library_item.uri,
            library_item,
        )
        # notify music providers of the update so they can sync their own storage
        for prov_mapping in library_item.provider_mappings:
            if provider := self.mass.get_provider(prov_mapping.provider_instance):
                if provider.type != ProviderType.MUSIC:
                    continue
                provider = cast("MusicProvider", provider)
                await provider.on_item_updated(library_item)
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
        # cleanup external_id_lookup table
        await self.mass.music.database.delete(
            DB_TABLE_EXTERNAL_ID_LOOKUP,
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
            # cleanup audio analysis rows for this provider mapping
            for prov_key in (prov_mapping.provider_domain, prov_mapping.provider_instance):
                await self.mass.music.database.delete(
                    DB_TABLE_AUDIO_ANALYSIS,
                    {
                        "media_type": self.media_type.value,
                        "item_id": prov_mapping.item_id,
                        "provider": prov_key,
                    },
                )
        # delete genre exclusions for this media item
        await self.mass.music.database.delete(
            DB_TABLE_GENRE_MEDIA_ITEM_EXCLUSION,
            {"media_type": self.media_type.value, "media_id": db_id},
        )
        # NOTE: this does not delete any references to this item in other records,
        # this is handled/overridden in the mediatype specific controllers
        # drop cached artwork for the removed item
        for img in library_item.metadata.images or []:
            await self.mass.metadata.invalidate_image_cache(img.provider, img.path)
        if not SUPPRESS_MEDIA_ITEM_UPDATES.get():
            self.mass.signal_event(EventType.MEDIA_ITEM_DELETED, library_item.uri, library_item)
        self.logger.debug("deleted item with id %s from database", db_id)

    async def library_count(self, favorite_only: bool = False) -> int:
        """
        Return the number of items in the library.

        Restricted to the providers the current user is allowed to see when that user
        has a provider filter set.

        :param favorite_only: Only count items marked as favorite.
        """
        query_parts: list[str] = []
        query_params: dict[str, Any] = {}
        if favorite_only:
            query_parts.append("favorite = 1")
        if provider_filter := self._ensure_provider_filter(None):
            query_parts.append(
                self._provider_filter_clause(query_params, provider_filter, in_library_only=True)
            )
        if not query_parts:
            return await self.mass.music.database.get_count(self.db_table)
        sql_query = f"SELECT item_id FROM {self.db_table} WHERE {' AND '.join(query_parts)}"
        return await self.mass.music.database.get_count_from_query(sql_query, query_params)

    if TYPE_CHECKING:

        @overload
        async def library_items(
            self,
            favorite: bool | None = None,
            search: str | None = None,
            limit: int = 500,
            offset: int = 0,
            order_by: str = "sort_name",
            provider: str | list[str] | None = None,
            genre: int | list[int] | None = None,
            played_only: bool = False,
            *,
            summary: bool = True,
            collapse_collections: Literal[False] = False,
            reachable_via: list[str] | None = None,
            **kwargs: Any,
        ) -> list[ItemCls]: ...

        @overload
        async def library_items(
            self,
            favorite: bool | None = None,
            search: str | None = None,
            limit: int = 500,
            offset: int = 0,
            order_by: str = "sort_name",
            provider: str | list[str] | None = None,
            genre: int | list[int] | None = None,
            played_only: bool = False,
            *,
            summary: bool = True,
            collapse_collections: Literal[True],
            reachable_via: list[str] | None = None,
            **kwargs: Any,
        ) -> list[ItemCls] | list[ItemCls | MediaCollection[ItemCls]]: ...

        @overload
        async def library_items(
            self,
            favorite: bool | None = None,
            search: str | None = None,
            limit: int = 500,
            offset: int = 0,
            order_by: str = "sort_name",
            provider: str | list[str] | None = None,
            genre: int | list[int] | None = None,
            played_only: bool = False,
            *,
            summary: bool = True,
            collapse_collections: bool,
            reachable_via: list[str] | None = None,
            **kwargs: Any,
        ) -> list[ItemCls] | list[ItemCls | MediaCollection[ItemCls]]: ...

    async def library_items(  # noqa: PLR0913
        self,
        favorite: bool | None = None,
        search: str | None = None,
        limit: int = 500,
        offset: int = 0,
        order_by: str = "sort_name",
        provider: str | list[str] | None = None,
        genre: int | list[int] | None = None,
        played_only: bool = False,
        *,
        summary: bool = True,
        collapse_collections: bool = False,
        reachable_via: list[str] | None = None,
        **kwargs: Any,
    ) -> list[ItemCls] | list[ItemCls | MediaCollection[ItemCls]]:
        """
        Get the library items for this mediatype.

        :param favorite: Filter by favorite status.
        :param search: Filter by search query.
        :param limit: Maximum number of items to return.
        :param offset: Number of items to skip.
        :param order_by: Order by field (e.g. 'sort_name', 'timestamp_added').
        :param provider: Filter by provider instance ID (single string or list).
        :param genre: Filter by genre id(s).
        :param played_only: Only include items that have been played (last_played > 0).
        :param summary: When True (default), return slim summary items containing only the
            fields needed for a list view. Set to False to get fully hydrated items.
        :param collapse_collections: Collapse available collections. Items in a collection won't
            be returned individually.
        :param reachable_via: Restrict results to items with a provider mapping reachable
            through one of these provider instance ids (OR semantics), regardless of
            whether that mapping is itself in that provider's own library. This is
            independent of `provider`, which instead requires the *matched* mapping to
            be in-library. None applies no filter; an explicit empty list, or a list
            with no currently loaded/allowed instance, returns no items.
        """
        reachable_via = self._resolve_reachable_via(reachable_via)
        if reachable_via is not None and not reachable_via:
            return []
        items = await self.get_library_items_by_query(
            favorite=favorite,
            search=search,
            limit=limit,
            offset=offset,
            order_by=order_by,
            provider_filter=self._provider_filter_considering_reachability(provider, reachable_via),
            genre_ids=genre,
            played_only=played_only,
            in_library_only=True,
            summary=summary,
            collapse_collections=collapse_collections,
            reachable_via=reachable_via,
        )
        if (
            kwargs.get("_localized_fallback", True)
            and search
            and not items
            and self.media_type in (MediaType.GENRE, MediaType.PLAYLIST)
        ):
            return await self._localized_search_fallback(
                search,
                limit=limit,
                offset=offset,
                favorite=favorite,
                order_by=order_by,
                provider=provider,
                genre=genre,
                summary=summary,
                reachable_via=reachable_via,
            )
        return items

    async def iter_library_items(
        self,
        favorite: bool | None = None,
        search: str | None = None,
        order_by: str = "sort_name",
        provider: str | list[str] | None = None,
        genre: int | list[int] | None = None,
        library_items_only: bool = True,
    ) -> AsyncGenerator[ItemCls]:
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
                genre_ids=genre,
                limit=limit,
                offset=offset,
                order_by=order_by,
                provider_filter=provider_filter,
                in_library_only=library_items_only,
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
        allow_update_metadata: bool = True,
    ) -> ItemCls:
        """
        Return (full) details for a single media item.

        Tries to find the item in the library first, falling back to
        fetching directly from the provider if not found.

        :param item_id: The provider item id to fetch.
        :param provider_instance_id_or_domain: The provider instance id or
            domain to fetch the item from.
        :param allow_update_metadata: Schedule a metadata refresh on access.
            Set to False when fetching items in bulk (e.g. provider sync).
        """
        # always prefer the full library item if we have it
        if library_item := await self.get_library_item_by_prov_id(
            item_id,
            provider_instance_id_or_domain,
        ):
            # schedule a refresh of the metadata on access of the item
            # e.g. the item is being played or opened in the UI
            if allow_update_metadata:
                assert library_item.uri is not None
                self.mass.metadata.schedule_update_metadata(library_item)
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
            return await self.library_items(
                search=search_query, limit=limit, summary=False, collapse_collections=False
            )
        if not (prov := self.mass.get_provider(provider_instance_id_or_domain)):
            return []
        if prov.type != ProviderType.MUSIC:
            return []
        prov = cast("MusicProvider", prov)
        if ProviderFeature.SEARCH not in prov.supported_features:
            return []
        if self.media_type not in prov.supported_media_types:
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

    async def get_collection(self, item_id: str) -> MediaCollection[ItemCls]:
        """Get a single collection."""
        name = get_collection_name_from_item_id(item_id)
        query_params: dict[str, Any] = {"collection_name": name}
        sql_query, base_query_params = self._build_final_query([], [], None, summary=False)
        for key, value in base_query_params.items():
            query_params.setdefault(key, value)
        sql_query = await self._adapt_query_for_collections(
            sql_query, query_params, summary=False, order_by=None, collection_name=name
        )
        db_rows = await self.mass.music.database.get_rows_from_query(
            sql_query, query_params, limit=1, offset=0
        )
        if len(db_rows) != 1:
            raise MediaNotFoundError(f"Collection {name} not found.")

        return cast(
            "MediaCollection[ItemCls]",
            MediaCollection(
                item_id=get_collection_item_id(db_rows[0]["name"], item_media_type=self.media_type),
                name=db_rows[0]["name"],
                provider="library",
                provider_mappings=set(),
                items=UniqueList(
                    [
                        self.item_cls.from_dict(self._parse_db_row(json_loads(x)))
                        for x in json_loads(db_rows[0]["media_data"])
                    ]
                ),
            ),
        )

    async def get_library_item(self, item_id: int | str) -> ItemCls:
        """Get single library item by id."""
        db_id = int(item_id)  # ensure integer
        extra_query = f"WHERE {self.db_table}.item_id = :item_id"
        for db_item in await self.get_library_items_by_query(
            extra_query_parts=[extra_query],
            extra_query_params={"item_id": db_id},
            in_library_only=False,
        ):
            return db_item
        msg = f"{self.media_type.value} not found in library: {db_id}"
        raise MediaNotFoundError(msg)

    async def get_library_item_by_prov_id(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
    ) -> ItemCls | None:
        """Get the library item for the given provider item, if present."""
        assert item_id
        assert provider_instance_id_or_domain
        if provider_instance_id_or_domain == "library":
            try:
                return await self.get_library_item(item_id)
            except MediaNotFoundError:
                return None
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
    async def get_library_item_sync_details(
        self,
        provider_mappings: Iterable[ProviderMapping],
    ) -> LibraryItemSyncDetails | None:
        """
        Get a lightweight sync snapshot of the library item for the given provider mappings.

        Returns only the scalar columns and raw provider mapping rows the library sync
        needs for its change detection, without hydrating a full MediaItem object.
        Resolution order matches get_library_item_by_prov_mappings (instance first,
        then domain).
        """
        extra_columns, extra_joins, extra_params = self._sync_details_query_parts()
        base_sql = f"""
            SELECT
                {self.db_table}.item_id,
                {self.db_table}.favorite,
                {self.db_table}.timestamp_added,
                (SELECT JSON_GROUP_ARRAY(
                    json_object(
                        'item_id', pm.provider_item_id,
                        'provider_domain', pm.provider_domain,
                        'provider_instance', pm.provider_instance,
                        'available', pm.available,
                        'in_library', pm.in_library,
                        'is_unique', pm.is_unique
                    )) FROM provider_mappings pm WHERE pm.item_id = {self.db_table}.item_id
                        AND pm.media_type = '{self.media_type.value}') AS provider_mappings
                {extra_columns}
            FROM {self.db_table}
            {extra_joins}
            WHERE {self.db_table}.item_id IN (
                SELECT item_id FROM provider_mappings
                WHERE provider_mappings.media_type = '{self.media_type.value}'
                AND provider_mappings.{{prov_column}} = :prov_id
                AND provider_mappings.provider_item_id = :prov_item_id
            )
        """
        # always prefer provider instance first, then domain
        # (same resolution order as get_library_item_by_prov_mappings)
        for prov_column in ("provider_instance", "provider_domain"):
            for mapping in provider_mappings:
                for db_row in await self.mass.music.database.get_rows_from_query(
                    base_sql.format(prov_column=prov_column),
                    {
                        **extra_params,
                        "prov_id": getattr(mapping, prov_column),
                        "prov_item_id": mapping.item_id,
                    },
                    limit=1,
                ):
                    return self._parse_sync_details_row(db_row)
        return None

    @final
    async def get_library_items_by_external_id(
        self,
        external_id: str,
        external_id_type: ExternalID | None = None,
        *,
        limit: int | None,
    ) -> list[ItemCls]:
        """
        Get library items for the given external identifier.

        :param external_id: External identifier value to look up.
        :param external_id_type: Optional identifier type.
        :param limit: Maximum number of library items to return, or None for all matches.
        """
        if external_id_type:
            lookup_values = external_id_lookup_values(external_id_type, external_id)
        else:
            lookup_values = external_id_lookup_values_untyped(external_id)
        subquery_parts = [
            "media_type = :ext_id_media_type",
            "external_id IN :external_ids",
        ]
        query_params: dict[str, Any] = {
            "ext_id_media_type": self.media_type.value,
            "external_ids": lookup_values,
        }
        if external_id_type:
            subquery_parts.append("external_id_type = :external_id_type")
            query_params["external_id_type"] = str(external_id_type)
        subquery = (
            f"SELECT item_id FROM {DB_TABLE_EXTERNAL_ID_LOOKUP} "
            f"WHERE {' AND '.join(subquery_parts)}"
        )
        query = f"{self.db_table}.item_id IN ({subquery})"
        if limit is not None:
            limited_items = await self.get_library_items_by_query(
                limit=limit,
                extra_query_parts=[query],
                extra_query_params=query_params,
            )
            return sorted(limited_items, key=lambda item: int(item.item_id))

        all_items: list[ItemCls] = []
        offset = 0
        page_size = 500
        while page := await self.get_library_items_by_query(
            limit=page_size,
            offset=offset,
            extra_query_parts=[query],
            extra_query_params=query_params,
        ):
            all_items.extend(page)
            if len(page) < page_size:
                break
            offset += page_size
        return sorted(all_items, key=lambda item: int(item.item_id))

    @final
    async def get_library_item_by_external_id(
        self, external_id: str, external_id_type: ExternalID | None = None
    ) -> ItemCls | None:
        """Get the first library item for the given external id, if present."""
        items = await self.get_library_items_by_external_id(external_id, external_id_type, limit=1)
        return items[0] if items else None

    @final
    async def get_library_items_by_external_ids(
        self, external_ids: set[tuple[ExternalID, str]]
    ) -> list[ItemCls]:
        """Get all library items matching any of the given external identifiers."""
        result: dict[str, ItemCls] = {}
        for external_id_type, external_id in sorted(external_ids, key=external_id_sort_key):
            for item in await self.get_library_items_by_external_id(
                external_id, external_id_type, limit=None
            ):
                result.setdefault(item.item_id, item)
        return list(result.values())

    @final
    async def get_library_item_by_external_ids(
        self, external_ids: set[tuple[ExternalID, str]]
    ) -> ItemCls | None:
        """Get the library item for (one of) the given external ids."""
        items = await self.get_library_items_by_external_ids(external_ids)
        return items[0] if items else None

    @final
    async def get_library_items_by_prov_id(
        self,
        provider_domain: str | None = None,
        provider_instance: str | None = None,
        provider_instance_id_or_domain: str | None = None,
        provider_item_id: str | None = None,
        provider_item_ids: list[str] | None = None,
        limit: int = 500,
        offset: int = 0,
    ) -> list[ItemCls]:
        """
        Fetch all records from library for given provider.

        :param provider_item_ids: When given, batch-match this list of provider
            item ids in a single query (the plural form of provider_item_id);
            takes precedence over provider_item_id when both are passed. An
            empty list matches nothing (distinct from None, which applies no
            item-id filter).
        """
        assert provider_instance_id_or_domain != "library"
        assert provider_domain != "library"
        assert provider_instance != "library"
        if provider_item_ids is not None and not provider_item_ids:
            return []
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
        if provider_item_ids:
            placeholders = ", ".join(f":item_id_{i}" for i in range(len(provider_item_ids)))
            subquery_parts.append(f"provider_mappings.provider_item_id IN ({placeholders})")
            for i, item_id in enumerate(provider_item_ids):
                query_params[f"item_id_{i}"] = item_id
        elif provider_item_id:
            subquery_parts.append("provider_mappings.provider_item_id = :item_id")
            query_params["item_id"] = provider_item_id
        subquery = f"SELECT item_id FROM provider_mappings WHERE {' AND '.join(subquery_parts)}"
        query = f"WHERE {self.db_table}.item_id IN ({subquery})"
        return await self.get_library_items_by_query(
            limit=limit,
            offset=offset,
            extra_query_parts=[query],
            extra_query_params=query_params,
            in_library_only=False,
        )

    @final
    async def iter_library_items_by_prov_id(
        self,
        provider_instance_id_or_domain: str,
        provider_item_id: str | None = None,
    ) -> AsyncGenerator[ItemCls]:
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

    @guard_single_request
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
            provider = cast("MusicProvider | PluginProvider", provider)
            with suppress(MediaNotFoundError):
                async with self.mass.cache.handle_refresh(force_refresh):
                    if self.media_type == MediaType.PLAYLIST:
                        return cast("ItemCls", await provider.get_playlist(item_id))
                    music_prov = cast("MusicProvider", provider)
                    if self.media_type == MediaType.ARTIST:
                        return cast("ItemCls", await music_prov.get_artist(item_id))
                    if self.media_type == MediaType.ALBUM:
                        return cast("ItemCls", await music_prov.get_album(item_id))
                    if self.media_type == MediaType.TRACK:
                        return cast("ItemCls", await music_prov.get_track(item_id))
                    if self.media_type == MediaType.RADIO:
                        return cast("ItemCls", await music_prov.get_radio(item_id))
                    if self.media_type == MediaType.AUDIOBOOK:
                        return cast("ItemCls", await music_prov.get_audiobook(item_id))
                    if self.media_type == MediaType.PODCAST:
                        return cast("ItemCls", await music_prov.get_podcast(item_id))
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
    async def merge_library_items(
        self, target_item_id: str | int, source_item_id: str | int
    ) -> ItemCls:
        """
        Merge one library item into another and return the target item.

        The explicit target is the deterministic winner. Its current values stay authoritative
        where the normal non-overwrite model update keeps them; the source is merged as the
        incoming update. All source state is transferred before the source row is deleted.

        :param target_item_id: Library ID of the item that remains after the merge.
        :param source_item_id: Library ID of the duplicate item that is removed after transfer.
        :raises InvalidDataError: When the IDs are identical or do not belong to this media type.
        """
        target_id = int(target_item_id)
        source_id = int(source_item_id)
        if target_id == source_id:
            msg = "Cannot merge a library item into itself"
            raise InvalidDataError(msg)
        async with self._db_add_lock:
            return await self._merge_library_items_batched(target_id, source_id)

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
        mappings = set(provider_mappings)
        if not mappings:
            return
        async with self._db_add_lock:
            library_item = await self.get_library_item(db_id)
            while True:
                conflicting_item = None
                for mapping in mappings:
                    existing_item = await self.get_library_item_by_prov_id(
                        mapping.item_id, mapping.provider_instance
                    )
                    if existing_item and int(existing_item.item_id) != db_id:
                        conflicting_item = existing_item
                        break
                if conflicting_item is None:
                    break
                self.logger.debug(
                    "merging item id %s into item id %s based on provider mapping",
                    conflicting_item.item_id,
                    library_item.item_id,
                )
                library_item = await self._merge_library_items_batched(
                    db_id, int(conflicting_item.item_id)
                )

            new_mappings = mappings.difference(library_item.provider_mappings)
            if not new_mappings:
                return
            library_item.provider_mappings.update(new_mappings)
            self.mass.music.match_provider_instances(library_item)
            await self.set_provider_mappings(db_id, library_item.provider_mappings)
            self.mass.signal_event(EventType.MEDIA_ITEM_UPDATED, library_item.uri, library_item)

    @final
    async def update_provider_mapping(
        self,
        item_id: str | int,
        provider_instance_id: str,
        provider_item_id: str,
        *,
        available: bool | Any = UNSET,
        in_library: bool | Any = UNSET,
        is_unique: bool | None | Any = UNSET,
        url: str | None | Any = UNSET,
        details: str | None | Any = UNSET,
        audio_format: AudioFormat | Any = UNSET,
    ) -> None:
        """Update an existing provider mapping for a library item."""
        db_id = int(item_id)  # ensure integer
        library_item = await self.get_library_item(db_id)

        # find the current mapping (strictly by provider instance + provider item id)
        cur_mapping: ProviderMapping | None = None
        for mapping in library_item.provider_mappings:
            if (
                mapping.provider_instance == provider_instance_id
                and mapping.item_id == provider_item_id
            ):
                cur_mapping = mapping
                break
        if cur_mapping is None:
            msg = (
                f"Provider mapping {provider_instance_id}/{provider_item_id} "
                f"not found for item {db_id}"
            )
            raise MediaNotFoundError(msg)

        # guard against nulls for NOT NULL columns
        if available is None:
            available = UNSET
        if in_library is None:
            in_library = UNSET

        updates: dict[str, Any] = {}
        if available is not UNSET:
            updates["available"] = bool(available)
        if in_library is not UNSET:
            updates["in_library"] = bool(in_library)
        if is_unique is not UNSET:
            updates["is_unique"] = is_unique
        if url is not UNSET:
            updates["url"] = url
        if details is not UNSET:
            updates["details"] = details
        if audio_format is not UNSET:
            updates["audio_format"] = serialize_to_json(audio_format)

        if not updates:
            return

        match = {
            "media_type": self.media_type.value,
            "item_id": db_id,
            "provider_instance": provider_instance_id,
            "provider_item_id": provider_item_id,
        }
        await self.mass.music.database.update(DB_TABLE_PROVIDER_MAPPINGS, match, updates)

        # Re-fetch the updated item so the event payload reflects persisted DB state.
        updated_item = await self.get_library_item(db_id)
        self.mass.signal_event(EventType.MEDIA_ITEM_UPDATED, updated_item.uri, updated_item)

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

        remaining_mappings = {
            x
            for x in library_item.provider_mappings
            if not (x.provider_instance == provider_instance_id and x.item_id == provider_item_id)
        }
        if not remaining_mappings:
            # this was the last mapping, so remove the entire library item, which also
            # clears its provider mapping rows. Dropping those rows up front would leave
            # the item behind without any mappings if the removal itself fails.
            with suppress(MediaNotFoundError):
                await self.remove_item_from_library(db_id)
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
        library_item.provider_mappings = remaining_mappings
        # if this was the last mapping for the provider instance, strip any artwork
        # that belonged to it (e.g. local file paths that are no longer resolvable)
        images_changed = not any(
            x.provider_instance == provider_instance_id for x in remaining_mappings
        ) and await self._remove_provider_images(db_id, provider_instance_id)
        self.logger.debug(
            "removed provider_mapping %s/%s from item id %s",
            provider_instance_id,
            provider_item_id,
            db_id,
        )
        # the removed provider mapping is itself a change to the item, so always notify
        # (unless suppressed during a bulk cleanup); re-fetch first when images were
        # stripped so the event payload stays accurate
        if not SUPPRESS_MEDIA_ITEM_UPDATES.get():
            event_item = await self.get_library_item(db_id) if images_changed else library_item
            self.mass.signal_event(EventType.MEDIA_ITEM_UPDATED, event_item.uri, event_item)

    @final
    async def remove_provider_mappings(self, item_id: str | int, provider_instance_id: str) -> None:
        """Remove all provider mappings from an item."""
        db_id = int(item_id)  # ensure integer
        try:
            library_item = await self.get_library_item(db_id)
        except MediaNotFoundError:
            # edge case: already deleted / race condition, just drop any leftover rows
            await self.mass.music.database.delete(
                DB_TABLE_PROVIDER_MAPPINGS,
                {
                    "media_type": self.media_type.value,
                    "item_id": db_id,
                    "provider_instance": provider_instance_id,
                },
            )
            return

        remaining_mappings = {
            x for x in library_item.provider_mappings if x.provider_instance != provider_instance_id
        }
        if not remaining_mappings:
            # these were the last mappings, so remove the entire library item, which also
            # clears its provider mapping rows. Dropping those rows up front would leave
            # the item behind without any mappings if the removal itself fails.
            with suppress(MediaNotFoundError):
                await self.remove_item_from_library(db_id)
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
        library_item.provider_mappings = remaining_mappings
        # the item is kept (it still has other providers), but it may carry artwork
        # that belonged to the removed provider (e.g. local file paths that are no
        # longer resolvable), so strip those images from the stored metadata
        images_changed = await self._remove_provider_images(db_id, provider_instance_id)
        self.logger.debug(
            "removed all provider mappings for provider %s from item id %s",
            provider_instance_id,
            db_id,
        )
        # the removed provider mapping(s) are themselves a change to the item, so
        # always notify (unless suppressed during a bulk cleanup); re-fetch first when
        # images were stripped so the event payload stays accurate
        if not SUPPRESS_MEDIA_ITEM_UPDATES.get():
            event_item = await self.get_library_item(db_id) if images_changed else library_item
            self.mass.signal_event(EventType.MEDIA_ITEM_UPDATED, event_item.uri, event_item)

    @final
    async def set_provider_mappings(
        self,
        item_id: str | int,
        provider_mappings: Iterable[ProviderMapping],
        overwrite: bool = False,
    ) -> None:
        """
        Update the provider_mappings table for the media item.

        An empty set of mappings never clears the stored rows: an item without any
        mapping can not be played or resolved.
        """
        db_id = int(item_id)  # ensure integer
        prov_map_objs: list[dict[str, Any]] = []
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
            prov_map_objs.append(prov_map_obj)
        if not prov_map_objs:
            if overwrite:
                # a caller asking to replace all mappings with none is a bug,
                # so keep the stored rows and make the attempt visible
                self.logger.warning(
                    "Ignoring request to clear all provider mappings of %s item id %s",
                    self.media_type.value,
                    db_id,
                )
            return
        if overwrite:
            # on overwrite, clear the provider_mappings table first
            # this is done for filesystem provider changing the path (and thus item_id)
            await self.mass.music.database.delete(
                DB_TABLE_PROVIDER_MAPPINGS,
                {"media_type": self.media_type.value, "item_id": db_id},
            )
        await self.mass.music.database.upsert_many(
            DB_TABLE_PROVIDER_MAPPINGS,
            prov_map_objs,
        )

    @final
    async def set_external_ids(
        self,
        item_id: str | int,
        external_ids: Iterable[tuple[ExternalID, str]],
        replace: bool = False,
    ) -> None:
        """
        Update the external_id_lookup table rows for the media item.

        An empty set never clears the stored rows: identifiers are the strongest
        evidence available when matching an item across providers. An authoritative
        caller can pass ``replace=True`` to persist the given set verbatim, deleting
        existing rows even when the set is empty.

        :param item_id: The library item id.
        :param external_ids: The external ids to store for the item.
        :param replace: Whether to delete rows not present in the given set (allows clearing).
        """
        db_id = int(item_id)  # ensure integer
        external_ids = normalize_external_ids(external_ids)
        if not external_ids and not replace:
            return
        await self.mass.music.database.delete(
            DB_TABLE_EXTERNAL_ID_LOOKUP,
            {"media_type": self.media_type.value, "item_id": db_id},
        )
        if not external_ids:
            return
        await self.mass.music.database.upsert_many(
            DB_TABLE_EXTERNAL_ID_LOOKUP,
            [
                {
                    "media_type": self.media_type.value,
                    "external_id_type": external_id_type,
                    "external_id": external_id,
                    "item_id": db_id,
                }
                for external_id_type, external_id in external_ids
            ],
        )

    @abstractmethod
    async def match_providers(self, db_item: ItemCls) -> None:
        """
        Try to find match on all (streaming) providers for the provided (database) item.

        This is used to link objects of different providers/qualities together.
        """

    if TYPE_CHECKING:

        @overload
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
            genre_ids: int | list[int] | None = None,
            played_only: bool = False,
            in_library_only: bool = False,
            summary: bool = False,
            *,
            collapse_collections: Literal[True],
            reachable_via: list[str] | None = None,
        ) -> list[ItemCls | MediaCollection[ItemCls]]: ...

        @overload
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
            genre_ids: int | list[int] | None = None,
            played_only: bool = False,
            in_library_only: bool = False,
            summary: bool = False,
            *,
            collapse_collections: Literal[False] = False,
            reachable_via: list[str] | None = None,
        ) -> list[ItemCls]: ...

        @overload
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
            genre_ids: int | list[int] | None = None,
            played_only: bool = False,
            in_library_only: bool = False,
            summary: bool = False,
            *,
            collapse_collections: bool,
            reachable_via: list[str] | None = None,
        ) -> list[ItemCls] | list[ItemCls | MediaCollection[ItemCls]]: ...

    @final
    async def get_library_items_by_query(  # noqa: PLR0913
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
        genre_ids: int | list[int] | None = None,
        played_only: bool = False,
        in_library_only: bool = False,
        summary: bool = False,
        *,
        collapse_collections: bool = False,
        reachable_via: list[str] | None = None,
    ) -> list[ItemCls] | list[ItemCls | MediaCollection[ItemCls]]:
        """Fetch MediaItem records from database by building the query."""
        query_params = dict(extra_query_params) if extra_query_params else {}
        query_parts: list[str] = list(extra_query_parts) if extra_query_parts else []
        join_parts: list[str] = list(extra_join_parts) if extra_join_parts else []
        search = self._preprocess_search(search)
        genre_ids = self._preprocess_genre_ids(genre_ids)
        # create special performant random query
        if order_by and order_by.startswith("random"):
            self._apply_random_subquery(
                query_parts=query_parts,
                query_params=query_params,
                join_parts=join_parts,
                favorite=favorite,
                search=search if not collapse_collections else None,
                genre_ids=genre_ids,
                provider_filter=provider_filter,
                played_only=played_only,
                limit=limit,
                in_library_only=in_library_only,
                reachable_via=reachable_via,
            )
        else:
            # apply filters
            self._apply_filters(
                query_parts=query_parts,
                query_params=query_params,
                favorite=favorite,
                search=search if not collapse_collections else None,
                genre_ids=genre_ids,
                provider_filter=provider_filter,
                played_only=played_only,
                in_library_only=in_library_only,
                reachable_via=reachable_via,
            )
        # build and execute final query
        sql_query, base_query_params = self._build_final_query(
            query_parts, join_parts, order_by, summary=summary
        )
        # base query params act as defaults: callers may override them via extra_query_params
        for key, value in base_query_params.items():
            query_params.setdefault(key, value)

        if collapse_collections:
            if search:
                query_params["search"] = f"%{search}%"
            sql_query = await self._adapt_query_for_collections(
                sql_query, query_params, summary=summary, order_by=order_by, search=search
            )

        db_rows = await self.mass.music.database.get_rows_from_query(
            sql_query, query_params, limit=limit, offset=offset
        )
        if collapse_collections:
            items: list[ItemCls | MediaCollection[ItemCls]] = []

            def _parse_method(x: str) -> ItemCls:
                if summary:
                    return cast("ItemCls", self._parse_summary_row(json_loads(x)))
                return cast(
                    "ItemCls",
                    self.item_cls.from_dict(self._parse_db_row(json_loads(x))),
                )

            for db_row in db_rows:
                if db_row["type"] == "single":
                    items.append(_parse_method(db_row["media_data"]))
                elif db_row["type"] == "collection":
                    items.append(
                        MediaCollection[ItemCls](
                            item_id=get_collection_item_id(
                                db_row["name"], item_media_type=self.media_type
                            ),
                            name=db_row["name"],
                            provider="library",
                            provider_mappings=set(),
                            items=UniqueList(
                                [_parse_method(x) for x in json_loads(db_row["media_data"])]
                            ),
                        )
                    )
            return items
        if summary:
            return [cast("ItemCls", self._parse_summary_row(db_row)) for db_row in db_rows]
        return [
            cast("ItemCls", self.item_cls.from_dict(self._parse_db_row(db_row)))
            for db_row in db_rows
        ]

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
        # fetch candidates per external id (best identifier first) and stop at the
        # first verified match; external identifiers may be reused, so verify
        # every candidate before accepting it
        seen_item_ids: set[str] = set()
        for external_id_type, external_id in sorted(item.external_ids, key=external_id_sort_key):
            for cur_item in await self.get_library_items_by_external_id(
                external_id, external_id_type, limit=None
            ):
                if cur_item.item_id in seen_item_ids:
                    continue
                seen_item_ids.add(cur_item.item_id)
                if await self._confirm_library_candidate(cur_item, item):
                    return int(cur_item.item_id)
        # search by normalized exact name match
        query = (
            f"{self.db_table}.search_name IN :search_names "
            f"OR {self.db_table}.search_sort_name = :search_sort_name"
        )
        query_params = {
            "search_names": self._library_match_names(item),
            "search_sort_name": create_safe_string(item.sort_name or "", True, True),
        }
        for db_item in await self.get_library_items_by_query(
            extra_query_parts=[query], extra_query_params=query_params
        ):
            if await self._confirm_library_candidate(db_item, item):
                return int(db_item.item_id)
        return None

    def _library_match_names(self, item: ItemCls | ItemMapping) -> list[str]:
        """
        Return the normalized names a library row for this item may be stored under.

        Override in a subclass when a media type's title carries formatting that the
        stored name keeps but its identity comparison ignores.
        """
        return [create_safe_string(item.name, True, True)]

    async def _confirm_library_candidate(
        self, db_item: ItemCls, item: ItemCls | ItemMapping
    ) -> bool:
        """
        Return True if a library candidate is the same item as the one being added.

        Override in a subclass to confirm a candidate that the items' own metadata
        cannot decide on with additional evidence.

        :param db_item: Existing library item that matched on an external id or name.
        :param item: The (provider) item that is being added to the library.
        """
        return bool(compare_media_item(db_item, item, True))

    def _external_ids_query(
        self, media_type: MediaType | None = None, table_alias: str | None = None
    ) -> str:
        """
        Return a subquery that selects the external ids of a media item as a JSON array.

        :param media_type: Media type to select the external ids for, defaults to
            this controller's media type.
        :param table_alias: (Aliased) table name the subquery correlates against,
            defaults to this controller's table.
        """
        media_type = media_type or self.media_type
        table_alias = table_alias or self.db_table
        return (
            f"(SELECT JSON_GROUP_ARRAY(json_array("
            f"{DB_TABLE_EXTERNAL_ID_LOOKUP}.external_id_type, "
            f"{DB_TABLE_EXTERNAL_ID_LOOKUP}.external_id)) "
            f"FROM {DB_TABLE_EXTERNAL_ID_LOOKUP} "
            f"WHERE {DB_TABLE_EXTERNAL_ID_LOOKUP}.media_type = '{media_type.value}' "
            f"AND {DB_TABLE_EXTERNAL_ID_LOOKUP}.item_id = {table_alias}.item_id)"
        )

    def _provider_mappings_query(self) -> str:
        """Return a subquery that selects the provider mappings of a media item as a JSON array."""
        return f"""(SELECT JSON_GROUP_ARRAY(
            json_object(
                'item_id', pm.provider_item_id,
                'provider_domain', pm.provider_domain,
                'provider_instance', pm.provider_instance,
                'available', pm.available,
                'audio_format', json(pm.audio_format),
                'url', pm.url,
                'details', pm.details,
                'in_library', pm.in_library,
                'is_unique', pm.is_unique
            )) FROM {DB_TABLE_PROVIDER_MAPPINGS} pm
            WHERE pm.item_id = {self.db_table}.item_id
            AND pm.media_type = '{self.media_type.value}')"""

    def _artist_mappings_summary_query(
        self, m2m_table: str, m2m_key: str, include_artist_type: bool = False
    ) -> str:
        """
        Return a subquery selecting the slim artist mappings JSON of a summary row.

        :param m2m_table: The many-to-many table linking artists to this media type.
        :param m2m_key: The column in the m2m table referencing this media type's item id.
        :param include_artist_type: Also select the artist_type of each artist.
        """
        artist_type_part = ",\n                'artist_type', artists.artist_type"
        return f"""(SELECT JSON_GROUP_ARRAY(
            json_object(
                'item_id', artists.item_id,
                'name', artists.name,
                'sort_name', artists.sort_name{artist_type_part if include_artist_type else ""}
            )) FROM artists
            JOIN {m2m_table} ON artists.item_id = {m2m_table}.artist_id
            WHERE {m2m_table}.{m2m_key} = {self.db_table}.item_id)"""

    def _summary_base_columns(self) -> str:
        """Return the SELECT columns shared by every summary query."""
        # the search/sort/statistics columns are selected so ORDER BY (see sort_keys)
        # resolves them from the result set, like the full query's SELECT * does
        return f"""
            {self.db_table}.item_id,
            {self.db_table}.name,
            {self.db_table}.sort_name,
            {self.db_table}.favorite,
            {self.db_table}.search_name AS search_name,
            {self.db_table}.search_sort_name AS search_sort_name,
            {self.db_table}.play_count AS play_count,
            {self.db_table}.last_played AS last_played,
            {self.db_table}.timestamp_added AS timestamp_added,
            {self.db_table}.timestamp_modified AS timestamp_modified,
            json_extract({self.db_table}.metadata, '$.images') AS images,
            json_extract({self.db_table}.metadata, '$.collections') AS collections"""

    async def _localized_search_fallback(
        self, search_query: str, limit: int, offset: int = 0, **call_kwargs: Any
    ) -> list[ItemCls]:
        """
        Retry a library search using the canonical names behind a localized query.

        For genre/playlist searches that return nothing literally, reverse-resolve the query to the
        canonical (English) names of matching localized items and search those, so an item is
        findable by the localized name the user sees. The caller's other filters (favorite,
        order_by, provider and any controller-specific kwargs) are forwarded unchanged so the retry
        behaves like the literal search; results are merged, de-duplicated and paginated here. See
        ``TranslationController.reverse_lookup_media_names``.
        """
        seen: set[Any] = set()
        merged: list[ItemCls] = []
        # iterate the canonical names in a stable order, and fetch each from the start so the
        # offset/limit window can be applied to the merged, de-duplicated result set
        for name in sorted(await self.mass.translations.reverse_lookup_media_names(search_query)):
            for item in await self.library_items(
                search=name,
                limit=limit + offset,
                offset=0,
                _localized_fallback=False,
                **call_kwargs,
            ):
                if item.item_id not in seen:
                    seen.add(item.item_id)
                    merged.append(item)
        return merged[offset : offset + limit]

    @abstractmethod
    async def _add_library_item(
        self,
        item: ItemCls,
        overwrite_existing: bool = False,
    ) -> int:
        """Add item to library and return the database id."""

    @abstractmethod
    async def _update_library_item(
        self,
        item_id: str | int,
        update: ItemCls,
        overwrite: bool = False,
        full_replace: bool = False,
    ) -> None:
        """Update existing library record in the database."""

    def _search_filter_clause(self, search: str, query_params: dict[str, Any]) -> str:
        """Return the SQL WHERE clause fragment used for search filtering."""
        return search_name_match_clause(self.db_table, search, "search", query_params)

    @final
    def _preprocess_search(self, search: str | None) -> str | None:
        """Normalize the search string for use in the search filter clauses."""
        return create_safe_string(search, True, True) if search else search

    @final
    @staticmethod
    def _preprocess_genre_ids(genre_ids: int | list[int] | None) -> list[int] | None:
        if genre_ids is None:
            return None
        if isinstance(genre_ids, list):
            normalized = [int(x) for x in genre_ids]
        else:
            normalized = [int(genre_ids)]
        return normalized or None

    @final
    @staticmethod
    def _clean_query_parts(query_parts: list[str]) -> list[str]:
        """Clean the query parts list by removing duplicate where statements."""
        return [x[5:] if x.lower().startswith("where ") else x for x in query_parts]

    @final
    def _apply_random_subquery(  # noqa: PLR0913
        self,
        query_parts: list[str],
        query_params: dict[str, Any],
        join_parts: list[str],
        favorite: bool | None,
        search: str | None,
        genre_ids: list[int] | None,
        provider_filter: list[str] | None,
        played_only: bool = False,
        limit: int = 500,
        in_library_only: bool = False,
        reachable_via: list[str] | None = None,
    ) -> None:
        """Build a fast random subquery with all filters applied."""
        sub_query_parts = query_parts.copy()
        sub_join_parts = join_parts.copy()

        # Apply all filters to the subquery
        self._apply_filters(
            query_parts=sub_query_parts,
            query_params=query_params,
            favorite=favorite,
            search=search,
            genre_ids=genre_ids,
            provider_filter=provider_filter,
            played_only=played_only,
            in_library_only=in_library_only,
            reachable_via=reachable_via,
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
        favorite: bool | None,
        search: str | None,
        genre_ids: list[int] | None,
        provider_filter: list[str] | None,
        played_only: bool = False,
        in_library_only: bool = False,
        reachable_via: list[str] | None = None,
    ) -> None:
        """Apply search, favorite, and provider filters."""
        # handle search
        if search:
            query_parts.append(self._search_filter_clause(search, query_params))
        # handle favorite filter
        if favorite is not None:
            query_parts.append(f"{self.db_table}.favorite = :favorite")
            query_params["favorite"] = favorite
        # handle played_only filter
        if played_only:
            query_parts.append(f"{self.db_table}.last_played > 0")
        # handle genre filter
        if genre_ids:
            query_params["genre_ids"] = genre_ids
            query_params["genre_media_type"] = self.media_type.value
            query_parts.append(
                f"EXISTS("
                f"SELECT 1 FROM {DB_TABLE_GENRE_MEDIA_ITEM_MAPPING} gm "
                f"WHERE gm.media_id = {self.db_table}.item_id "
                "AND gm.media_type = :genre_media_type "
                "AND gm.genre_id IN :genre_ids)"
            )
        # Apply the provider filter
        if provider_filter or in_library_only:
            query_parts.append(
                self._provider_filter_clause(query_params, provider_filter, in_library_only)
            )
        # Apply the reachability filter, independent of the (in-library) provider filter above
        if reachable_via is not None:
            query_parts.append(self._reachability_filter_clause(query_params, reachable_via))

    @final
    def _reachability_filter_clause(
        self, query_params: dict[str, Any], reachable_via: list[str]
    ) -> str:
        """
        Return the SQL clause that restricts items to those reachable via given providers.

        Unlike `_provider_filter_clause`, this only checks that an available mapping to
        one of the given provider instances exists: it does not require that mapping to
        be in that provider's own library. This is used to answer "can this (already
        in-library) item be played through one of these providers", as opposed to
        "is this item favorited on one of these providers".

        :param query_params: Query params dict; the clause's bound params are added to it.
        :param reachable_via: Only match items with an available mapping to one of these
            provider instances.
        """
        query_params["reachable_via_media_type"] = self.media_type.value
        query_params["reachable_via_providers"] = reachable_via
        return (
            f"EXISTS(SELECT 1 FROM {DB_TABLE_PROVIDER_MAPPINGS} reachable_mappings "
            f"WHERE reachable_mappings.item_id = {self.db_table}.item_id "
            "AND reachable_mappings.media_type = :reachable_via_media_type "
            "AND reachable_mappings.available = 1 "
            "AND reachable_mappings.provider_instance IN :reachable_via_providers)"
        )

    @final
    def _provider_filter_clause(
        self,
        query_params: dict[str, Any],
        provider_filter: list[str] | None,
        in_library_only: bool = False,
    ) -> str:
        """
        Return the SQL clause that restricts items by their provider mappings.

        At least one of provider_filter/in_library_only must be set, otherwise the
        returned clause only asserts that the item has any mapping at all.

        :param query_params: Query params dict; the clause's bound params are added to it.
        :param provider_filter: Only match items mapped to one of these provider instances.
        :param in_library_only: Only match provider mappings that are in the provider's library.
        """
        # NOTE: provider mapping filters are applied as a correlated EXISTS subquery
        # instead of a JOIN + GROUP BY, so SQLite can stream results straight from the
        # sort index instead of materializing/sorting the whole (deduped) result set.
        query_params["provider_media_type"] = self.media_type.value
        conditions = [
            f"provider_mappings.item_id = {self.db_table}.item_id",
            "provider_mappings.media_type = :provider_media_type",
        ]
        if in_library_only:
            conditions.append("provider_mappings.in_library = 1")
        if provider_filter:
            provider_conditions = []
            for idx, prov in enumerate(provider_filter):
                param_name = f"provider_filter_{idx}"
                provider_conditions.append(f"provider_mappings.provider_instance = :{param_name}")
                query_params[param_name] = prov
            conditions.append(f"({' OR '.join(provider_conditions)})")
        return f"EXISTS(SELECT 1 FROM provider_mappings WHERE {' AND '.join(conditions)})"

    @final
    def _build_final_query(
        self,
        query_parts: list[str],
        join_parts: list[str],
        order_by: str | None,
        summary: bool = False,
    ) -> tuple[str, dict[str, Any]]:
        """Build the final SQL query string and its (base) bound query params."""
        sql_query, base_query_params = self.summary_query if summary else self.base_query

        # Add joins
        if join_parts:
            sql_query += f" {' '.join(join_parts)} "

        # Add where clauses
        if query_parts:
            # prevent duplicate where statement
            sql_query += " WHERE " + " AND ".join(self._clean_query_parts(query_parts))

        # Add grouping (only needed when caller-provided joins can fan out rows)
        # and ordering. Without a GROUP BY, SQLite can stream results directly
        # from the sort index instead of sorting the whole result set.
        if join_parts:
            sql_query += f" GROUP BY {self.db_table}.item_id"

        if order_by:
            if sort_key := SORT_KEYS.get(order_by):
                sql_query += f" ORDER BY {sort_key}"

        return sql_query, base_query_params

    @final
    @staticmethod
    def _parse_db_row(db_row: Mapping[str, Any]) -> dict[str, Any]:
        """Parse raw db Mapping into a dict."""
        db_row_dict = dict(db_row)
        db_row_dict["provider"] = "library"
        db_row_dict["favorite"] = bool(db_row_dict["favorite"])
        db_row_dict["item_id"] = str(db_row_dict["item_id"])
        db_row_dict["date_added"] = datetime.fromtimestamp(
            db_row_dict["timestamp_added"], tz=UTC
        ).isoformat()

        for key in JSON_KEYS:
            if key not in db_row_dict:
                continue
            if not (raw_value := db_row_dict[key]):
                continue
            db_row_dict[key] = json_loads(raw_value)

        # parse "fully_played" as bool if present in the row
        if "fully_played" in db_row_dict:
            db_row_dict["fully_played"] = parse_optional_bool(db_row_dict["fully_played"])

        # copy track_album --> album
        if track_album := db_row_dict.get("track_album"):
            db_row_dict["album"] = track_album
            db_row_dict["disc_number"] = track_album["disc_number"]
            db_row_dict["track_number"] = track_album["track_number"]
            # always prefer album image over track image
            if (album_images := track_album.get("images")) and (
                album_thumb := next((x for x in album_images if x["type"] == "thumb"), None)
            ):
                # copy album image to itemmapping single image (on the track)
                db_row_dict["image"] = album_thumb
                # also set image on the album dict for ItemMapping compatibility
                track_album["image"] = album_thumb
                if db_row_dict["metadata"].get("images"):
                    # merge album image with existing images
                    db_row_dict["metadata"]["images"] = [
                        album_thumb,
                        *db_row_dict["metadata"]["images"],
                    ]
                else:
                    db_row_dict["metadata"]["images"] = [album_thumb]

        if audiobook_artists := db_row_dict.get("audiobook_artists"):
            _narrators = []
            _authors = []
            for artist in audiobook_artists:
                artist_type = artist.get("artist_type")
                if artist_type == "author":
                    _authors.append(artist)
                elif artist_type == "narrator":
                    _narrators.append(artist)
            if _authors:
                # prevent overwriting string values
                db_row_dict["authors"] = _authors
            if _narrators:
                # prevent overwriting string values
                db_row_dict["narrators"] = _narrators

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
            plugin_provider_instances = {
                prov.instance_id for prov in self.mass.providers if prov.type == ProviderType.PLUGIN
            }
            # User has a provider filter set
            if provider:
                # Explicit provider filter provided - validate against user's allowed providers
                requested_providers = [provider] if isinstance(provider, str) else provider
                # Only restrict access to music providers.
                final_provider_filter = [
                    p
                    for p in requested_providers
                    if p in user_provider_filter or p in plugin_provider_instances
                ]
                if not final_provider_filter:
                    # No overlap - user requested providers they don't have access to
                    raise InsufficientPermissions(
                        "User does not have permission to access the requested provider(s)."
                    )
            else:
                # No explicit filter - apply user music provider filter but keep plugin providers.
                final_provider_filter = list(
                    dict.fromkeys([*user_provider_filter, *plugin_provider_instances])
                )
        elif provider is not None:
            # No user filter - use the provided filter as is
            final_provider_filter = [provider] if isinstance(provider, str) else provider
        return final_provider_filter

    @final
    def _resolve_reachable_via(self, reachable_via: list[str] | None) -> list[str] | None:
        """
        Resolve a `reachable_via` filter against currently loaded, user-allowed providers.

        :param reachable_via: Requested provider instance ids, or None for no filter.
        :return: None if no filter should be applied. Otherwise, the subset of
            `reachable_via` that is currently active and allowed for the current user
            (per `MusicController.get_active_provider_instances`). An empty list means
            the filter cannot match anything; callers must then return no items rather
            than issue a query.
        """
        if reachable_via is None:
            return None
        if not reachable_via:
            return []
        allowed_providers = set(self.mass.music.get_active_provider_instances())
        return [p for p in reachable_via if p in allowed_providers]

    @final
    def _provider_filter_considering_reachability(
        self,
        provider: str | list[str] | None,
        resolved_reachable_via: list[str] | None,
    ) -> list[str] | None:
        """
        Resolve the `provider` filter, deferring to an active `reachable_via` filter.

        The current user's provider access is already enforced on `resolved_reachable_via`
        by `_resolve_reachable_via`. So when `reachable_via` is active and no explicit
        `provider` filter was requested, skip `_ensure_provider_filter`'s implicit
        injection of the user's provider filter: that would additionally require the
        item's in-library mapping itself to be on one of those providers, which is
        stricter than (and redundant with) what `reachable_via` already checks.

        :param provider: The explicit provider filter, as passed to `library_items`.
        :param resolved_reachable_via: The already-resolved `reachable_via` filter (the
            return value of `_resolve_reachable_via`), or None if not active.
        """
        if resolved_reachable_via is not None and provider is None:
            return None
        return self._ensure_provider_filter(provider)

    @final
    def _select_provider_id(self, library_item: ItemCls) -> tuple[str, str]:
        """Select the correct provider id to use for fetching the item."""
        if not library_item.provider_mappings:
            msg = (
                f"{self.media_type.value} {library_item.item_id} "
                "is no longer available on any provider"
            )
            raise MediaNotFoundError(msg)
        user = get_current_user()
        user_provider_filter = user.provider_filter if user and user.provider_filter else None
        if not user_provider_filter:
            mapping = next(iter(library_item.provider_mappings))
            return (mapping.provider_instance, mapping.item_id)

        # First prefer music provider mappings that are explicitly allowed for this user.
        # prefer user provider filter if available
        for mapping in library_item.provider_mappings:
            provider = self.mass.get_provider(mapping.provider_instance)
            if provider and provider.type == ProviderType.MUSIC:
                if mapping.provider_instance in user_provider_filter:
                    return (mapping.provider_instance, mapping.item_id)

        # If no allowed music mapping exists, fall back to plugin mappings.
        for mapping in library_item.provider_mappings:
            provider = self.mass.get_provider(mapping.provider_instance)
            if provider and provider.type == ProviderType.PLUGIN:
                return (mapping.provider_instance, mapping.item_id)

        # As a final fallback, preserve previous behavior.
        for mapping in library_item.provider_mappings:
            if mapping.provider_instance in user_provider_filter:
                return (mapping.provider_instance, mapping.item_id)

        # fallback to first mapping
        mapping = next(iter(library_item.provider_mappings))
        return (mapping.provider_instance, mapping.item_id)

    async def _remove_provider_images(self, db_id: int, provider_instance_id: str) -> bool:
        """
        Remove images belonging to a provider from a library item's stored metadata.

        :param db_id: The library (database) id of the item.
        :param provider_instance_id: The provider instance whose images should be removed.
        :return: True if any images were removed and the db record was updated.
        """
        # read the raw metadata straight from the db (instead of via get_library_item)
        # to avoid persisting any images that are only injected at read time (such as
        # the album thumb that gets merged into a track's images)
        db_row = await self.mass.music.database.get_row(self.db_table, {"item_id": db_id})
        if not db_row or not (raw_metadata := db_row["metadata"]):
            return False
        metadata = MediaItemMetadata.from_dict(json_loads(raw_metadata))
        if not metadata.images:
            return False
        remaining = UniqueList(
            img for img in metadata.images if img.provider != provider_instance_id
        )
        if len(remaining) == len(metadata.images):
            # nothing belonged to this provider
            return False
        metadata.images = remaining or None
        await self.mass.music.database.update(
            self.db_table,
            {"item_id": db_id},
            {"metadata": serialize_to_json(metadata)},
        )
        return True

    def _sync_details_query_parts(self) -> tuple[str, str, dict[str, Any]]:
        """
        Return extra (columns, joins, params) for this media type's sync-details query.

        Override in a subclass to select additional lightweight columns needed by the
        library sync change detection for this media type.
        """
        return "", "", {}

    def _parse_sync_details_row(self, db_row: Mapping[str, Any]) -> LibraryItemSyncDetails:
        """Parse a raw sync-details db row into a LibraryItemSyncDetails object."""
        return LibraryItemSyncDetails(
            item_id=db_row["item_id"],
            favorite=bool(db_row["favorite"]),
            date_added=datetime.fromtimestamp(db_row["timestamp_added"], tz=UTC),
            provider_mappings=self._parse_sync_details_mappings(db_row),
        )

    @final
    def _parse_sync_details_mappings(self, db_row: Mapping[str, Any]) -> set[ProviderMapping]:
        """Parse the aggregated raw provider mapping rows of a sync-details db row."""
        return {
            ProviderMapping(
                item_id=raw_mapping["item_id"],
                provider_domain=raw_mapping["provider_domain"],
                provider_instance=raw_mapping["provider_instance"],
                available=bool(raw_mapping["available"]),
                in_library=parse_optional_bool(raw_mapping["in_library"]),
                is_unique=parse_optional_bool(raw_mapping["is_unique"]),
            )
            for raw_mapping in json_loads(db_row["provider_mappings"])
        }

    def _parse_summary_row(self, db_row: Mapping[str, Any]) -> MediaItemSummaryType:
        """
        Parse a raw summary db row into a summary item of this controller's media type.

        Override in a subclass to fill additional per-type fields (selected by the
        subclass's summary_query).
        """
        provider_mappings = self._parse_summary_provider_mappings(db_row)
        return self.summary_item_cls(
            item_id=str(db_row["item_id"]),
            provider="library",
            name=db_row["name"],
            sort_name=db_row["sort_name"],
            favorite=bool(db_row["favorite"]),
            provider_mappings=provider_mappings,
            available=self._summary_available(provider_mappings),
            metadata=self._parse_summary_metadata(db_row),
        )

    @final
    @staticmethod
    def _parse_summary_provider_mappings(db_row: Mapping[str, Any]) -> set[ProviderMapping]:
        """Hydrate the provider mappings of a summary row into ProviderMapping objects."""
        if not (raw_mappings := db_row["provider_mappings"]):
            return set()
        return {ProviderMapping.from_dict(x) for x in json_loads(raw_mappings)}

    @final
    @staticmethod
    def _summary_available(provider_mappings: set[ProviderMapping]) -> bool:
        """Compute the availability flag from a summary item's provider mappings."""
        # same semantics as the MediaItem.available property
        if not (available_providers := get_global_cache_value("available_providers")):
            return any(x.available for x in provider_mappings)
        if TYPE_CHECKING:
            available_providers = cast("set[str]", available_providers)
        return any(
            x.available and x.provider_instance in available_providers for x in provider_mappings
        )

    @final
    @staticmethod
    def _parse_summary_metadata(db_row: Mapping[str, Any]) -> MediaItemMetadataSummary:
        """Build the slim metadata of a summary row, carrying only the (first) thumb image."""
        thumb: MediaItemImage | None = None
        if raw_images := db_row["images"]:
            for image in json_loads(raw_images):
                if image["type"] != ImageType.THUMB.value:
                    continue
                thumb = MediaItemImage(
                    type=ImageType.THUMB,
                    path=image["path"],
                    provider=image["provider"],
                    remotely_accessible=image.get("remotely_accessible", False),
                )
                break
        return MediaItemMetadataSummary(images=UniqueList([thumb]) if thumb else None)

    @final
    def _parse_summary_artist_mappings(
        self, db_row: Mapping[str, Any]
    ) -> UniqueList[ItemMappingSummary]:
        """Parse the aggregated slim artist mapping rows of a summary db row."""
        return UniqueList(
            ItemMappingSummary(
                media_type=MediaType.ARTIST,
                item_id=str(raw_mapping["item_id"]),
                provider="library",
                name=raw_mapping["name"],
                sort_name=raw_mapping["sort_name"],
            )
            for raw_mapping in json_loads(db_row["artists"])
        )

    async def _adapt_query_for_collections(
        self,
        sql_query: str,
        query_params: dict[str, Any],
        summary: bool,
        order_by: str | None,
        collection_name: str | None = None,
        search: str | None = None,
    ) -> str:
        cache_key_json_object = f"collection_{self.api_base}"
        json_object = await self.mass.cache.get(key=cache_key_json_object, category=int(summary))
        if json_object is None:
            # get column names of base query
            db_rows = await self.mass.music.database.get_rows_from_query(
                sql_query, query_params, limit=1, offset=0
            )
            # create a sql json_object which queries all these columns
            if db_rows:
                json_object = (
                    "json_object(" + ",".join([f"'{x}',{x}" for x in db_rows[0].keys()]) + ")"  # noqa: SIM118
                )
                await self.mass.cache.set(
                    key=cache_key_json_object, category=int(summary), data=json_object
                )
            else:
                json_object = "json_object()"

        collections_column = "collections" if summary else "json_extract(metadata, '$.collections')"

        supported_order_keys = [
            "name",
            "name_desc",
            "sort_name",
            "sort_name_desc",
            "timestamp_added",
            "timestamp_added_desc",
            "timestamp_modified",
            "timestamp_modified_desc",
            "last_played",
            "last_played_desc",
            "play_count",
            "play_count_desc",
        ]

        # additional order options subject to media type
        # single is targeting a single media item, collection the aggregated ones
        single_extra_order_keys = ""
        collection_extra_order_keys = ""
        if MediaType.AUDIOBOOK.value in self.api_base:
            single_extra_order_keys = "duration,"
            collection_extra_order_keys = "SUM(duration) as duration,"
            supported_order_keys += ["duration", "duration_desc"]

        sql_query = f"""
        SELECT * FROM (

            WITH
                joined_table as ({sql_query}),
                collection_extract as (
                    SELECT
                        name as media_name,
                        timestamp_added,
                        timestamp_modified,
                        last_played,
                        play_count,
                        {single_extra_order_keys}
                        json_extract(iter_coll.value, '$.title') as collection_title,
                        json_extract(iter_coll.value, '$.sequence') as collection_sequence,
                        json_extract(iter_coll.value, '$.search_title') as collection_search_title,
                        json_extract(iter_coll.value, '$.search_sort_title') as collection_search_sort_title,
                        CASE
                            WHEN json_type(iter_coll.value, '$.sequence') IN ('integer', 'real')
                            THEN 1
                            WHEN json_type(iter_coll.value, '$.sequence') = 'text'
                                AND json_valid(json_extract(iter_coll.value, '$.sequence'))
                            THEN CASE
                                WHEN json_type(json_extract(iter_coll.value, '$.sequence'))
                                    IN ('integer', 'real')
                                THEN 1
                                ELSE 0
                            END
                            ELSE 0
                        END as collection_sequence_is_numeric,
                        {json_object} as media_data
                    FROM (
                        SELECT * FROM joined_table
                    ), json_each({collections_column}) as iter_coll
                )
            SELECT
                'collection' as type,
                collection_title as name,
                COALESCE(MAX(collection_search_title), replace(lower(collection_title),' ','')) AS search_name,
                COALESCE(MAX(collection_search_sort_title), replace(lower(collection_title),' ','')) AS search_sort_name,
                MAX(timestamp_added) as timestamp_added,
                MAX(timestamp_modified) as timestamp_modified,
                MAX(last_played) as last_played,
                SUM(play_count) as play_count,
                {collection_extra_order_keys}
                json_group_array(media_data) as media_data
            FROM (
                SELECT * FROM collection_extract
                -- NOTE: The following ORDER_BY to control the aggregation order of json_group_array is undocumented sqlite behavior
                -- Confirmed working with sqlite 3.40.1 & 3.53
                -- Once our image moves to sqlite 3.44 we can and should make use of ORDER_BY in the aggregate itself
                ORDER BY collection_title,
                -- null case
                CASE WHEN collection_sequence IS NULL THEN 1 ELSE 0 END,
                -- numeric before text
                CASE WHEN collection_sequence_is_numeric THEN 0 ELSE 1 END,
                -- order NUMERIC
                CASE WHEN collection_sequence_is_numeric
                    THEN CAST(collection_sequence AS REAL)
                END,
                -- order TEXT
                CASE WHEN NOT collection_sequence_is_numeric
                    THEN collection_sequence
                END COLLATE NOCASE,
                -- order by media name if no sequence given
                CASE
                    WHEN collection_sequence IS NULL
                    THEN media_name
                END COLLATE NOCASE
            )
            GROUP BY collection_title

            UNION ALL

            SELECT 'single', name, search_name, search_sort_name,
                timestamp_added, timestamp_modified, last_played, play_count,
                {single_extra_order_keys}
                {json_object} FROM joined_table
                WHERE {collections_column} IS NULL
                    OR {collections_column} = '[]'
        )
        """

        if collection_name:
            sql_query += " WHERE type = 'collection' AND name = :collection_name"
            return sql_query

        if search:
            sql_query += " WHERE search_name LIKE :search"

        if order_by:
            if order_by not in supported_order_keys:
                self.logger.warning("%s is not supported for order_by key in collections", order_by)
                order_by = "name"  # fallback
            if sort_key := SORT_KEYS.get(order_by):
                sql_query += f" ORDER BY {sort_key}"

        return sql_query

    async def _merge_library_items(self, target_id: int, source_id: int) -> tuple[ItemCls, ItemCls]:
        """Merge the source library item into the target while the controller lock is held."""
        target_item = await self.get_library_item(target_id)
        source_item = await self.get_library_item(source_id)
        await self._validate_library_item_merge(target_item, source_item)
        target_row = await self.mass.music.database.get_row(self.db_table, {"item_id": target_id})
        source_row = await self.mass.music.database.get_row(self.db_table, {"item_id": source_id})
        assert target_row is not None
        assert source_row is not None
        timestamps_added = tuple(
            timestamp
            for timestamp in (
                int(target_row["timestamp_added"] or 0),
                int(source_row["timestamp_added"] or 0),
            )
            if timestamp
        )

        token = SUPPRESS_MEDIA_ITEM_UPDATES.set(True)
        try:
            source_mappings = source_item.provider_mappings
            source_item.provider_mappings = set()
            try:
                await self._update_library_item_for_merge(target_id, source_item)
            finally:
                source_item.provider_mappings = source_mappings

            await self.mass.music.database.execute_write(
                f"""
                UPDATE {self.db_table}
                SET play_count = CASE item_id
                    WHEN :target_id THEN :merged_play_count
                    WHEN :source_id THEN 0
                END
                WHERE item_id IN (:target_id, :source_id)
                """,
                {
                    "target_id": target_id,
                    "source_id": source_id,
                    "merged_play_count": int(target_row["play_count"] or 0)
                    + int(source_row["play_count"] or 0),
                },
            )
            await self.mass.music.database.update(
                self.db_table,
                {"item_id": target_id},
                {
                    "favorite": bool(target_row["favorite"]) or bool(source_row["favorite"]),
                    "last_played": max(
                        int(target_row["last_played"] or 0), int(source_row["last_played"] or 0)
                    ),
                    "timestamp_added": min(timestamps_added) if timestamps_added else 0,
                },
            )
            await self._merge_genre_mappings(target_id, source_id)
            await self._merge_library_item_references(target_id, source_id)
            await self._merge_library_playlog(target_id, source_id)
            # the transfer commits in steps (see `deferred_commit`), so it is ordered to
            # leave the source repairable wherever it is cut short: relations are copied
            # rather than moved, and only dropped once the target holds them and the
            # provider mappings. A source that kept its relations stays a duplicate the
            # reconciliation pass can finish; one that lost its mappings is cleaned up.
            await self._copy_library_item_relations(target_id, source_id)
            await self.mass.music.database.execute_write(
                f"UPDATE {DB_TABLE_PROVIDER_MAPPINGS} SET item_id = :target_id "
                "WHERE media_type = :media_type AND item_id = :source_id",
                {
                    "target_id": target_id,
                    "source_id": source_id,
                    "media_type": self.media_type.value,
                },
            )
            await self._drop_library_item_relations(source_id)
            await MediaControllerBase.remove_item_from_library(self, source_id, recursive=False)
            merged_item = await self.get_library_item(target_id)
        finally:
            SUPPRESS_MEDIA_ITEM_UPDATES.reset(token)

        return source_item, merged_item

    async def _merge_library_items_batched(self, target_id: int, source_id: int) -> ItemCls:
        """Merge library items while batching the transfer's database writes."""
        async with self.mass.music.database.deferred_commit():
            source_item, merged_item = await self._merge_library_items(target_id, source_id)
        if not SUPPRESS_MEDIA_ITEM_UPDATES.get():
            self.mass.signal_event(EventType.MEDIA_ITEM_DELETED, source_item.uri, source_item)
            self.mass.signal_event(EventType.MEDIA_ITEM_UPDATED, merged_item.uri, merged_item)
        return merged_item

    async def _validate_library_item_merge(self, target: ItemCls, source: ItemCls) -> None:
        """Validate that the target and source items can be merged."""
        if target.media_type != self.media_type or source.media_type != self.media_type:
            msg = "Library items must have the controller's media type"
            raise InvalidDataError(msg)

    async def _update_library_item_for_merge(self, item_id: int, update: ItemCls) -> None:
        """Merge model state into an existing library item."""
        await self._update_library_item(item_id, update)

    async def _copy_library_item_relations(self, target_id: int, source_id: int) -> None:
        """Copy the relations that reference the merged media item onto the target."""
        for table, item_column in self._library_item_relations():
            columns = RELATION_TABLE_COLUMNS[table]
            selected = ", ".join(
                ":target_id" if column == item_column else column for column in columns
            )
            await self.mass.music.database.execute_write(
                f"INSERT OR IGNORE INTO {table}({', '.join(columns)}) "
                f"SELECT {selected} FROM {table} WHERE {item_column} = :source_id",
                {"target_id": target_id, "source_id": source_id},
            )

    async def _drop_library_item_relations(self, source_id: int) -> None:
        """Drop the relations of a merged media item once the target holds them."""
        for table, item_column in self._library_item_relations():
            await self.mass.music.database.delete(table, {item_column: source_id})

    def _library_item_relations(self) -> tuple[tuple[str, str], ...]:
        """Return the (table, column) pairs holding relations to this controller's items."""
        if self.media_type == MediaType.ALBUM:
            return (
                (DB_TABLE_ALBUM_ARTISTS, "album_id"),
                (DB_TABLE_ALBUM_TRACKS, "album_id"),
            )
        if self.media_type == MediaType.ARTIST:
            return (
                (DB_TABLE_ALBUM_ARTISTS, "artist_id"),
                (DB_TABLE_AUDIOBOOK_ARTISTS, "artist_id"),
                (DB_TABLE_TRACK_ARTISTS, "artist_id"),
            )
        if self.media_type == MediaType.AUDIOBOOK:
            return ((DB_TABLE_AUDIOBOOK_ARTISTS, "audiobook_id"),)
        if self.media_type == MediaType.TRACK:
            return (
                (DB_TABLE_ALBUM_TRACKS, "track_id"),
                (DB_TABLE_TRACK_ARTISTS, "track_id"),
            )
        return ()

    async def _merge_library_item_references(self, target_id: int, source_id: int) -> None:
        """Transfer references to the source item owned by specialized controllers."""
        return

    async def _merge_genre_mappings(self, target_id: int, source_id: int) -> None:
        """Transfer genre mappings and exclusions to the target item."""
        values = {
            "target_id": target_id,
            "source_id": source_id,
            "media_type": self.media_type.value,
        }
        await self.mass.music.database.execute_write(
            f"""
            INSERT INTO {DB_TABLE_GENRE_MEDIA_ITEM_MAPPING}(
                genre_id, media_id, media_type, alias, is_derived, is_manual
            )
            SELECT genre_id, :target_id, media_type, alias, is_derived, is_manual
            FROM {DB_TABLE_GENRE_MEDIA_ITEM_MAPPING}
            WHERE media_id = :source_id AND media_type = :media_type
            ON CONFLICT(genre_id, media_id, media_type) DO UPDATE SET
                alias = CASE
                    WHEN excluded.is_manual AND NOT is_manual
                    THEN COALESCE(excluded.alias, alias)
                    ELSE COALESCE(alias, excluded.alias)
                END,
                is_derived = is_derived OR excluded.is_derived,
                is_manual = is_manual OR excluded.is_manual
            """,
            values,
        )
        await self.mass.music.database.execute_write(
            f"""
            INSERT OR IGNORE INTO {DB_TABLE_GENRE_MEDIA_ITEM_EXCLUSION}(
                genre_id, media_id, media_type
            )
            SELECT genre_id, :target_id, media_type
            FROM {DB_TABLE_GENRE_MEDIA_ITEM_EXCLUSION}
            WHERE media_id = :source_id AND media_type = :media_type
            """,
            values,
        )
        await self.mass.music.database.delete(
            DB_TABLE_GENRE_MEDIA_ITEM_MAPPING,
            {"media_id": source_id, "media_type": self.media_type.value},
        )
        await self.mass.music.database.delete(
            DB_TABLE_GENRE_MEDIA_ITEM_EXCLUSION,
            {"media_id": source_id, "media_type": self.media_type.value},
        )

    async def _merge_genre_references(self, target_id: int, source_id: int) -> None:
        """Transfer media mappings and exclusions that point to the source genre."""
        values = {"target_id": target_id, "source_id": source_id}
        await self.mass.music.database.execute_write(
            f"""
            INSERT INTO {DB_TABLE_GENRE_MEDIA_ITEM_MAPPING}(
                genre_id, media_id, media_type, alias, is_derived, is_manual
            )
            SELECT :target_id, media_id, media_type, alias, is_derived, is_manual
            FROM {DB_TABLE_GENRE_MEDIA_ITEM_MAPPING}
            WHERE genre_id = :source_id
            ON CONFLICT(genre_id, media_id, media_type) DO UPDATE SET
                alias = CASE
                    WHEN excluded.is_manual AND NOT is_manual
                    THEN COALESCE(excluded.alias, alias)
                    ELSE COALESCE(alias, excluded.alias)
                END,
                is_derived = is_derived OR excluded.is_derived,
                is_manual = is_manual OR excluded.is_manual
            """,
            values,
        )
        await self.mass.music.database.execute_write(
            f"""
            INSERT OR IGNORE INTO {DB_TABLE_GENRE_MEDIA_ITEM_EXCLUSION}(
                genre_id, media_id, media_type
            )
            SELECT :target_id, media_id, media_type
            FROM {DB_TABLE_GENRE_MEDIA_ITEM_EXCLUSION}
            WHERE genre_id = :source_id
            """,
            values,
        )
        await self.mass.music.database.delete(
            DB_TABLE_GENRE_MEDIA_ITEM_MAPPING, {"genre_id": source_id}
        )
        await self.mass.music.database.delete(
            DB_TABLE_GENRE_MEDIA_ITEM_EXCLUSION, {"genre_id": source_id}
        )

    async def _merge_library_playlog(self, target_id: int, source_id: int) -> None:
        """Transfer library-keyed playlog rows using the normal latest-entry semantics."""
        values = {
            "target_id": target_id,
            "source_id": source_id,
            "media_type": self.media_type.value,
        }
        await self.mass.music.database.execute_write(
            f"""
            INSERT INTO {DB_TABLE_PLAYLOG}(
                item_id, provider, media_type, name, image, artists, timestamp,
                fully_played, seconds_played, userid, queue_id, user_initiated, playback_speed
            )
            SELECT
                :target_id, provider, media_type, name, image, artists, timestamp,
                fully_played, seconds_played, userid, queue_id, user_initiated, playback_speed
            FROM {DB_TABLE_PLAYLOG}
            WHERE item_id = :source_id AND provider = 'library' AND media_type = :media_type
            ON CONFLICT(item_id, provider, media_type, userid) DO UPDATE SET
                name = CASE WHEN excluded.timestamp > timestamp THEN excluded.name ELSE name END,
                image = CASE WHEN excluded.timestamp > timestamp THEN excluded.image ELSE image END,
                artists = CASE WHEN excluded.timestamp > timestamp THEN excluded.artists ELSE artists END,
                timestamp = MAX(timestamp, excluded.timestamp),
                fully_played = CASE
                    WHEN excluded.timestamp > timestamp THEN excluded.fully_played ELSE fully_played
                END,
                seconds_played = CASE
                    WHEN excluded.timestamp > timestamp THEN excluded.seconds_played
                    ELSE seconds_played
                END,
                queue_id = CASE
                    WHEN excluded.timestamp > timestamp THEN excluded.queue_id ELSE queue_id
                END,
                user_initiated = user_initiated OR excluded.user_initiated,
                playback_speed = CASE
                    WHEN excluded.timestamp > timestamp THEN excluded.playback_speed
                    ELSE playback_speed
                END
            """,
            values,
        )
        await self.mass.music.database.delete(
            DB_TABLE_PLAYLOG,
            {
                "item_id": source_id,
                "provider": "library",
                "media_type": self.media_type.value,
            },
        )
