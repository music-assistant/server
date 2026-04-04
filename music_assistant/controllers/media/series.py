"""Manage MediaItems of type Series."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Sequence
from typing import TYPE_CHECKING, Any, cast

from music_assistant_models.enums import MediaType, ProviderFeature
from music_assistant_models.errors import MediaNotFoundError
from music_assistant_models.media_items import (
    Audiobook,
    ProviderMapping,
    Series,
    UniqueList,
)

from music_assistant.constants import DB_TABLE_SERIES
from music_assistant.controllers.media.base import MediaControllerBase
from music_assistant.helpers.compare import (
    compare_media_item,
    compare_series,
    create_safe_string,
    loose_compare_strings,
)
from music_assistant.helpers.database import UNSET
from music_assistant.helpers.json import serialize_to_json
from music_assistant.helpers.util import guard_single_request
from music_assistant.models.music_provider import MusicProvider

if TYPE_CHECKING:
    from music_assistant_models.media_items import Track

    from music_assistant import MusicAssistant


class SeriesController(MediaControllerBase[Series]):
    """Controller managing MediaItems of type Series."""

    db_table = DB_TABLE_SERIES
    media_type = MediaType.SERIES
    item_cls = Series

    def __init__(self, mass: MusicAssistant) -> None:
        """Initialize class."""
        super().__init__(mass)
        # register (extra) api handlers
        api_base = self.api_base
        self.mass.register_api_command(f"music/{api_base}/audiobooks", self.audiobooks)

    async def library_items(
        self,
        favorite: bool | None = None,
        search: str | None = None,
        limit: int = 500,
        offset: int = 0,
        order_by: str = "sort_name",
        provider: str | list[str] | None = None,
        genre: int | list[int] | None = None,
        **kwargs: Any,
    ) -> list[Series]:
        """Get in-database series.

        :param favorite: Filter by favorite status.
        :param search: Filter by search query.
        :param limit: Maximum number of items to return.
        :param offset: Number of items to skip.
        :param order_by: Order by field (e.g. 'sort_name', 'timestamp_added').
        :param provider: Filter by provider instance ID (single string or list).
        :param genre: Filter by genre id(s).
        """
        return await self.get_library_items_by_query(
            favorite=favorite,
            search=search,
            genre_ids=genre,
            limit=limit,
            offset=offset,
            order_by=order_by,
            provider_filter=self._ensure_provider_filter(provider),
            in_library_only=True,
        )

    async def audiobooks(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
    ) -> AsyncGenerator[Audiobook, None]:
        """Return audiobooks for the given provider series id."""
        # always check if we have a library item for this series
        if provider_instance_id_or_domain == "library":
            library_series = await self.get_library_item(item_id)
            if not library_series:
                raise MediaNotFoundError(f"Series {item_id} not found in library")
            provider_instance_id_or_domain, item_id = self._select_provider_id(library_series)

        # series audiobooks are not stored in the db,
        page = 0
        while True:
            audiobooks = await self._get_provider_series_audiobooks(
                item_id,
                provider_instance_id_or_domain,
                page=page,
            )
            if not audiobooks:
                break
            for audiobook in audiobooks:
                yield audiobook
            page += 1

    @guard_single_request  # type: ignore[type-var]  # TODO: fix typing in util.py
    async def _get_provider_series_audiobooks(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
        page: int = 0,
        force_refresh: bool = False,
    ) -> Sequence[Audiobook]:
        """Return playlist tracks for the given provider playlist id."""
        assert provider_instance_id_or_domain != "library"
        if not (provider := self.mass.get_provider(provider_instance_id_or_domain)):
            return []
        provider = cast("MusicProvider", provider)
        async with self.mass.cache.handle_refresh(force_refresh):
            return await provider.get_series_audiobooks(item_id, page=page)

    async def versions(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
    ) -> UniqueList[Series]:
        """Return all versions of an podcast we can find on all providers."""
        series = await self.get_provider_item(item_id, provider_instance_id_or_domain)
        search_query = series.name
        result: UniqueList[Series] = UniqueList()
        for provider_id in self.mass.music.get_unique_providers():
            provider = self.mass.get_provider(provider_id)
            if not isinstance(provider, MusicProvider):
                continue
            if not provider.library_supported(MediaType.SERIES):
                continue
            result.extend(
                prov_item
                for prov_item in await self.search(search_query, provider_id)
                if loose_compare_strings(series.name, prov_item.name)
                # make sure that the 'base' version is NOT included
                and not series.provider_mappings.intersection(prov_item.provider_mappings)
            )
        return result

    async def _add_library_item(self, item: Series, overwrite_existing: bool = False) -> int:
        """Add a new record to the database."""
        db_id = await self.mass.music.database.insert(
            self.db_table,
            {
                "name": item.name,
                "sort_name": item.sort_name,
                "version": item.version,
                "favorite": item.favorite,
                "metadata": serialize_to_json(item.metadata),
                "external_ids": serialize_to_json(item.external_ids),
                "search_name": create_safe_string(item.name, True, True),
                "search_sort_name": create_safe_string(item.sort_name or "", True, True),
                "timestamp_added": int(item.date_added.timestamp()) if item.date_added else UNSET,
                "in_progress": item.in_progress,
                "progress_percent": item.progress_percent,
            },
        )
        # update/set provider_mappings table
        await self.set_provider_mappings(db_id, item.provider_mappings)
        self.logger.debug("added %s to database (id: %s)", item.name, db_id)
        return db_id

    async def _update_library_item(
        self, item_id: str | int, update: Series, overwrite: bool = False
    ) -> None:
        """Update existing record in the database."""
        db_id = int(item_id)  # ensure integer
        cur_item = await self.get_library_item(db_id)
        metadata = update.metadata if overwrite else cur_item.metadata.update(update.metadata)
        cur_item.external_ids.update(update.external_ids)
        name = update.name if overwrite else cur_item.name
        sort_name = update.sort_name if overwrite else cur_item.sort_name or update.sort_name
        await self.mass.music.database.update(
            self.db_table,
            {"item_id": db_id},
            {
                "name": name,
                "sort_name": sort_name,
                "version": update.version if overwrite else cur_item.version or update.version,
                "metadata": serialize_to_json(metadata),
                "external_ids": serialize_to_json(
                    update.external_ids if overwrite else cur_item.external_ids
                ),
                "search_name": create_safe_string(name, True, True),
                "search_sort_name": create_safe_string(sort_name or "", True, True),
                "timestamp_added": int(update.date_added.timestamp())
                if update.date_added
                else UNSET,
                "in_progress": update.in_progress,
                "progress_percent": update.progress_percent,
            },
        )
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
        item: Series,
        preferred_provider_instances: list[str] | None = None,
    ) -> list[Track]:
        """
        Get the list of base tracks from the controller used to calculate the dynamic radio.

        :param item: The Podcast to get base tracks for.
        :param preferred_provider_instances: List of preferred provider instance IDs to use.
        """
        msg = "Dynamic tracks not supported for Series MediaItem"
        raise NotImplementedError(msg)

    async def match_provider(
        self, db_series: Series, provider: MusicProvider, strict: bool = True
    ) -> list[ProviderMapping]:
        """
        Try to find match on (streaming) provider for the provided (database) series.

        This is used to link objects of different providers/qualities together.
        """
        self.logger.debug(
            "Trying to match series %s on provider %s",
            db_series.name,
            provider.name,
        )
        matches: list[ProviderMapping] = []
        search_str = db_series.name
        search_result = await self.search(search_str, provider.instance_id)
        for search_result_item in search_result:
            if not search_result_item.available:
                continue
            if not compare_media_item(db_series, search_result_item, strict=strict):
                continue
            # we must fetch the full podcast version, search results can be simplified objects
            prov_podcast = await self.get_provider_item(
                search_result_item.item_id,
                search_result_item.provider,
                fallback=search_result_item,
            )
            if compare_series(db_series, prov_podcast, strict=strict):
                # 100% match
                matches.extend(prov_podcast.provider_mappings)
        if not matches:
            self.logger.debug(
                "Could not find match for Podcast %s on provider %s",
                db_series.name,
                provider.name,
            )
        return matches

    async def match_providers(self, db_item: Series) -> None:
        """Try to find match on all (streaming) providers for the provided (database) series.

        This is used to link objects of different providers/qualities together.
        """
        if db_item.provider != "library":
            return  # Matching only supported for database items

        # try to find match on all providers
        cur_provider_domains = {x.provider_domain for x in db_item.provider_mappings}
        for provider in self.mass.music.providers:
            if provider.domain in cur_provider_domains:
                continue
            if ProviderFeature.SEARCH not in provider.supported_features:
                continue
            if not provider.library_supported(MediaType.SERIES):
                continue
            if not provider.is_streaming_provider:
                # matching on unique providers is pointless as they push (all) their content to MA
                continue
            if match := await self.match_provider(db_item, provider):
                # 100% match, we update the db with the additional provider mapping(s)
                await self.add_provider_mappings(db_item.item_id, match)
                cur_provider_domains.add(provider.domain)

    async def get(
        self, item_id: str, provider_instance_id_or_domain: str, allow_update_metadata: bool = True
    ) -> Series:
        """Get series, and enhance with progress if available."""
        series = await super().get(item_id, provider_instance_id_or_domain, allow_update_metadata)
        if series.in_progress is not None and series.progress_percent is not None:
            return series

        total_books = 0
        total_books_finished = 0
        in_progress = False
        async for book in self.audiobooks(item_id, provider_instance_id_or_domain):
            total_books += 1
            if book.fully_played:
                total_books_finished += 1
            elif (
                book.fully_played is not None
                and not book.fully_played
                and book.resume_position_ms is not None
                and book.resume_position_ms != 0
            ):
                in_progress = True

        if total_books == 0:
            return series

        series.in_progress = in_progress if total_books != total_books_finished else False
        series.progress_percent = int(total_books_finished / total_books * 100)
        return series
