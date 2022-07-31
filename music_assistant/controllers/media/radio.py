"""Manage MediaItems of type Radio."""
from __future__ import annotations

import asyncio
from time import time
from typing import List, Optional

from music_assistant.controllers.database import TABLE_RADIOS
from music_assistant.helpers.compare import loose_compare_strings
from music_assistant.helpers.json import json_serializer
from music_assistant.models.enums import EventType, MediaType, ProviderType
from music_assistant.models.event import MassEvent
from music_assistant.models.media_items import Radio, Track

from .base import MediaControllerBase


class RadioController(MediaControllerBase[Radio]):
    """Controller managing MediaItems of type Radio."""

    db_table = TABLE_RADIOS
    media_type = MediaType.RADIO
    item_cls = Radio

    async def versions(
        self,
        item_id: str,
        provider: Optional[ProviderType] = None,
        provider_id: Optional[str] = None,
    ) -> List[Radio]:
        """Return all versions of a radio station we can find on all providers."""
        assert provider or provider_id, "Provider type or ID must be specified"
        radio = await self.get(item_id, provider, provider_id)
        # perform a search on all provider(types) to collect all versions/variants
        prov_types = {item.type for item in self.mass.music.providers}
        all_versions = {
            prov_item.item_id: prov_item
            for prov_items in await asyncio.gather(
                *[self.search(radio.name, prov_type) for prov_type in prov_types]
            )
            for prov_item in prov_items
            if loose_compare_strings(radio.name, prov_item.name)
        }
        # make sure that the 'base' version is included
        for prov_version in radio.provider_ids:
            if prov_version.item_id in all_versions:
                continue
            radio_copy = Radio.from_dict(radio.to_dict())
            radio_copy.item_id = prov_version.item_id
            radio_copy.provider = prov_version.prov_type
            radio_copy.provider_ids = {prov_version}
            all_versions[prov_version.item_id] = radio_copy

        # return the aggregated result
        return all_versions.values()

    async def add(self, item: Radio) -> Radio:
        """Add radio to local db and return the new database item."""
        item.metadata.last_refresh = int(time())
        await self.mass.metadata.get_radio_metadata(item)
        existing = await self.get_db_item_by_prov_id(item.item_id, item.provider)
        if existing:
            db_item = await self.update_db_item(existing.item_id, item)
        else:
            db_item = await self.add_db_item(item)
        self.mass.signal_event(
            MassEvent(
                EventType.MEDIA_ITEM_UPDATED
                if existing
                else EventType.MEDIA_ITEM_ADDED,
                db_item.uri,
                db_item,
            )
        )
        return db_item

    async def add_db_item(self, item: Radio, overwrite_existing: bool = False) -> Radio:
        """Add a new item record to the database."""
        assert item.provider_ids
        async with self._db_add_lock:
            match = {"name": item.name}
            if cur_item := await self.mass.database.get_row(self.db_table, match):
                # update existing
                return await self.update_db_item(
                    cur_item["item_id"], item, overwrite=overwrite_existing
                )

            # insert new item
            new_item = await self.mass.database.insert(self.db_table, item.to_db_row())
            item_id = new_item["item_id"]
            self.logger.debug("added %s to database", item.name)
            # return created object
            return await self.get_db_item(item_id)

    async def update_db_item(
        self,
        item_id: int,
        item: Radio,
        overwrite: bool = False,
    ) -> Radio:
        """Update Radio record in the database."""
        cur_item = await self.get_db_item(item_id)
        if overwrite:
            metadata = item.metadata
            provider_ids = item.provider_ids
        else:
            metadata = cur_item.metadata.update(item.metadata)
            provider_ids = {*cur_item.provider_ids, *item.provider_ids}

        match = {"item_id": item_id}
        await self.mass.database.update(
            self.db_table,
            match,
            {
                # always prefer name from updated item here
                "name": item.name,
                "sort_name": item.sort_name,
                "metadata": json_serializer(metadata),
                "provider_ids": json_serializer(provider_ids),
            },
        )
        self.logger.debug("updated %s in database: %s", item.name, item_id)
        return await self.get_db_item(item_id)

    async def _get_provider_dynamic_tracks(
        self,
        item_id: str,
        provider: Optional[ProviderType] = None,
        provider_id: Optional[str] = None,
        limit: int = 25,
    ) -> List[Track]:
        """Generate a dynamic list of tracks based on the item's content."""
        raise NotImplementedError("Dynamic tracks not supported for Radio MediaItem")

    async def _get_dynamic_tracks(
        self, media_item: Radio, limit: int = 25
    ) -> List[Track]:
        """Get dynamic list of tracks for given item, fallback/default implementation."""
        raise NotImplementedError("Dynamic tracks not supported for Radio MediaItem")
