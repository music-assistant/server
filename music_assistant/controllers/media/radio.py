"""Manage MediaItems of type Radio."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from music_assistant_models.enums import MediaType, ProviderFeature
from music_assistant_models.media_items import ProviderMapping, Radio, Track

from music_assistant.constants import DB_TABLE_RADIOS
from music_assistant.helpers.compare import (
    compare_media_item,
    compare_radio,
    create_safe_string,
    loose_compare_strings,
)
from music_assistant.helpers.database import UNSET
from music_assistant.helpers.json import serialize_to_json
from music_assistant.models.music_provider import MusicProvider

from .base import MediaControllerBase

if TYPE_CHECKING:
    from music_assistant import MusicAssistant


class RadioController(MediaControllerBase[Radio]):
    """Controller managing MediaItems of type Radio."""

    db_table = DB_TABLE_RADIOS
    media_type = MediaType.RADIO
    item_cls = Radio

    def __init__(self, mass: MusicAssistant) -> None:
        """Initialize class."""
        super().__init__(mass)
        # register (extra) api handlers
        api_base = self.api_base
        self.mass.register_api_command(f"music/{api_base}/radio_versions", self.versions)

    async def versions(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
    ) -> list[Radio]:
        """Return all versions of a radio station we can find on all providers."""
        radio = await self.get(item_id, provider_instance_id_or_domain)
        # perform a search on all provider(types) to collect all versions/variants
        all_versions = {
            prov_item.item_id: prov_item
            for prov_items in await asyncio.gather(
                *[
                    self.search(radio.name, provider_domain)
                    for provider_domain in self.mass.music.get_unique_providers()
                ]
            )
            for prov_item in prov_items
            if loose_compare_strings(radio.name, prov_item.name)
        }
        # make sure that the 'base' version is NOT included
        for prov_version in radio.provider_mappings:
            all_versions.pop(prov_version.item_id, None)

        # return the aggregated result
        return list(all_versions.values())

    async def _add_library_item(self, item: Radio, overwrite_existing: bool = False) -> int:
        """Add a new item record to the database."""
        assert self.mass.music.database is not None  # For type checking
        db_id = await self.mass.music.database.insert(
            self.db_table,
            {
                "name": item.name,
                "sort_name": item.sort_name,
                "favorite": item.favorite,
                "metadata": serialize_to_json(item.metadata),
                "external_ids": serialize_to_json(item.external_ids),
                "search_name": create_safe_string(item.name, True, True),
                "search_sort_name": create_safe_string(
                    item.sort_name if item.sort_name is not None else "", True, True
                ),
                "timestamp_added": int(item.date_added.timestamp()) if item.date_added else UNSET,
            },
        )
        # update/set provider_mappings table
        await self.set_provider_mappings(db_id, item.provider_mappings)
        self.logger.debug("added %s to database (id: %s)", item.name, db_id)
        return db_id

    async def _update_library_item(
        self, item_id: str | int, update: Radio, overwrite: bool = False
    ) -> None:
        """Update existing record in the database."""
        db_id = int(item_id)  # ensure integer
        cur_item = await self.get_library_item(db_id)
        metadata = update.metadata if overwrite else cur_item.metadata.update(update.metadata)
        cur_item.external_ids.update(update.external_ids)
        match = {"item_id": db_id}
        name = update.name if overwrite else cur_item.name
        sort_name = update.sort_name if overwrite else cur_item.sort_name or update.sort_name
        assert self.mass.music.database is not None  # For type checking
        await self.mass.music.database.update(
            self.db_table,
            match,
            {
                # always prefer name from updated item here
                "name": name,
                "sort_name": sort_name,
                "metadata": serialize_to_json(metadata),
                "external_ids": serialize_to_json(
                    update.external_ids if overwrite else cur_item.external_ids
                ),
                "search_name": create_safe_string(name, True, True),
                "search_sort_name": create_safe_string(sort_name or "", True, True),
                "timestamp_added": int(update.date_added.timestamp())
                if update.date_added
                else UNSET,
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
        item: Radio,
        preferred_provider_instances: list[str] | None = None,
    ) -> list[Track]:
        """
        Get the list of base tracks from the controller used to calculate the dynamic radio.

        :param item: The Radio to get base tracks for.
        :param preferred_provider_instances: List of preferred provider instance IDs to use.
        """
        msg = "Dynamic tracks not supported for Radio MediaItem"
        raise NotImplementedError(msg)

    async def match_provider(
        self, db_radio: Radio, provider: MusicProvider, strict: bool = True
    ) -> list[ProviderMapping]:
        """
        Try to find match on (streaming) provider for the provided (database) radio.

        This is used to link objects of different providers/qualities together.
        """
        self.logger.debug(
            "Trying to match radio %s on provider %s",
            db_radio.name,
            provider.name,
        )
        matches: list[ProviderMapping] = []
        search_str = db_radio.name
        search_result = await self.search(search_str, provider.instance_id)
        for search_result_item in search_result:
            if not search_result_item.available:
                continue
            if not compare_media_item(db_radio, search_result_item, strict=strict):
                continue
            # we must fetch the full radio version, search results can be simplified objects
            prov_radio = await self.get_provider_item(
                search_result_item.item_id,
                search_result_item.provider,
                fallback=search_result_item,
            )
            if compare_radio(db_radio, prov_radio, strict=strict):
                # 100% match
                matches.extend(prov_radio.provider_mappings)
        if not matches:
            self.logger.debug(
                "Could not find match for Radio %s on provider %s",
                db_radio.name,
                provider.name,
            )
        return matches

    async def match_providers(self, db_radio: Radio) -> None:
        """Try to find match on all (streaming) providers for the provided (database) radio.

        This is used to link objects of different providers/qualities together.
        """
        if db_radio.provider != "library":
            return  # Matching only supported for database items

        # try to find match on all providers
        cur_provider_domains = {x.provider_domain for x in db_radio.provider_mappings}
        for provider in self.mass.music.providers:
            if provider.domain in cur_provider_domains:
                continue
            if ProviderFeature.SEARCH not in provider.supported_features:
                continue
            if not provider.library_supported(MediaType.RADIO):
                continue
            if not provider.is_streaming_provider:
                # matching on unique providers is pointless as they push (all) their content to MA
                continue
            if match := await self.match_provider(db_radio, provider):
                # 100% match, we update the db with the additional provider mapping(s)
                await self.add_provider_mappings(db_radio.item_id, match)
                cur_provider_domains.add(provider.domain)
