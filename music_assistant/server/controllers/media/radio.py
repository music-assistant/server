"""Manage MediaItems of type Radio."""
from __future__ import annotations

import asyncio
from time import time

from music_assistant.common.helpers.datetime import utc_timestamp
from music_assistant.common.helpers.json import serialize_to_json
from music_assistant.common.models.enums import EventType, MediaType
from music_assistant.common.models.media_items import Radio, Track
from music_assistant.constants import DB_TABLE_RADIOS
from music_assistant.server.helpers.compare import loose_compare_strings

from .base import MediaControllerBase


class RadioController(MediaControllerBase[Radio]):
    """Controller managing MediaItems of type Radio."""

    db_table = DB_TABLE_RADIOS
    media_type = MediaType.RADIO
    item_cls = Radio

    def __init__(self, *args, **kwargs):
        """Initialize class."""
        super().__init__(*args, **kwargs)
        # register api handlers
        self.mass.register_api_command("music/radios", self.db_items)
        self.mass.register_api_command("music/radio", self.get)
        self.mass.register_api_command("music/radio/versions", self.versions)
        self.mass.register_api_command("music/radio/update", self.update_db_item)
        self.mass.register_api_command("music/radio/delete", self.delete_db_item)

    async def versions(
        self,
        item_id: str,
        provider_domain: str | None = None,
        provider_instance: str | None = None,
    ) -> list[Radio]:
        """Return all versions of a radio station we can find on all providers."""
        assert provider_domain or provider_instance, "Provider type or ID must be specified"
        radio = await self.get(item_id, provider_domain, provider_instance)
        # perform a search on all provider(types) to collect all versions/variants
        provider_domains = {prov.domain for prov in self.mass.music.providers}
        all_versions = {
            prov_item.item_id: prov_item
            for prov_items in await asyncio.gather(
                *[self.search(radio.name, provider_domain) for provider_domain in provider_domains]
            )
            for prov_item in prov_items
            if loose_compare_strings(radio.name, prov_item.name)
        }
        # make sure that the 'base' version is NOT included
        for prov_version in radio.provider_mappings:
            all_versions.pop(prov_version.item_id, None)

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
            EventType.MEDIA_ITEM_UPDATED if existing else EventType.MEDIA_ITEM_ADDED,
            db_item.uri,
            db_item,
        )
        return db_item

    async def add_db_item(self, item: Radio) -> Radio:
        """Add a new item record to the database."""
        assert item.provider_mappings, "Item is missing provider mapping(s)"
        async with self._db_add_lock:
            match = {"name": item.name}
            if cur_item := await self.mass.music.database.get_row(self.db_table, match):
                # update existing
                return await self.update_db_item(cur_item["item_id"], item)

            # insert new item
            item.timestamp_added = int(utc_timestamp())
            item.timestamp_modified = int(utc_timestamp())
            new_item = await self.mass.music.database.insert(self.db_table, item.to_db_row())
            item_id = new_item["item_id"]
            # update/set provider_mappings table
            await self._set_provider_mappings(item_id, item.provider_mappings)
            self.logger.debug("added %s to database", item.name)
            # return created object
            return await self.get_db_item(item_id)

    async def update_db_item(
        self,
        item_id: int,
        item: Radio,
    ) -> Radio:
        """Update Radio record in the database."""
        assert item.provider_mappings, "Item is missing provider mapping(s)"
        cur_item = await self.get_db_item(item_id)
        metadata = cur_item.metadata.update(item.metadata)
        provider_mappings = {*cur_item.provider_mappings, *item.provider_mappings}
        match = {"item_id": item_id}
        await self.mass.music.database.update(
            self.db_table,
            match,
            {
                # always prefer name from updated item here
                "name": item.name,
                "sort_name": item.sort_name,
                "metadata": serialize_to_json(metadata),
                "provider_mappings": serialize_to_json(provider_mappings),
                "timestamp_modified": int(utc_timestamp()),
            },
        )
        # update/set provider_mappings table
        await self._set_provider_mappings(item_id, provider_mappings)
        self.logger.debug("updated %s in database: %s", item.name, item_id)
        return await self.get_db_item(item_id)

    async def _get_provider_dynamic_tracks(
        self,
        item_id: str,
        provider_domain: str | None = None,
        provider_instance: str | None = None,
        limit: int = 25,
    ) -> list[Track]:
        """Generate a dynamic list of tracks based on the item's content."""
        raise NotImplementedError("Dynamic tracks not supported for Radio MediaItem")

    async def _get_dynamic_tracks(self, media_item: Radio, limit: int = 25) -> list[Track]:
        """Get dynamic list of tracks for given item, fallback/default implementation."""
        raise NotImplementedError("Dynamic tracks not supported for Radio MediaItem")
