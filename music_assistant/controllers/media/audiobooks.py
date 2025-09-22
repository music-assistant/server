"""Manage MediaItems of type Audiobook."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from music_assistant_models.enums import MediaType, ProviderFeature
from music_assistant_models.errors import InvalidDataError
from music_assistant_models.media_items import Artist, Audiobook, UniqueList

from music_assistant.constants import DB_TABLE_AUDIOBOOKS, DB_TABLE_PLAYLOG
from music_assistant.controllers.media.base import MediaControllerBase
from music_assistant.helpers.compare import (
    compare_audiobook,
    compare_media_item,
    create_safe_string,
    loose_compare_strings,
)
from music_assistant.helpers.datetime import utc_timestamp
from music_assistant.helpers.json import serialize_to_json

if TYPE_CHECKING:
    from music_assistant_models.media_items import Track

    from music_assistant.models.music_provider import MusicProvider


class AudiobooksController(MediaControllerBase[Audiobook]):
    """Controller managing MediaItems of type Audiobook."""

    db_table = DB_TABLE_AUDIOBOOKS
    media_type = MediaType.AUDIOBOOK
    item_cls = Audiobook

    def __init__(self, *args, **kwargs) -> None:
        """Initialize class."""
        super().__init__(*args, **kwargs)
        self.base_query = """
        SELECT
            audiobooks.*,
            (SELECT JSON_GROUP_ARRAY(
                json_object(
                'item_id', provider_mappings.provider_item_id,
                    'provider_domain', provider_mappings.provider_domain,
                        'provider_instance', provider_mappings.provider_instance,
                        'available', provider_mappings.available,
                        'audio_format', json(provider_mappings.audio_format),
                        'url', provider_mappings.url,
                        'details', provider_mappings.details
                )) FROM provider_mappings WHERE provider_mappings.item_id = audiobooks.item_id AND media_type = 'audiobook') AS provider_mappings,
            playlog.fully_played AS fully_played,
            playlog.seconds_played AS seconds_played,
            playlog.seconds_played * 1000 as resume_position_ms
            FROM audiobooks
            LEFT JOIN playlog ON playlog.item_id = audiobooks.item_id AND playlog.media_type = 'audiobook'
            """  # noqa: E501
        # register (extra) api handlers
        api_base = self.api_base
        self.mass.register_api_command(f"music/{api_base}/audiobook_versions", self.versions)

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
    ) -> list[Artist]:
        """Get in-database audiobooks."""
        extra_query_params: dict[str, Any] = extra_query_params or {}
        extra_query_parts: list[str] = [extra_query] if extra_query else []
        result = await self._get_library_items_by_query(
            favorite=favorite,
            search=search,
            limit=limit,
            offset=offset,
            order_by=order_by,
            provider=provider,
            extra_query_parts=extra_query_parts,
            extra_query_params=extra_query_params,
        )
        if search and len(result) < 25 and not offset:
            # append author items to result
            extra_query_parts = [
                "WHERE audiobooks.authors LIKE :search or audiobooks.narrators LIKE :search",
            ]
            extra_query_params["search"] = f"%{search}%"
            return result + await self._get_library_items_by_query(
                favorite=favorite,
                search=None,
                limit=limit,
                order_by=order_by,
                provider=provider,
                extra_query_parts=extra_query_parts,
                extra_query_params=extra_query_params,
            )
        return result

    async def versions(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
    ) -> UniqueList[Audiobook]:
        """Return all versions of an audiobook we can find on all providers."""
        audiobook = await self.get_provider_item(item_id, provider_instance_id_or_domain)
        search_query = audiobook.name
        result: UniqueList[Audiobook] = UniqueList()
        for provider_id in self.mass.music.get_unique_providers():
            provider = self.mass.get_provider(provider_id)
            if not provider:
                continue
            if not provider.library_supported(MediaType.AUDIOBOOK):
                continue
            result.extend(
                prov_item
                for prov_item in await self.search(search_query, provider_id)
                if loose_compare_strings(audiobook.name, prov_item.name)
                # make sure that the 'base' version is NOT included
                and not audiobook.provider_mappings.intersection(prov_item.provider_mappings)
            )
        return result

    async def _add_library_item(self, item: Audiobook) -> int:
        """Add a new record to the database."""
        if not isinstance(item, Audiobook):
            msg = "Not a valid Audiobook object (ItemMapping can not be added to db)"
            raise InvalidDataError(msg)
        db_id = await self.mass.music.database.insert(
            self.db_table,
            {
                "name": item.name,
                "sort_name": item.sort_name,
                "version": item.version,
                "favorite": item.favorite,
                "metadata": serialize_to_json(item.metadata),
                "external_ids": serialize_to_json(item.external_ids),
                "publisher": item.publisher,
                "authors": serialize_to_json(item.authors),
                "narrators": serialize_to_json(item.narrators),
                "duration": item.duration,
                "search_name": create_safe_string(item.name, True, True),
                "search_sort_name": create_safe_string(item.sort_name, True, True),
            },
        )
        # update/set provider_mappings table
        await self._set_provider_mappings(db_id, item.provider_mappings)
        self.logger.debug("added %s to database (id: %s)", item.name, db_id)
        await self._set_playlog(db_id, item)
        return db_id

    async def _update_library_item(
        self, item_id: str | int, update: Audiobook, overwrite: bool = False
    ) -> None:
        """Update existing record in the database."""
        db_id = int(item_id)  # ensure integer
        cur_item = await self.get_library_item(db_id)
        metadata = update.metadata if overwrite else cur_item.metadata.update(update.metadata)
        cur_item.external_ids.update(update.external_ids)
        provider_mappings = (
            update.provider_mappings
            if overwrite
            else {*cur_item.provider_mappings, *update.provider_mappings}
        )
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
                "publisher": cur_item.publisher or update.publisher,
                "authors": serialize_to_json(
                    update.authors if overwrite else cur_item.authors or update.authors
                ),
                "narrators": serialize_to_json(
                    update.narrators if overwrite else cur_item.narrators or update.narrators
                ),
                "duration": update.duration or update.duration,
                "search_name": create_safe_string(name, True, True),
                "search_sort_name": create_safe_string(sort_name, True, True),
            },
        )
        # update/set provider_mappings table
        await self._set_provider_mappings(db_id, provider_mappings, overwrite)
        self.logger.debug("updated %s in database: (id %s)", update.name, db_id)
        await self._set_playlog(db_id, update)

    async def radio_mode_base_tracks(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
        limit: int = 25,
    ) -> list[Track]:
        """Get the list of base tracks from the controller used to calculate the dynamic radio."""
        msg = "Dynamic tracks not supported for Radio MediaItem"
        raise NotImplementedError(msg)

    async def match_providers(self, db_audiobook: Audiobook) -> None:
        """Try to find match on all (streaming) providers for the provided (database) audiobook.

        This is used to link objects of different providers/qualities together.
        """
        if db_audiobook.provider != "library":
            return  # Matching only supported for database items
        if not db_audiobook.authors:
            return  # guard
        author_name = db_audiobook.authors[0]

        async def find_prov_match(provider: MusicProvider):
            self.logger.debug(
                "Trying to match audiobook %s on provider %s",
                db_audiobook.name,
                provider.name,
            )
            match_found = False
            search_str = f"{author_name} - {db_audiobook.name}"
            search_result = await self.search(search_str, provider.instance_id)
            for search_result_item in search_result:
                if not search_result_item.available:
                    continue
                if not compare_media_item(db_audiobook, search_result_item):
                    continue
                # we must fetch the full audiobook version, search results can be simplified objects
                prov_audiobook = await self.get_provider_item(
                    search_result_item.item_id,
                    search_result_item.provider,
                    fallback=search_result_item,
                )
                if compare_audiobook(db_audiobook, prov_audiobook):
                    # 100% match, we update the db with the additional provider mapping(s)
                    match_found = True
                    for provider_mapping in search_result_item.provider_mappings:
                        await self.add_provider_mapping(db_audiobook.item_id, provider_mapping)
                        db_audiobook.provider_mappings.add(provider_mapping)
            return match_found

        # try to find match on all providers
        cur_provider_domains = {x.provider_domain for x in db_audiobook.provider_mappings}
        for provider in self.mass.music.providers:
            if provider.domain in cur_provider_domains:
                continue
            if ProviderFeature.SEARCH not in provider.supported_features:
                continue
            if not provider.library_supported(MediaType.AUDIOBOOK):
                continue
            if not provider.is_streaming_provider:
                # matching on unique providers is pointless as they push (all) their content to MA
                continue
            if await find_prov_match(provider):
                cur_provider_domains.add(provider.domain)
            else:
                self.logger.debug(
                    "Could not find match for Audiobook %s on provider %s",
                    db_audiobook.name,
                    provider.name,
                )

    async def _set_playlog(self, db_id: int, media_item: Audiobook) -> None:
        """Update/set the playlog table for the given audiobook db item_id."""
        # cleanup provider specific entries for this item
        # we always prefer the library playlog entry
        for prov_mapping in media_item.provider_mappings:
            if not (provider := self.mass.get_provider(prov_mapping.provider_instance)):
                continue
            await self.mass.music.database.delete(
                DB_TABLE_PLAYLOG,
                {
                    "media_type": self.media_type.value,
                    "item_id": prov_mapping.item_id,
                    "provider": provider.lookup_key,
                },
            )
        if media_item.fully_played is None and media_item.resume_position_ms is None:
            return
        cur_entry = await self.mass.music.database.get_row(
            DB_TABLE_PLAYLOG,
            {
                "media_type": self.media_type.value,
                "item_id": db_id,
                "provider": "library",
            },
        )
        seconds_played = int(media_item.resume_position_ms or 0 / 1000)
        # abort if nothing changed
        if (
            cur_entry
            and cur_entry["fully_played"] == media_item.fully_played
            and abs((cur_entry["seconds_played"] or 0) - seconds_played) > 2
        ):
            return
        await self.mass.music.database.insert(
            DB_TABLE_PLAYLOG,
            {
                "item_id": db_id,
                "provider": "library",
                "media_type": media_item.media_type.value,
                "name": media_item.name,
                "image": serialize_to_json(media_item.image.to_dict())
                if media_item.image
                else None,
                "fully_played": media_item.fully_played,
                "seconds_played": seconds_played,
                "timestamp": utc_timestamp(),
            },
            allow_replace=True,
        )
