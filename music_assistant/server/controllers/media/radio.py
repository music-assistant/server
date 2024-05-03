"""Manage MediaItems of type Radio."""

from __future__ import annotations

import asyncio

from music_assistant.common.helpers.json import serialize_to_json
from music_assistant.common.models.enums import MediaType
from music_assistant.common.models.media_items import Radio, Track
from music_assistant.constants import DB_TABLE_RADIOS
from music_assistant.server.helpers.compare import loose_compare_strings

from .base import MediaControllerBase


class RadioController(MediaControllerBase[Radio]):
    """Controller managing MediaItems of type Radio."""

    db_table = DB_TABLE_RADIOS
    media_type = MediaType.RADIO
    item_cls = Radio

    def __init__(self, *args, **kwargs) -> None:
        """Initialize class."""
        super().__init__(*args, **kwargs)
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
        return all_versions.values()

    async def _add_library_item(self, item: Radio) -> int:
        """Add a new item record to the database."""
        new_item = await self.mass.music.database.insert(
            self.db_table,
            {
                "name": item.name,
                "sort_name": item.sort_name,
                "favorite": item.favorite,
                "metadata": serialize_to_json(item.metadata),
                "external_ids": serialize_to_json(item.external_ids),
            },
        )
        db_id = new_item["item_id"]
        # update/set provider_mappings table
        await self._set_provider_mappings(db_id, item.provider_mappings)
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
        await self.mass.music.database.update(
            self.db_table,
            match,
            {
                # always prefer name from updated item here
                "name": update.name if overwrite else cur_item.name,
                "sort_name": update.sort_name
                if overwrite
                else cur_item.sort_name or update.sort_name,
                "metadata": serialize_to_json(metadata),
                "external_ids": serialize_to_json(
                    update.external_ids if overwrite else cur_item.external_ids
                ),
            },
        )
        # update/set provider_mappings table
        provider_mappings = (
            update.provider_mappings
            if overwrite
            else {*cur_item.provider_mappings, *update.provider_mappings}
        )
        await self._set_provider_mappings(db_id, provider_mappings, overwrite)
        self.logger.debug("updated %s in database: (id %s)", update.name, db_id)

    async def _get_provider_dynamic_tracks(
        self,
        item_id: str,
        provider_instance_id_or_domain: str,
        limit: int = 25,
    ) -> list[Track]:
        """Generate a dynamic list of tracks based on the item's content."""
        msg = "Dynamic tracks not supported for Radio MediaItem"
        raise NotImplementedError(msg)

    async def _get_dynamic_tracks(self, media_item: Radio, limit: int = 25) -> list[Track]:
        """Get dynamic list of tracks for given item, fallback/default implementation."""
        msg = "Dynamic tracks not supported for Radio MediaItem"
        raise NotImplementedError(msg)
